"""Internal participant worker process.

Every participant operation that touches durable state can be run in its own
OS process, which makes the "independent SQLite file" claim visible: the
coordinator only ever receives a JSON line, and the participant file is the
source of truth.

The coordinator invokes this module as a subprocess.  It is also runnable by
hand for inspection; see README.  It is intentionally *not* one of the public
top-level commands, but it is fully documented so a reader can hand-run it.
"""
from __future__ import annotations

import argparse
import sys
from typing import Optional

from .participant import Participant
from .request import ABSENT
from .util import (
    CrashInjected,
    DemoError,
    crash_if,
    emit,
    parse_json,
)


def _read_stdin_json() -> dict:
    text = sys.stdin.read()
    if not text.strip():
        return {}
    value = parse_json(text)
    if not isinstance(value, dict):
        raise DemoError("worker input must be a JSON object")
    return value


def _crash_point_for(name: str, suffix: str) -> str:
    return f"{name}-{suffix}"


def cmd_prepare(args) -> dict:
    payload = _read_stdin_json()
    updates = payload.get("updates")
    if not isinstance(updates, list):
        raise DemoError("worker input must contain an updates array")
    request_digest = payload.get("request_digest")
    if not isinstance(request_digest, str) or not request_digest:
        raise DemoError("worker input must contain request_digest")
    with Participant(args.name, args.db) as p:
        reply = p.prepare(args.txid, updates, request_digest)
        # Simulate a participant dying right after its durable prepare and
        # before its YES reaches the coordinator.
        if reply.get("decision") == "YES":
            crash_if(_crash_point_for(args.name, "after-prepare"))
        return reply


def cmd_apply(args) -> dict:
    with Participant(args.name, args.db) as p:
        reply = p.apply_decision(
            args.txid,
            args.request_digest,
            args.decision,
            args.order,
            args.decision_digest,
            crash_before=args.crash_before,
            crash_after_commit=args.crash_after_commit,
        )
        if reply.get("state") == "COMMITTED":
            crash_if(_crash_point_for(args.name, "after-commit"))
        if reply.get("state") == "ABORTED":
            crash_if(_crash_point_for(args.name, "after-abort"))
        return reply


def cmd_status(args) -> dict:
    with Participant(args.name, args.db) as p:
        return p.status(args.txid)


def cmd_describe(args) -> dict:
    with Participant(args.name, args.db) as p:
        return p.describe()


def cmd_ordinary_set(args) -> dict:
    with Participant(args.name, args.db) as p:
        if args.expected == ABSENT:
            if p.get_value(args.key) is not None:
                raise DemoError(
                    f"key {args.key!r} already exists; expected ABSENT",
                    code="CONDITION_FAILED",
                )
        else:
            actual = p.get_value(args.key)
            if actual != args.expected:
                raise DemoError(
                    f"key {args.key!r}: expected {args.expected} but found {actual}",
                    code="CONDITION_FAILED",
                )
        p.ordinary_set(args.key, args.value)
        return {
            "participant": args.name,
            "key": args.key,
            "value": args.value,
            "applied": True,
        }


def cmd_seed(args) -> dict:
    payload = _read_stdin_json()
    seed = payload.get("seed")
    if not isinstance(seed, dict):
        raise DemoError("worker input must contain a seed object")
    with Participant(args.name, args.db) as p:
        rows = []
        for key, value in seed.items():
            p.ordinary_set(key, int(value))
            rows.append({"key": key, "value": int(value)})
        return {"participant": args.name, "seeded": rows}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="twopc.worker", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    def add_common(sp):
        sp.add_argument("--db", required=True)
        sp.add_argument("--name", required=True)

    sp = sub.add_parser("prepare", help="validate expectations and stage a YES")
    add_common(sp)
    sp.add_argument("--txid", required=True)
    sp.set_defaults(func=cmd_prepare)

    sp = sub.add_parser("apply", help="apply a verified COMMIT or ABORT")
    add_common(sp)
    sp.add_argument("--txid", required=True)
    sp.add_argument("--request-digest", required=True)
    sp.add_argument("--decision", required=True, choices=["COMMIT", "ABORT", "commit", "abort"])
    sp.add_argument("--order", required=True, type=int)
    sp.add_argument("--decision-digest", required=True)
    sp.add_argument("--crash-before", action="store_true")
    sp.add_argument("--crash-after-commit", action="store_true")
    sp.set_defaults(func=cmd_apply)

    sp = sub.add_parser("status", help="read-only transaction state")
    add_common(sp)
    sp.add_argument("--txid", required=True)
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("describe", help="dump the participant file at rest")
    add_common(sp)
    sp.set_defaults(func=cmd_describe)

    sp = sub.add_parser("ordinary-set", help="non-2PC write, blocked by claims")
    add_common(sp)
    sp.add_argument("--key", required=True)
    sp.add_argument("--value", type=int, required=True)
    expected = sp.add_mutually_exclusive_group(required=True)
    expected.add_argument("--expected", type=int)
    expected.add_argument("--expected-absent", dest="expected_absent", action="store_true")
    sp.set_defaults(func=cmd_ordinary_set)

    sp = sub.add_parser("seed", help="seed business rows from stdin")
    add_common(sp)
    sp.set_defaults(func=cmd_seed)
    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "expected_absent", False):
        args.expected = ABSENT
    try:
        result = args.func(args)
    except CrashInjected:
        raise
    except DemoError as exc:
        emit({"ok": False, "error": exc.message, "code": exc.code})
        return 2
    except Exception as exc:  # pragma: no cover - unexpected
        emit({"ok": False, "error": str(exc), "code": "UNEXPECTED"})
        return 3
    emit({"ok": True, **result})
    return 0


if __name__ == "__main__":
    sys.exit(main())
