"""Command-line interface for the two-phase-commit demo.

Public commands (see README for examples):

  init                create the coordinator and two participant SQLite files
  generate            write deterministic local request JSON files
  submit              register + prepare + decide + deliver one request
  participant-status  read one participant's durable state for a txid
  recover             apply the recovery rules to unfinished transactions
  result              report whether a request is complete
  verify              independently verify values, claims and decisions
  journal             human-readable coordinator/participant log
  locks               explain any outstanding prepared claims
  ordinary-set        non-2PC write, to show claims block outsiders
  inspect             dump all three files at rest

Every command prints one JSON object on stdout.  Diagnostics go to stderr.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from typing import Optional

from .coordinator import Coordinator, init_coordinator
from .driver import Driver
from .layout import layout, participant_paths
from .participant import (
    Participant,
    init_participant,
    read_participant_status,
)
from .util import DemoError, emit, open_database, parse_json
from .verify import verify, verify_final_values, _coordinator_row
from .workload import make_workload

DEFAULT_BASE = os.path.join(".runtime", "demo")
DEFAULT_SEED_P1 = {"a": 1, "b": 2, "c": 3,
                   "checking": 1000, "savings": 500, "reserve": 250}
DEFAULT_SEED_P2 = {"x": 1, "y": 2,
                   "partner-checking": 800, "partner-savings": 300}

FAULT_TOKENS = (
    "after-prepare:p1", "after-prepare:p2",
    "before-decision", "after-decision", "after-first-delivery",
    "after-apply:p1", "after-apply:p2",
)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _lay(args) -> dict[str, str]:
    return layout(args.base if getattr(args, "base", None) else DEFAULT_BASE)


def _open(args) -> Driver:
    lay = _lay(args)
    for key in ("coordinator", "p1", "p2"):
        if not os.path.exists(lay[key]):
            raise DemoError(
                f"missing {lay[key]}; run `python -m twopc init` first",
                code="NOT_INITIALIZED",
            )
    coord = Coordinator(lay["coordinator"])
    return Driver(coord, participant_paths(lay), lay["traces"])


def _faults(tokens: list[str]) -> dict[str, object]:
    """Translate CLI fault tokens into driver/env crash points."""
    faults = set(tokens)
    unknown = faults - set(FAULT_TOKENS)
    if unknown:
        raise DemoError(
            f"unknown fault point(s): {sorted(unknown)}; allowed: {list(FAULT_TOKENS)}",
            code="BAD_FAULT",
        )
    prepare_crashes = {}
    deliver_crashes = {}
    env_coordinator = []
    for token in sorted(faults):
        if token.startswith("after-prepare:"):
            name = token.split(":", 1)[1]
            prepare_crashes[name] = f"{name}-after-prepare"
        elif token.startswith("after-apply:"):
            name = token.split(":", 1)[1]
            deliver_crashes[name] = "after-apply"
        elif token == "before-decision":
            env_coordinator.append("coordinator-after-prepare")
        elif token == "after-decision":
            env_coordinator.append("coordinator-after-decision")
        elif token == "after-first-delivery":
            env_coordinator.append("coordinator-after-first-delivery")
    return {
        "prepare": prepare_crashes or None,
        "deliver": deliver_crashes or None,
        "env_coordinator": ",".join(env_coordinator),
        "raw": sorted(faults),
    }


def _set_env(value: str) -> None:
    if value:
        os.environ["TWOPC_CRASH_AT"] = value
    else:
        os.environ.pop("TWOPC_CRASH_AT", None)


def _read_request(args) -> dict:
    if args.request_file:
        with open(args.request_file, encoding="utf-8") as fh:
            text = fh.read()
    elif args.request_json:
        text = args.request_json
    elif args.stdin:
        text = sys.stdin.read()
    else:
        raise DemoError(
            "submit needs --request-file, --request-json or --stdin",
            code="BAD_REQUEST_SOURCE",
        )
    value = parse_json(text)
    if not isinstance(value, dict):
        raise DemoError("request must be a JSON object")
    return value


def _snapshot_all(lay: dict[str, str]) -> dict[str, dict[str, int]]:
    out = {}
    for name in ("p1", "p2"):
        if os.path.exists(lay[name]):
            with Participant(name, lay[name]) as p:
                out[name] = p.snapshot()
        else:
            out[name] = {}
    return out


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_init(args) -> dict:
    lay = _lay(args)
    os.makedirs(lay["base"], exist_ok=True)
    existing = [lay[k] for k in ("coordinator", "p1", "p2") if os.path.exists(lay[k])]
    if existing and not args.force:
        raise DemoError(
            "already initialized: " + ", ".join(existing)
            + " (use --force to recreate them)",
            code="ALREADY_INITIALIZED",
        )
    if args.force and existing:
        for key in ("coordinator", "p1", "p2"):
            for suffix in ("", "-wal", "-shm"):
                path = lay[key] + suffix
                if os.path.exists(path):
                    os.remove(path)
        for sub in ("traces", "requests"):
            path = os.path.join(lay["base"], sub)
            if os.path.isdir(path):
                shutil.rmtree(path)
    os.makedirs(lay["traces"], exist_ok=True)
    os.makedirs(os.path.join(lay["base"], "requests"), exist_ok=True)

    coord = init_coordinator(lay["coordinator"])
    p1 = init_participant("p1", lay["p1"],
                          None if args.no_seed else DEFAULT_SEED_P1)
    p2 = init_participant("p2", lay["p2"],
                          None if args.no_seed else DEFAULT_SEED_P2)
    return {
        "initialized": lay["base"],
        "coordinator": coord,
        "participants": [p1, p2],
        "note": "three independent SQLite files; no ATTACH is used anywhere",
    }


def cmd_generate(args) -> dict:
    lay = _lay(args)
    os.makedirs(os.path.join(lay["base"], "requests"), exist_ok=True)
    snap = _snapshot_all(lay)
    requests = make_workload(args.kind, args.seed, snap["p1"], snap["p2"],
                             count=args.count)
    written = []
    for req in requests:
        path = os.path.join(lay["base"], "requests", f"{req['txid']}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(req, fh, indent=2, sort_keys=True)
            fh.write("\n")
        written.append({"txid": req["txid"], "path": path})
    return {"kind": args.kind, "seed": args.seed, "requests": written}


def cmd_submit(args) -> dict:
    lay = _lay(args)
    req = _read_request(args)
    faults = _faults(args.fault or [])
    _set_env(faults["env_coordinator"])
    driver = _open(args)
    try:
        env_points = (faults["env_coordinator"] or "").split(",")
        out = driver.submit(
            req,
            crash_at_during={
                **{f"prepare.{n}": v for n, v in (faults["prepare"] or {}).items()},
                **{f"deliver.{n}": v for n, v in (faults["deliver"] or {}).items()},
            } or None,
            crash_after_decision=("coordinator-after-decision"
                                  if "coordinator-after-decision" in env_points
                                  else None),
        )
    finally:
        driver.coord.close()
        _set_env("")
    return {
        "txid": out["register"]["txid"],
        "register": out["register"],
        "prepare": out["prepare"],
        "decision": out["decision"],
        "delivery_complete": out["delivery"].get("complete"),
        "delivery": out["delivery"],
        "result": out["result"],
        "faults_applied": faults["raw"],
    }


def cmd_participant_status(args) -> dict:
    lay = _lay(args)
    names = ["p1", "p2"] if args.participant == "both" else [args.participant]
    views = []
    for name in names:
        views.append(read_participant_status(name, lay[name], args.txid))
    # Explain outstanding claims rather than merely listing them.
    coord = _coordinator_row(lay["coordinator"], args.txid)
    for view in views:
        if view.get("reservations"):
            view["reservation_explanation"] = [
                _explain_reservation(lay, args.txid, view["participant"], key, coord)
                for key in view["reservations"]
            ]
    return {"txid": args.txid, "participants": views}


def _explain_reservation(lay, txid, participant, key, coord) -> dict:
    return {
        "key": key,
        "held_by": txid,
        "participant": participant,
        "coordinator_state": None if coord is None else coord["state"],
        "coordinator_decision": None if coord is None else coord["decision"],
        "why": (
            "This key is logically owned by a transaction that reached PREPARED. "
            "It stays unavailable to every other write until an authorised "
            "COMMIT or ABORT decision is applied.  It must not be cleared by "
            "deleting the reservation row."
        ),
        "what_to_do": (
            "Run `python -m twopc recover --txid %s` (or the same command "
            "without --txid).  Recovery reads the coordinator's durable "
            "decision: no decision => record ABORT; COMMIT => keep driving "
            "COMMIT.  The claim is released only when the outcome is applied "
            "in the same SQLite transaction as the business change." % txid
        ),
    }


def cmd_recover(args) -> dict:
    lay = _lay(args)
    faults = _faults(args.fault or [])
    _set_env(faults["env_coordinator"])
    driver = _open(args)
    before = _snapshot_all(lay) if args.report_impact else None
    try:
        report = driver.recover(args.txid)
    finally:
        driver.coord.close()
        _set_env("")
    if before is not None:
        after = _snapshot_all(lay)
        report["business_changed"] = {
            "p1": before["p1"] != after["p1"],
            "p2": before["p2"] != after["p2"],
            "before": before,
            "after": after,
        }
    return report


def cmd_result(args) -> dict:
    lay = _lay(args)
    driver = _open(args)
    try:
        result = driver.coord.result(args.txid, participant_paths(lay))
    finally:
        driver.coord.close()
    return result


def cmd_verify(args) -> dict:
    lay = _lay(args)
    request = None
    if args.request_file:
        with open(args.request_file, encoding="utf-8") as fh:
            request = parse_json(fh.read())
    elif args.request_json:
        request = parse_json(args.request_json)
    elif args.use_recorded_request:
        coord = _coordinator_row(lay["coordinator"], args.txid)
        if coord is not None:
            request = parse_json(coord["request_json"])
        else:
            raise DemoError(f"unknown txid {args.txid!r}", code="UNKNOWN_TXID")
    basic = verify(lay, args.txid)
    strict = None
    if request is not None:
        strict = verify_final_values(lay, args.txid, request)
    out = {"txid": args.txid, "verify": basic, "final_values": strict}
    if basic.get("failures") or (strict and strict.get("failures")):
        # Machine-readable failure: exit non-zero so scripts can gate on it.
        out["ok"] = False
        out["error"] = "verification failed"
        out["code"] = "VERIFICATION_FAILED"
    return out


def cmd_journal(args) -> dict:
    lay = _lay(args)
    coord_conn = Coordinator(lay["coordinator"])
    try:
        coord_rows = [
            dict(r) for r in coord_conn.conn.execute(
                "SELECT id, at, txid, event, detail FROM coordinator_log "
                + ("WHERE txid=? " if args.txid else "")
                + "ORDER BY id",
                (args.txid,) if args.txid else (),
            )
        ]
        replies = [
            dict(r) for r in coord_conn.conn.execute(
                "SELECT * FROM participant_reply "
                + ("WHERE txid=? " if args.txid else "")
                + "ORDER BY txid, participant",
                (args.txid,) if args.txid else (),
            )
        ]
    finally:
        coord_conn.close()
    parts = {}
    for name in ("p1", "p2"):
        with Participant(name, lay[name]) as p:
            parts[name] = [
                dict(r) for r in p.conn.execute(
                    "SELECT id, at, txid, event, detail FROM participant_log "
                    + ("WHERE txid=? " if args.txid else "")
                    + "ORDER BY id",
                    (args.txid,) if args.txid else (),
                )
            ]
    return {
        "coordinator_log": coord_rows,
        "coordinator_replies": replies,
        "participant_logs": parts,
        "raw_sql_hint": (
            "sqlite3 has no CLI here; inspect with: python -m twopc inspect, "
            "or open the files with any SQLite client.  Rows are plain text."
        ),
    }


def cmd_locks(args) -> dict:
    lay = _lay(args)
    out = {"reservations": [], "unresolved": 0}
    for name in ("p1", "p2"):
        with Participant(name, lay[name]) as p:
            rows = [
                dict(r) for r in p.conn.execute(
                    "SELECT key, txid FROM reservation ORDER BY key"
                )
            ]
        for row in rows:
            coord = _coordinator_row(lay["coordinator"], row["txid"])
            owner = p_status = None
            with Participant(name, lay[name]) as p2:
                row2 = p2.txn_row(row["txid"])
                p_status = None if row2 is None else row2["state"]
            entry = {
                "participant": name,
                "key": row["key"],
                "txid": row["txid"],
                "participant_state": p_status,
                "coordinator_state": None if coord is None else coord["state"],
                "coordinator_decision": None if coord is None else coord["decision"],
                "explanation": _explain_reservation(lay, row["txid"], name,
                                                    row["key"], coord),
            }
            out["reservations"].append(entry)
            if p_status == "PREPARED":
                out["unresolved"] += 1
    out["summary"] = (
        "No reservation may be deleted to 'repair' a stuck transaction. "
        "Run recover; it resolves claims only by applying a verified decision."
    )
    return out


def cmd_ordinary_set(args) -> dict:
    lay = _lay(args)
    with Participant(args.participant, lay[args.participant]) as p:
        expected = args.expected
        actual = p.get_value(args.key)
        if expected == "ABSENT":
            if actual is not None:
                raise DemoError(
                    f"key {args.key!r} exists with {actual}; expected ABSENT",
                    code="CONDITION_FAILED",
                )
        elif expected is not None:
            if actual != expected:
                raise DemoError(
                    f"key {args.key!r}: expected {expected}, found {actual}",
                    code="CONDITION_FAILED",
                )
        p.ordinary_set(args.key, args.value)
        return {"participant": args.participant, "key": args.key,
                "value": args.value, "applied": True}


def cmd_inspect(args) -> dict:
    lay = _lay(args)
    out = {}
    coord = Coordinator(lay["coordinator"])
    try:
        out["coordinator"] = coord.describe(participant_paths(lay))
    finally:
        coord.close()
    for name in ("p1", "p2"):
        with Participant(name, lay[name]) as p:
            out[name] = p.describe()
    return out


def cmd_sql(args) -> dict:
    """Run a read-only query straight against one of the three files.

    This is the hand-inspection escape hatch for environments without the
    `sqlite3` shell: the same tables are visible to any SQLite client.  Writes
    are refused by PRAGMA query_only.
    """
    lay = _lay(args)
    paths = {
        "coordinator": lay["coordinator"],
        "p1": lay["p1"],
        "p2": lay["p2"],
    }
    if args.target not in paths:
        raise DemoError(f"unknown target {args.target!r}", code="BAD_TARGET")
    sql = args.query.strip()
    if not sql.lower().startswith(("select", "pragma", "with", "explain")):
        raise DemoError(
            "only read-only statements are allowed here; use the protocol "
            "commands to change state",
            code="SQL_READ_ONLY",
        )
    conn = open_database(paths[args.target], read_only=True)
    try:
        rows = [dict(r) for r in conn.execute(sql)]
    finally:
        conn.close()
    return {"target": args.target,
            "database": paths[args.target],
            "rows": rows,
            "row_count": len(rows)}


def cmd_config(args) -> dict:
    """Show the effective SQLite settings of all three files."""
    lay = _lay(args)
    from .util import sqlite_config
    return {
        "files": {
            "coordinator": sqlite_config(lay["coordinator"]),
            "p1": sqlite_config(lay["p1"]),
            "p2": sqlite_config(lay["p2"]),
        },
        "explanation": {
            "journal_mode": "WAL: readers see a consistent snapshot while a "
                            "writer commits; the file keeps this mode.",
            "synchronous": "FULL: every commit is fsynced, which makes a saved "
                           "decision genuinely durable before it is delivered.",
            "busy_timeout_ms": "Wait for a writer lock instead of failing "
                               "immediately; independent processes share files.",
            "transaction": "BEGIN IMMEDIATE takes the write lock up front, so "
                           "read-check-write blocks cannot race.",
        },
    }


# --------------------------------------------------------------------------
# argument parsing
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m twopc", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base", default=DEFAULT_BASE,
                   help=f"demo directory (default {DEFAULT_BASE})")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("init", help="create the three independent SQLite files")
    sp.add_argument("--force", action="store_true",
                    help="remove existing demo files under --base first")
    sp.add_argument("--no-seed", action="store_true",
                    help="create empty business tables")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("generate", help="write deterministic request JSON")
    sp.add_argument("--kind", choices=["basic", "coverage", "realistic"],
                    default="basic")
    sp.add_argument("--seed", type=int, default=7)
    sp.add_argument("--count", type=int, default=1)
    sp.set_defaults(func=cmd_generate)

    sp = sub.add_parser("submit", help="run one two-phase commit request")
    src = sp.add_mutually_exclusive_group(required=True)
    src.add_argument("--request-file")
    src.add_argument("--request-json")
    src.add_argument("--stdin", action="store_true")
    sp.add_argument("--fault", action="append", choices=list(FAULT_TOKENS),
                    help="inject a fault point (repeatable)")
    sp.set_defaults(func=cmd_submit)

    sp = sub.add_parser("participant-status", help="durable state of one/both participants")
    sp.add_argument("txid")
    sp.add_argument("--participant", choices=["p1", "p2", "both"], default="both")
    sp.set_defaults(func=cmd_participant_status)

    sp = sub.add_parser("recover", help="apply recovery rules")
    sp.add_argument("--txid", default=None)
    sp.add_argument("--fault", action="append", choices=list(FAULT_TOKENS),
                    help="inject a fault point while recovering (repeatable)")
    sp.add_argument("--report-impact", action="store_true",
                    help="include business snapshots before/after")
    sp.set_defaults(func=cmd_recover)

    sp = sub.add_parser("result", help="report completeness of a request")
    sp.add_argument("txid")
    sp.set_defaults(func=cmd_result)

    sp = sub.add_parser("verify", help="independent safety verification")
    sp.add_argument("txid")
    src = sp.add_mutually_exclusive_group()
    src.add_argument("--request-file", help="also check exact old/new values")
    src.add_argument("--request-json", help="request JSON inline")
    src.add_argument("--use-recorded-request", action="store_true",
                     help="use the request the coordinator recorded")
    sp.set_defaults(func=cmd_verify)

    sp = sub.add_parser("journal", help="human-readable transaction logs")
    sp.add_argument("--txid", default=None)
    sp.set_defaults(func=cmd_journal)

    sp = sub.add_parser("locks", help="list and explain outstanding claims")
    sp.set_defaults(func=cmd_locks)

    sp = sub.add_parser("ordinary-set", help="non-2PC write blocked by claims")
    sp.add_argument("--participant", required=True, choices=["p1", "p2"])
    sp.add_argument("--key", required=True)
    sp.add_argument("--value", required=True, type=int)
    sp.add_argument("--expected", default=None,
                    help="expected old value or ABSENT")
    sp.set_defaults(func=cmd_ordinary_set)

    sp = sub.add_parser("inspect", help="dump all three files at rest")
    sp.set_defaults(func=cmd_inspect)

    sp = sub.add_parser("sql", help="read-only SQL against one file")
    sp.add_argument("--target", required=True, choices=["coordinator", "p1", "p2"])
    sp.add_argument("--query", required=True)
    sp.set_defaults(func=cmd_sql)

    sp = sub.add_parser("config", help="show SQLite synchronization settings")
    sp.set_defaults(func=cmd_config)
    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = args.func(args)
    except DemoError as exc:
        emit({"ok": False, "error": exc.message, "code": exc.code})
        return 2
    emit({"ok": True, **result})
    return 0


if __name__ == "__main__":
    sys.exit(main())
