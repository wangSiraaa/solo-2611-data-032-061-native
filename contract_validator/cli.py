"""Command-line entry points: check-schema, validate, generate, verify."""

from __future__ import annotations

import argparse
import os
import sys
import tempfile

from . import contract_check
from .report_json import canonical_json


def _emit_error(payload: dict) -> None:
    sys.stderr.write(canonical_json(payload) + "\n")


def cmd_check_schema(args) -> int:
    try:
        contract = contract_check.load_contract_file(args.schema)
    except contract_check.ContractError as exc:
        _emit_error(exc.to_dict())
        return 2
    summary = {
        "ok": True,
        "code": "ok",
        "nodes": contract.node_count,
        "root": contract.root,
        "defs": sorted(contract.defs),
    }
    sys.stdout.write(canonical_json(summary) + "\n")
    return 0


def cmd_validate(args) -> int:
    from .instance_check import validate_stream, report_line

    # The contract is checked (and rejected) *before* the input is opened, so
    # a malformed contract never depends on data being present.
    try:
        contract = contract_check.load_contract_file(args.schema)
    except contract_check.ContractError as exc:
        _emit_error(exc.to_dict())
        return 2

    try:
        with open(args.input, "rb") as handle:
            results, summary = validate_stream(
                contract, handle, use_cache=not args.no_cache
            )
    except OSError as exc:
        _emit_error({"ok": False, "code": "input-unreadable", "message": str(exc)})
        return 2

    text = "".join(report + "\n" for _ok, report in (report_line(r) for r in results))
    try:
        _atomic_write(args.output, text.encode("utf-8"))
    except OSError as exc:
        _emit_error(
            {"ok": False, "code": "output-unwritable", "message": str(exc)}
        )
        return 2

    summary["ok"] = True
    try:
        if args.summary:
            _atomic_write(
                args.summary, (canonical_json(summary) + "\n").encode("utf-8")
            )
        else:
            sys.stderr.write(canonical_json(summary) + "\n")
    except OSError as exc:
        _emit_error(
            {"ok": False, "code": "summary-unwritable", "message": str(exc)}
        )
        return 2
    return 0 if summary["rejected"] == 0 else 1


def _atomic_write(path: str, data: bytes) -> None:
    """Write to a sibling temp file and atomically replace the destination.

    The temporary file is created in the destination's directory and ``fsync``-ed
    before ``os.replace``; if anything fails the destination is untouched, so a
    failed write never replaces the previous good version.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def cmd_generate(args) -> int:
    from .generate import generate_from_cli

    return generate_from_cli(args)


def cmd_verify(args) -> int:
    from .verify import run_verify

    return run_verify(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="contract_validator",
        description="Local JSON Schema-subset contract validator (stdlib only).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_check = sub.add_parser("check-schema", help="statically check a contract")
    p_check.add_argument("schema", help="path to the contract JSON file")
    p_check.set_defaults(func=cmd_check_schema)

    p_validate = sub.add_parser("validate", help="validate a JSONL file")
    p_validate.add_argument("schema", help="path to the contract JSON file")
    p_validate.add_argument("input", help="path to the JSONL input file")
    p_validate.add_argument("output", help="path for the complete report/result file")
    p_validate.add_argument("--summary", help="optional path for a JSON summary")
    p_validate.add_argument(
        "--no-cache",
        action="store_true",
        help="disable (node,data) memoisation (for comparison only)",
    )
    p_validate.set_defaults(func=cmd_validate)

    p_gen = sub.add_parser("generate", help="synthesise contract + JSONL corpus")
    p_gen.add_argument("--out-dir", default=".runtime/corpus")
    p_gen.set_defaults(func=cmd_generate)

    p_verify = sub.add_parser("verify", help="run the conformance suite")
    p_verify.add_argument("--out-dir", default=".runtime/verify")
    p_verify.set_defaults(func=cmd_verify)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:  # pragma: no cover
        return 130


__all__ = ["build_parser", "main"]
