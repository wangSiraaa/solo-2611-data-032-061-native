"""The coordinator: one independent SQLite file with the durable decision log.

Rules implemented here
----------------------
* The decision is written with ``synchronous=FULL`` and an explicit marker
  ("DECIDED_COMMIT"/"DECIDED_ABORT") *before* any participant is contacted
  with the outcome, and it is append-only: the decision columns of an existing
  transaction are never changed.  A decision, once saved, cannot be altered.
* Recovery without a recorded final decision is ABORT: it records ABORT
  (subject to the rule that a decided row is immutable) and notifies both
  participants, which is safe because neither side can have committed by the
  protocol's ordering alone.
* With a recorded COMMIT, recovery re-drives the participants that are not
  finished; it never compensates with a rollback.  With a recorded ABORT it
  re-drives the abort.
* ``order`` is a monotonically increasing coordinator sequence number.  Every
  fan-out (commit or abort) to the same participant for a txid uses the same
  ``order``, so an identical replay reuses the exact same decision digest and
  a later, contradictory instruction has a different digest and is refused by
  the participant.
* The coordinator only reports a request COMPLETE once both participants are
  COMMITTED/ABORTED as they should be.  While one side is finished and the
  other is still PREPARED the request is reported as IN_PROGRESS; this
  transient is permitted but never hidden.
"""
from __future__ import annotations

import os
import sqlite3
from typing import Optional

from .digest import decision_digest, request_digest
from .participant import ABORTED, COMMITTED, read_participant_status
from .request import canonical_json, canonicalize_lenient
from .util import (
    DemoError,
    initialize_database,
    open_database,
    sqlite_config,
    stamp,
    write_transaction,
)

COORDINATORS = ("p1", "p2")

# Lifecycle of a coordinator-side transaction.
OPEN = "OPEN"                       # recorded, no complete prepare reply yet
PREPARED_BOTH = "PREPARED_BOTH"     # both said YES, decision not yet saved
DECIDED_COMMIT = "DECIDED_COMMIT"   # durable COMMIT
DECIDED_ABORT = "DECIDED_ABORT"     # durable ABORT
COMPLETE = "COMPLETE"               # both participants finished as decided

COORDINATOR_SCHEMA = """
CREATE TABLE IF NOT EXISTS coordinator_txn (
    txid TEXT PRIMARY KEY,
    state TEXT NOT NULL CHECK (state IN (
        'OPEN','PREPARED_BOTH','DECIDED_COMMIT','DECIDED_ABORT','COMPLETE')),
    request_json TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    updates_digest_p1 TEXT NOT NULL,
    updates_digest_p2 TEXT NOT NULL,
    decision TEXT,
    decision_digest TEXT,
    decision_order INTEGER,
    created_at TEXT NOT NULL,
    decided_at TEXT,
    completed_at TEXT
);

-- What a participant has reported back, plus the prepare answer.
CREATE TABLE IF NOT EXISTS participant_reply (
    txid TEXT NOT NULL,
    participant TEXT NOT NULL CHECK (participant IN ('p1','p2')),
    prepare TEXT NOT NULL,
    prepare_reason TEXT,
    state TEXT NOT NULL,
    delivered_order INTEGER,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (txid, participant),
    FOREIGN KEY (txid) REFERENCES coordinator_txn(txid)
);

-- Append-only human-readable coordinator log.  Every row is a fact one can
-- read with psql/sqlite by hand; rows are never edited.
CREATE TABLE IF NOT EXISTS coordinator_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    txid TEXT,
    event TEXT NOT NULL,
    detail TEXT
);

-- Monotonic sequence shared by all deliveries.  Using a separate, durable
-- counter rather than wall-clock time avoids ties and makes "later" well
-- defined even across rapid retries.
CREATE TABLE IF NOT EXISTS coordinator_counter (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
"""


def _log(c, txid: Optional[str], event: str, detail=None) -> None:
    c.execute(
        "INSERT INTO coordinator_log (at, txid, event, detail) VALUES (?,?,?,?)",
        (stamp(), txid, event, None if detail is None else canonical_json(detail)),
    )


