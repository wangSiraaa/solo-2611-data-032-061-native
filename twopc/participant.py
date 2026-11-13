"""A participant: one independent SQLite file holding business rows, prepared
changes, logical claims and its own local transaction log.

Invariants
----------
1. A prepared transaction owns every key it touches in ``reservation``.
   Ordinary (non-2PC) writes are refused while ANY reservation exists for a
   key, regardless of which txid owns it.  This is the "other transactions
   cannot step over a prepared transaction's keys" rule, implemented as a
   logical claim with a trigger backstop.  Recovery never "fixes" a stuck
   transaction by deleting its lock: the claim is released only by a
   verifiable COMMIT or ABORT decision (or by a local rollback that was
   explicitly authorised by the coordinator's recorded ABORT).
2. Prepare performs *all* checks and writes inside one BEGIN IMMEDIATE
   transaction: staged final values (invisible ``prepared_change`` rows), the
   reservation rows and the PREPARED log record commit atomically.  No visible
   business value changes, but the claim is durable.
3. Commit applies staged values and deletes staging/reservations and appends
   the terminal log record **in one SQLite transaction**, so a participant
   never has business values committed without its terminal state (or vice
   versa).
4. A PREPARED transaction is never rolled back because the coordinator is
   unreachable, a wait timed out or the process restarted.  It can only move
   to COMMITTED or ABORTED given a verifiable coordinator decision.  Rollback
   requires the same proof of decision as commit.
"""
from __future__ import annotations

import os
import sqlite3
from typing import Optional

from .digest import decision_digest, updates_digest
from .request import ABSENT, canonical_json, validate_side
from .util import (
    DemoError,
    crash_after,
    initialize_database,
    open_database,
    sqlite_config,
    stamp,
    write_transaction,
)

P_YES = "YES"
P_NO = "NO"
PREPARED = "PREPARED"
COMMITTED = "COMMITTED"
ABORTED = "ABORTED"

BUSINESS_SCHEMA = """
CREATE TABLE IF NOT EXISTS business (
    key TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
"""

PARTICIPANT_SCHEMA = BUSINESS_SCHEMA + """
-- One row per staged final value for each key the transaction touches.
CREATE TABLE IF NOT EXISTS prepared_change (
    txid  TEXT NOT NULL,
    seq   INTEGER NOT NULL,
    key   TEXT NOT NULL,
    old_value INTEGER,
    new_value INTEGER NOT NULL,
    PRIMARY KEY (txid, seq),
    UNIQUE (txid, key),
    FOREIGN KEY (txid) REFERENCES participant_txn(txid)
);

-- Logical claim: a key touched by a prepared transaction is unavailable to
-- everybody else.  One key can be claimed by at most one txid.
CREATE TABLE IF NOT EXISTS reservation (
    key  TEXT PRIMARY KEY,
    txid TEXT NOT NULL,
    FOREIGN KEY (txid) REFERENCES participant_txn(txid)
);

-- Local participant log.  state is the locally known lifecycle.
CREATE TABLE IF NOT EXISTS participant_txn (
    txid TEXT PRIMARY KEY,
    state TEXT NOT NULL CHECK (state IN ('PREPARED','COMMITTED','ABORTED')),
    updates_json TEXT NOT NULL,
    updates_digest TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    decision TEXT,
    decision_digest TEXT,
    decision_order INTEGER,
    prepared_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS participant_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    txid TEXT,
    event TEXT NOT NULL,
    detail TEXT
);

-- Immutable audit of every 2PC change, written in the same transaction as the
-- terminal state.  It survives commit and abort, so a human (or the verifier)
-- can still see which old value a transaction would have replaced, even
-- though the transactional staging rows have been cleaned up.
CREATE TABLE IF NOT EXISTS change_audit (
    txid TEXT NOT NULL,
    seq INTEGER NOT NULL,
    key TEXT NOT NULL,
    old_value INTEGER,
    new_value INTEGER NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('COMMITTED','ABORTED')),
    finished_at TEXT NOT NULL,
    PRIMARY KEY (txid, key)
);

-- Backstop for reservations: even direct SQL writes cannot change a claimed
-- business key while the claim stands.
CREATE TRIGGER IF NOT EXISTS reservation_guard_insert
AFTER INSERT ON business
WHEN EXISTS (SELECT 1 FROM reservation WHERE reservation.key = NEW.key)
BEGIN
    SELECT RAISE(ABORT, 'key is reserved by a prepared transaction');
END;

CREATE TRIGGER IF NOT EXISTS reservation_guard_update
AFTER UPDATE ON business
WHEN EXISTS (SELECT 1 FROM reservation WHERE reservation.key = NEW.key)
BEGIN
    SELECT RAISE(ABORT, 'key is reserved by a prepared transaction');
END;

CREATE TRIGGER IF NOT EXISTS reservation_guard_delete
AFTER DELETE ON business
WHEN EXISTS (SELECT 1 FROM reservation WHERE reservation.key = OLD.key)
BEGIN
    SELECT RAISE(ABORT, 'key is reserved by a prepared transaction');
END;
"""