def _next_order(c) -> int:
    row = c.execute(
        "SELECT value FROM coordinator_counter WHERE name='delivery_order'"
    ).fetchone()
    if row is None:
        c.execute(
            "INSERT INTO coordinator_counter(name, value) VALUES ('delivery_order', 1)"
        )
        return 1
    value = row["value"] + 1
    c.execute(
        "UPDATE coordinator_counter SET value=? WHERE name='delivery_order'", (value,)
    )
    return value


class Coordinator:
    def __init__(self, db_path: str):
        self.path = os.path.abspath(db_path)
        if not os.path.exists(self.path):
            raise DemoError(
                f"coordinator database not found: {self.path} "
                "(run the init command first)"
            )
        self.conn = open_database(self.path)
        self.conn.executescript(COORDINATOR_SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Coordinator":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- request acceptance ------------------------------------------------

    def register(self, request: dict) -> dict:
        """Persist a request after validating it and pinning its content.

        A fresh txid is inserted as OPEN.  A repeated txid with identical
        content is accepted idempotently.  A repeated txid with any difference
        (updates, expected values, endpoints, ordering) is refused outright.
        """
        # Structural acceptance uses the lenient path so that a semantically
        # illegal request still reaches prepare and is answered with NO.
        canonical = canonicalize_lenient(request)
        req_digest = request_digest(canonical)
        from .digest import updates_digest

        ud1 = updates_digest(canonical["participants"]["p1"])
        ud2 = updates_digest(canonical["participants"]["p2"])
        txid = canonical["txid"]

        with write_transaction(self.conn) as c:
            row = c.execute(
                "SELECT * FROM coordinator_txn WHERE txid=?", (txid,)
            ).fetchone()
            if row is not None:
                if row["request_digest"] != req_digest:
                    raise DemoError(
                        f"txid {txid!r} already exists with different content; "
                        "refusing to reuse the id",
                        code="TXID_CONTENT_MISMATCH",
                    )
                _log(c, txid, "REGISTER_REPLAY", {"state": row["state"]})
                return {
                    "txid": txid,
                    "request_digest": req_digest,
                    "state": row["state"],
                    "replayed": True,
                }
            c.execute(
                "INSERT INTO coordinator_txn("
                "txid, state, request_json, request_digest, "
                "updates_digest_p1, updates_digest_p2, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (txid, OPEN, canonical_json(canonical), req_digest, ud1, ud2, stamp()),
            )
            for endpoint in COORDINATORS:
                c.execute(
                    "INSERT INTO participant_reply(txid, participant, prepare, "
                    "state, updated_at) VALUES (?,?,?,?,?)",
                    (txid, endpoint, "PENDING", "UNKNOWN", stamp()),
                )
            _log(c, txid, "REGISTER", {"request_digest": req_digest})
            return {
                "txid": txid,
                "request_digest": req_digest,
                "state": OPEN,
                "replayed": False,
            }

    def request_row(self, txid: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM coordinator_txn WHERE txid=?", (txid,)
        ).fetchone()
        if row is None:
            raise DemoError(f"unknown txid {txid!r}", code="UNKNOWN_TXID")
        return row

    def payload(self, txid: str) -> dict:
        row = self.request_row(txid)
        import json

        return {
            "txid": txid,
            "request_digest": row["request_digest"],
            "participants": json.loads(row["request_json"])["participants"],
            "updates_digest_p1": row["updates_digest_p1"],
            "updates_digest_p2": row["updates_digest_p2"],
        }

    def _record_reply(self, txid: str, participant: str, prepare: str,
                      state: str, reason=None, order=None) -> None:
        with write_transaction(self.conn) as c:
            row = c.execute(
                "SELECT * FROM participant_reply WHERE txid=? AND participant=?",
                (txid, participant),
            ).fetchone()
            # Never downgrade a terminal participant state, and never let a
            # contradiction stand: once a side is COMMITTED/ABORTED, a report
            # of the *other* terminal state is a genuine protocol violation.
            # "UNKNOWN"/"NOT_PREPARED" are absences, not outcomes, so they are
            # simply ignored for a side that is already terminal (this is what
            # makes a repeated submit of a finished request idempotent).
            old_state = row["state"]
            if old_state in (COMMITTED, ABORTED):
                if state in (COMMITTED, ABORTED) and state != old_state:
                    raise DemoError(
                        f"participant {participant} reported {state} for "
                        f"{txid!r} after already reporting {old_state}",
                        code="CONTRADICTORY_REPLY",
                    )
                # Ignore any non-terminal/non-outcome report for a side that
                # already finished; re-acknowledging the same outcome is fine.
                if state != old_state:
                    return
            c.execute(
                "UPDATE participant_reply SET prepare=CASE WHEN ?='PENDING' "
                "THEN prepare ELSE ? END, prepare_reason=COALESCE(?, prepare_reason), "
                "state=?, delivered_order=COALESCE(?, delivered_order), updated_at=? "
                "WHERE txid=? AND participant=?",
                (prepare, prepare, reason, state, order, stamp(), txid, participant),
            )
            _log(c, txid, "REPLY", {"participant": participant,
                                      "prepare": prepare, "state": state})
            # Bookkeeping only: once both sides have durably answered YES the
            # coordinator may record "ready to decide".  This state carries no
            # commit intent and changes nothing on the participants.
            current = c.execute(
                "SELECT state FROM coordinator_txn WHERE txid=?", (txid,)
            ).fetchone()
            if current["state"] == OPEN:
                yes = c.execute(
                    "SELECT COUNT(*) AS n FROM participant_reply "
                    "WHERE txid=? AND prepare='YES'", (txid,)
                ).fetchone()["n"]
                if yes == 2:
                    c.execute(
                        "UPDATE coordinator_txn SET state=? WHERE txid=?",
                        (PREPARED_BOTH, txid),
                    )
                    _log(c, txid, "STATE", {"from": OPEN, "to": PREPARED_BOTH})

    # -- decision persistence ---------------------------------------------

    def decide(self, txid: str, decision: str) -> dict:
        """Durably record COMMIT or ABORT.  This is the commit point of the
        2PC protocol: after this returns, the decision cannot change."""
        decision = decision.upper()
        if decision not in ("COMMIT", "ABORT"):
            raise DemoError(f"invalid decision {decision!r}")
        with write_transaction(self.conn) as c:
            row = c.execute(
                "SELECT * FROM coordinator_txn WHERE txid=?", (txid,)
            ).fetchone()
            if row is None:
                raise DemoError(f"unknown txid {txid!r}")
            if row["decision"] is not None:
                if row["decision"] != decision:
                    raise DemoError(
                        f"txid {txid!r} already has final decision "
                        f"{row['decision']} (order {row['decision_order']}); "
                        f"{decision} would contradict it",
                        code="CONTRADICTORY_DECISION",
                    )
                _log(c, txid, "DECIDE_REPLAY", {"decision": decision})
                return {
                    "txid": txid, "decision": decision,
                    "order": row["decision_order"],
                    "decision_digest": row["decision_digest"],
                    "replayed": True,
                }
            # Enforce the protocol's ordering: COMMIT requires both YES.
            replies = c.execute(
                "SELECT participant, prepare, state FROM participant_reply "
                "WHERE txid=?", (txid,)
            ).fetchall()
            yes = {r["participant"] for r in replies if r["prepare"] == "YES"}
            if decision == "COMMIT" and len(yes) != 2:
                raise DemoError(
                    f"cannot COMMIT {txid!r}: both participants must answer YES, "
                    f"got {sorted(yes)}",
                    code="COMMIT_NOT_ALLOWED",
                )
            order = _next_order(c)
            dmac = decision_digest(txid, row["request_digest"], decision, order)
            new_state = DECIDED_COMMIT if decision == "COMMIT" else DECIDED_ABORT
            # Committed durably by the enclosing BEGIN IMMEDIATE ... COMMIT,
            # with synchronous=FULL.  This single UPDATE is the decision.
            c.execute(
                "UPDATE coordinator_txn SET state=?, decision=?, "
                "decision_digest=?, decision_order=?, decided_at=? WHERE txid=?",
                (new_state, decision, dmac, order, stamp(), txid),
            )
            _log(c, txid, f"DECIDED_{decision}", {
                "order": order, "decision_digest": dmac,
            })
            return {
                "txid": txid, "decision": decision, "order": order,
                "decision_digest": dmac, "replayed": False,
            }

    def decision_info(self, txid: str) -> Optional[dict]:
        row = self.request_row(txid)
        if row["decision"] is None:
            return None
        return {
            "decision": row["decision"],
            "order": row["decision_order"],
            "decision_digest": row["decision_digest"],
            "decided_at": row["decided_at"],
        }

    def result(self, txid: str, participant_paths: dict[str, str]) -> dict:
        row = self.request_row(txid)
        replies = {
            r["participant"]: dict(r)
            for r in self.conn.execute(
                "SELECT * FROM participant_reply WHERE txid=?", (txid,)
            )
        }
        parts = {}
        for endpoint in COORDINATORS:
            try:
                parts[endpoint] = read_participant_status(
                    endpoint, participant_paths[endpoint], txid
                )
            except DemoError as exc:
                parts[endpoint] = {"participant": endpoint, "error": exc.message,
                                   "state": None, "reservations": []}
        result = {
            "txid": txid,
            "coordinator_state": row["state"],
            "decision": row["decision"],
            "decision_order": row["decision_order"],
            "participants": parts,
            "complete": False,
            "message": "",
        }
        if row["decision"] is None:
            result["message"] = (
                "no final decision is recorded on the coordinator; recovery "
                "will record ABORT"
            )
            return result
        states = {p: parts[p].get("state") for p in COORDINATORS}
        # Completeness is a coordinator-side fact: it becomes true only after
        # both participants have acknowledged the outcome (delivery or
        # recovery records those acknowledgements and then marks COMPLETE).
        # A participant file merely showing COMMITTED is not enough: the
        # coordinator must have seen the acknowledgement.
        done = row["state"] == COMPLETE
        result["complete"] = done
        if done:
            result["message"] = (
                f"request complete: both participants applied {row['decision']}"
            )
        else:
            result["message"] = (
                f"decision {row['decision']} is recorded but not yet applied by "
                f"all participants (states: {states}); the request is NOT complete"
            )
            result["pending_delivery"] = [
                endpoint for endpoint in COORDINATORS
                if replies.get(endpoint, {}).get("state") not in (
                    COMMITTED if row["decision"] == "COMMIT" else ABORTED,
                )
            ]
        return result

    def mark_complete(self, txid: str) -> None:
        """Record the terminal coordinator state once both sides are done."""
        with write_transaction(self.conn) as c:
            row = c.execute(
                "SELECT state FROM coordinator_txn WHERE txid=?", (txid,)
            ).fetchone()
            if row is None or row["state"] == COMPLETE:
                return
            if row["state"] not in (DECIDED_COMMIT, DECIDED_ABORT):
                return
            c.execute(
                "UPDATE coordinator_txn SET state=?, completed_at=? WHERE txid=?",
                (COMPLETE, stamp(), txid),
            )
            _log(c, txid, "COMPLETE", None)

    def describe(self, participant_paths: dict[str, str]) -> dict:
        return {
            "coordinator": self.path,
            "sqlite_config": sqlite_config(self.path),
            "transactions": [
                dict(r)
                for r in self.conn.execute(
                    "SELECT * FROM coordinator_txn ORDER BY created_at DESC LIMIT 50"
                )
            ],
            "replies": [
                dict(r)
                for r in self.conn.execute(
                    "SELECT * FROM participant_reply ORDER BY txid, participant"
                )
            ],
            "counter": [
                dict(r)
                for r in self.conn.execute("SELECT * FROM coordinator_counter")
            ],
            "log": [
                dict(r)
                for r in self.conn.execute(
                    "SELECT id, at, txid, event, detail FROM coordinator_log "
                    "ORDER BY id DESC LIMIT 50"
                )
            ],
        }


def init_coordinator(db_path: str) -> dict:
    conn = initialize_database(db_path)
    try:
        conn.executescript(COORDINATOR_SCHEMA)
        with write_transaction(conn) as c:
            _log(c, None, "INIT", {})
    finally:
        conn.close()
    return {"coordinator": os.path.abspath(db_path),
            "sqlite_config": sqlite_config(db_path)}


def read_coordinator_txns(db_path: str, limit: int = 100) -> list[dict]:
    conn = open_database(db_path, read_only=True)
    try:
        return [
            dict(r) for r in conn.execute(
                "SELECT * FROM coordinator_txn ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
        ]
    finally:
        conn.close()