def _log(conn: sqlite3.Connection, txid: Optional[str], event: str, detail=None) -> None:
    conn.execute(
        "INSERT INTO participant_log (at, txid, event, detail) VALUES (?,?,?,?)",
        (stamp(), txid, event, None if detail is None else canonical_json(detail)),
    )


def _schema_ready(conn: sqlite3.Connection) -> None:
    """Create tables/triggers if necessary.

    ``executescript`` runs its own transaction and is idempotent thanks to
    IF NOT EXISTS, so a partially initialised file is safe to reopen.
    """
    conn.executescript(PARTICIPANT_SCHEMA)


class Participant:
    def __init__(self, name: str, db_path: str):
        self.name = name
        self.path = os.path.abspath(db_path)
        if not os.path.exists(self.path):
            raise DemoError(
                f"participant {name} database not found: {self.path} "
                "(run the init command first)"
            )
        self.conn = open_database(self.path)
        _schema_ready(self.conn)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Participant":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- business values ---------------------------------------------------

    def get_value(self, key: str) -> Optional[int]:
        row = self.conn.execute(
            "SELECT value FROM business WHERE key=?", (key,)
        ).fetchone()
        return None if row is None else row["value"]

    def snapshot(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT key, value FROM business ORDER BY key").fetchall()
        return {row["key"]: row["value"] for row in rows}

    def ordinary_set(self, key: str, value: int) -> None:
        """An ordinary local write, subject to reservations.

        Demonstrates that prepared logical claims are enforced against non-2PC
        traffic too.  The trigger is the last line of defence; the explicit
        check yields a clearer error.
        """
        with write_transaction(self.conn) as c:
            claimed = c.execute(
                "SELECT txid FROM reservation WHERE key=?", (key,)
            ).fetchone()
            if claimed is not None:
                raise DemoError(
                    f"key {key!r} is reserved by prepared transaction "
                    f"{claimed['txid']!r}; refusing ordinary write"
                )
            c.execute(
                "INSERT INTO business(key, value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            _log(c, None, "ORDINARY_SET", {"key": key, "value": value})

    # -- log access --------------------------------------------------------

    def txn_row(self, txid: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM participant_txn WHERE txid=?", (txid,)
        ).fetchone()

    def log_rows(self, limit: int = 100) -> list[dict]:
        return [
            dict(r)
            for r in self.conn.execute(
                "SELECT id, at, txid, event, detail FROM participant_log "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            )
        ]

    def txn_list(self, limit: int = 100) -> list[dict]:
        return [
            dict(r)
            for r in self.conn.execute(
                "SELECT txid, state, decision, decision_order, updates_digest, "
                "prepared_at, finished_at FROM participant_txn "
                "ORDER BY prepared_at DESC LIMIT ?",
                (limit,),
            )
        ]

    def reservations_for(self, txid: str) -> list[str]:
        return [
            r["key"]
            for r in self.conn.execute(
                "SELECT key FROM reservation WHERE txid=? ORDER BY key", (txid,)
            )
        ]

    # -- 2PC: prepare ------------------------------------------------------

    def prepare(self, txid: str, updates: list[dict], req_digest: str) -> dict:
        """Validate every expectation and durably stage the outcome."""
        try:
            normalized = validate_side(self.name, updates)
        except DemoError as exc:
            # An illegal request is answered NO, not raised: the protocol
            # treats "cannot prepare" and "conditions do not hold" the same.
            with write_transaction(self.conn) as c:
                _log(c, txid, "PREPARE_NO", {"reason": exc.message,
                                             "kind": "invalid_request"})
            return {
                "participant": self.name,
                "txid": txid,
                "decision": P_NO,
                "reason": exc.message,
                "invalid": True,
            }
        digest = updates_digest(normalized)
        with write_transaction(self.conn) as c:
            existing = c.execute(
                "SELECT * FROM participant_txn WHERE txid=?", (txid,)
            ).fetchone()
            if existing is not None:
                return self._replay_prepare(c, existing, digest, req_digest)
            updates = normalized

            # Phase A: read and check every expected old value.
            old_values: dict[str, Optional[int]] = {}
            for item in updates:
                key = item["key"]
                expected = item["expected"]
                row = c.execute(
                    "SELECT value FROM business WHERE key=?", (key,)
                ).fetchone()
                actual = None if row is None else row["value"]
                old_values[key] = actual
                if expected == ABSENT:
                    if actual is not None:
                        return self._refuse_prepare(
                            c, txid, f"key {key!r}: expected ABSENT but value "
                            f"{actual} exists",
                        )
                else:
                    if actual is None:
                        return self._refuse_prepare(
                            c, txid, f"key {key!r}: expected {expected} but key "
                            "is ABSENT",
                        )
                    if actual != expected:
                        return self._refuse_prepare(
                            c, txid, f"key {key!r}: expected {expected} but found "
                            f"{actual}",
                        )

            # Phase B: check the claims of *other* prepared transactions.
            for item in updates:
                key = item["key"]
                held = c.execute(
                    "SELECT txid FROM reservation WHERE key=?", (key,)
                ).fetchone()
                if held is not None:
                    return self._refuse_prepare(
                        c, txid, f"key {key!r} is reserved by prepared "
                        f"transaction {held['txid']!r}",
                    )

            # Phase C: stage final values, claims and the PREPARED record
            # atomically.  The business table is untouched here.  The parent
            # row is inserted first so immediate foreign-key checks pass.
            c.execute(
                "INSERT INTO participant_txn("
                "txid, state, updates_json, updates_digest, request_digest, "
                "prepared_at) VALUES (?,?,?,?,?,?)",
                (txid, PREPARED, canonical_json(updates), digest, req_digest, stamp()),
            )
            for seq, item in enumerate(updates):
                c.execute(
                    "INSERT INTO prepared_change(txid, seq, key, old_value, new_value) "
                    "VALUES (?,?,?,?,?)",
                    (txid, seq, item["key"], old_values[item["key"]], item["new"]),
                )
                c.execute(
                    "INSERT INTO reservation(key, txid) VALUES (?,?)",
                    (item["key"], txid),
                )
            _log(c, txid, "PREPARE_YES", {"keys": [i["key"] for i in updates]})
            return {
                "participant": self.name,
                "txid": txid,
                "decision": P_YES,
                "updates_digest": digest,
                "state": PREPARED,
            }

    def _refuse_prepare(self, c, txid: str, reason: str) -> dict:
        # A refused prepare leaves no staged rows and no claims; only a log
        # line, which is diagnostic rather than protocol state.
        _log(c, txid, "PREPARE_NO", {"reason": reason})
        return {
            "participant": self.name,
            "txid": txid,
            "decision": P_NO,
            "reason": reason,
        }

    def _replay_prepare(self, c, existing: sqlite3.Row, digest: str, req_digest: str) -> dict:
        if existing["updates_digest"] != digest or existing["request_digest"] != req_digest:
            raise DemoError(
                f"txid {existing['txid']!r} was already used with different "
                "content; refusing to reuse the id",
                code="TXID_CONTENT_MISMATCH",
            )
        state = existing["state"]
        _log(c, existing["txid"], "PREPARE_REPLAY", {"state": state})
        if state in (PREPARED, COMMITTED):
            return {
                "participant": self.name,
                "txid": existing["txid"],
                "decision": P_YES,
                "updates_digest": digest,
                "state": state,
                "replayed": True,
            }
        return {
            "participant": self.name,
            "txid": existing["txid"],
            "decision": P_NO,
            "reason": "transaction already aborted locally",
            "state": ABORTED,
            "replayed": True,
        }

    # -- 2PC: commit / abort ----------------------------------------------

    def apply_decision(
        self, txid: str, req_digest: str, decision: str, order: int,
        dmac: str, *,
        crash_before: bool = False, crash_after_commit: bool = False,
    ) -> dict:
        """Apply the staged values (or discard them) together with the
        terminal state, after verifying the coordinator's decision.

        ``dmac`` must equal H(txid, request_digest, decision, order) recomputed
        locally, so a forged, stale or mismatched decision is refused.
        """
        decision = decision.upper()
        if decision not in ("COMMIT", "ABORT"):
            raise DemoError(f"invalid coordinator decision {decision!r}")
        expected_mac = decision_digest(txid, req_digest, decision, order)
        if dmac != expected_mac:
            raise DemoError(
                "coordinator decision digest does not verify against the stored "
                "request digest; refusing to act on an unverifiable decision",
                code="BAD_DECISION_DIGEST",
            )
        if crash_before:
            raise DemoError(
                "simulated crash before applying", code="SIMULATED_CRASH"
            )

        with write_transaction(self.conn) as c:
            existing = c.execute(
                "SELECT * FROM participant_txn WHERE txid=?", (txid,)
            ).fetchone()
            if existing is None:
                raise DemoError(
                    f"no local record for txid {txid!r}; the participant never "
                    "prepared this transaction",
                    code="NOT_PREPARED",
                )
            if existing["request_digest"] != req_digest:
                raise DemoError(
                    f"txid {txid!r} belongs to a different request",
                    code="TXID_CONTENT_MISMATCH",
                )
            state = existing["state"]

            # Idempotent replay: the same decision arriving again.
            if (decision == "COMMIT" and state == COMMITTED) or (
                decision == "ABORT" and state == ABORTED
            ):
                _log(c, txid, f"{decision}_REPLAY", None)
                return {
                    "participant": self.name, "txid": txid,
                    "state": state, "replayed": True,
                }
            # Contradictory decision against an already terminal state.
            if state in (COMMITTED, ABORTED):
                raise DemoError(
                    f"txid {txid!r} is already {state}; a {decision} decision "
                    "contradicts the recorded outcome",
                    code="CONTRADICTORY_DECISION",
                )
            if state != PREPARED:
                raise DemoError(f"unexpected local state {state!r} for txid {txid!r}")

            if decision == "COMMIT":
                staged = c.execute(
                    "SELECT key, old_value, new_value FROM prepared_change "
                    "WHERE txid=? ORDER BY seq", (txid,)
                ).fetchall()
                # Re-verify preconditions one last time.  This is a safety net:
                # the triggers already prevent anybody from changing a claimed
                # key, so a mismatch here means a real bug, not normal flow.
                for row in staged:
                    cur = c.execute(
                        "SELECT value FROM business WHERE key=?", (row["key"],)
                    ).fetchone()
                    now = None if cur is None else cur["value"]
                    if now != row["old_value"]:
                        raise DemoError(
                            f"staged precondition for {row['key']!r} no longer "
                            "holds; refusing to commit",
                            code="PRECONDITION_LOST",
                        )
                # Drop our own claims first, otherwise the guard triggers fire
                # on our legitimate writes.
                c.execute("DELETE FROM reservation WHERE txid=?", (txid,))
                for row in staged:
                    c.execute(
                        "INSERT INTO business(key, value) VALUES (?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (row["key"], row["new_value"]),
                    )
                c.execute("DELETE FROM prepared_change WHERE txid=?", (txid,))
                new_state = COMMITTED
                audit_rows = [(r["key"], r["old_value"], r["new_value"])
                              for r in staged]
            else:
                staged = c.execute(
                    "SELECT key, old_value, new_value FROM prepared_change "
                    "WHERE txid=? ORDER BY seq", (txid,)
                ).fetchall()
                c.execute("DELETE FROM reservation WHERE txid=?", (txid,))
                c.execute("DELETE FROM prepared_change WHERE txid=?", (txid,))
                new_state = ABORTED
                audit_rows = [(r["key"], r["old_value"], r["new_value"])
                              for r in staged]

            now = stamp()
            for seq, (key, old_value, new_value) in enumerate(audit_rows):
                c.execute(
                    "INSERT INTO change_audit(txid, seq, key, old_value, "
                    "new_value, outcome, finished_at) VALUES (?,?,?,?,?,?,?)",
                    (txid, seq, key, old_value, new_value, new_state, now),
                )

            c.execute(
                "UPDATE participant_txn SET state=?, decision=?, decision_digest=?, "
                "decision_order=?, finished_at=? WHERE txid=?",
                (new_state, decision, dmac, order, stamp(), txid),
            )
            _log(c, txid, f"{decision}_APPLIED", {"state": new_state})
            result = {
                "participant": self.name, "txid": txid,
                "state": new_state, "replayed": False,
            }

        # "Committed locally, receipt lost": both the business values and the
        # terminal record are durable at this point.  The fault is armed only
        # for the process that was launched with the crash point, so a later
        # recover run (without the flag) can replay that decision idempotently.
        if crash_after_commit:
            crash_after(f"{self.name}-after-commit")
        return result

    # -- status / diagnostics ---------------------------------------------

    def status(self, txid: str) -> dict:
        row = self.txn_row(txid)
        if row is None:
            return {
                "participant": self.name, "txid": txid, "recorded": False,
                "state": None, "reservations": [],
            }
        return {
            "participant": self.name,
            "txid": txid,
            "recorded": True,
            "state": row["state"],
            "decision": row["decision"],
            "decision_order": row["decision_order"],
            "updates_digest": row["updates_digest"],
            "prepared_at": row["prepared_at"],
            "finished_at": row["finished_at"],
            "reservations": self.reservations_for(txid),
            "staged_changes": [
                dict(r)
                for r in self.conn.execute(
                    "SELECT seq, key, old_value, new_value FROM prepared_change "
                    "WHERE txid=? ORDER BY seq", (txid,)
                )
            ],
        }

    def audit_rows(self) -> list[dict]:
        return [
            dict(r) for r in self.conn.execute(
                "SELECT txid, seq, key, old_value, new_value, outcome, "
                "finished_at FROM change_audit ORDER BY finished_at, txid, seq"
            )
        ]

    def describe(self) -> dict:
        return {
            "participant": self.name,
            "database": self.path,
            "sqlite_config": sqlite_config(self.path),
            "business": self.snapshot(),
            "transactions": self.txn_list(limit=50),
            "reservations": [
                dict(r)
                for r in self.conn.execute(
                    "SELECT key, txid FROM reservation ORDER BY key"
                )
            ],
            "change_audit": self.audit_rows(),
            "log": self.log_rows(limit=30),
        }


def init_participant(name: str, db_path: str, seed: Optional[dict[str, int]] = None) -> dict:
    conn = initialize_database(db_path)
    try:
        conn.executescript(PARTICIPANT_SCHEMA)
        with write_transaction(conn) as c:
            _log(c, None, "INIT", {"participant": name})
            if seed:
                existing = c.execute("SELECT COUNT(*) AS n FROM business").fetchone()["n"]
                if existing:
                    raise DemoError(
                        f"participant {name} already has {existing} business "
                        "rows; refusing to reseed",
                        code="ALREADY_INITIALIZED",
                    )
                for key, value in seed.items():
                    c.execute("INSERT INTO business(key, value) VALUES (?,?)",
                              (key, int(value)))
                    _log(c, None, "SEED", {"key": key, "value": int(value)})
    finally:
        conn.close()
    return {
        "participant": name,
        "database": os.path.abspath(db_path),
        "sqlite_config": sqlite_config(db_path),
    }


def read_participant_status(name: str, path: str, txid: str) -> dict:
    """Read-only status view, usable while no writer is running."""
    if not os.path.exists(path):
        raise DemoError(f"participant {name} database not found: {path}")
    conn = open_database(path, read_only=True)
    try:
        if not _table_present(conn, "participant_txn"):
            return {"participant": name, "txid": txid, "recorded": False,
                    "state": None, "reservations": []}
        row = conn.execute(
            "SELECT * FROM participant_txn WHERE txid=?", (txid,)
        ).fetchone()
        if row is None:
            return {"participant": name, "txid": txid, "recorded": False,
                    "state": None, "reservations": []}
        return {
            "participant": name, "txid": txid, "recorded": True,
            "state": row["state"], "decision": row["decision"],
            "decision_order": row["decision_order"],
            "updates_digest": row["updates_digest"],
            "prepared_at": row["prepared_at"], "finished_at": row["finished_at"],
            "reservations": [
                r["key"] for r in conn.execute(
                    "SELECT key FROM reservation WHERE txid=? ORDER BY key", (txid,)
                )
            ],
            "staged_changes": [
                dict(r) for r in conn.execute(
                    "SELECT seq, key, old_value, new_value FROM prepared_change "
                    "WHERE txid=? ORDER BY seq", (txid,)
                )
            ],
        }
    finally:
        conn.close()


def _table_present(conn, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None
