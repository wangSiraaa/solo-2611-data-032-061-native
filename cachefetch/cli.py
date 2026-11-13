"""Command line interface: serve, fetch, inspect, generate, verify."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .client import FetchError, parse_header_option, fetch
from .generate import SCENARIO_DIR, write_scenarios
from .store import CacheStore


def _emit(value) -> None:
    sys.stdout.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    sys.stdout.flush()


def _fail(kind: str, message: str, status: int) -> int:
    _emit({"ok": False, "error": {"kind": kind, "message": message}})
    return status


def _parse_headers(args) -> dict[str, str]:
    headers: dict[str, str] = {}
    if args.header_json:
        try:
            parsed = json.loads(args.header_json)
        except json.JSONDecodeError as exc:
            raise ValueError(f"--header-json is not valid JSON: {exc}") from exc
        if not isinstance(parsed, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in parsed.items()
        ):
            raise ValueError("--header-json must be an object of string values")
        for name, value in parsed.items():
            headers[name] = value.strip(" \t")
    for item in args.header or []:
        name, value = parse_header_option(item)
        headers[name] = value
    return headers


def cmd_serve(args) -> int:
    from .server import serve_forever

    serve_forever(args.scenario, args.host, args.port)
    return 0


def cmd_fetch(args) -> int:
    try:
        headers = _parse_headers(args)
    except ValueError as exc:
        return _fail("usage_error", str(exc), 2)
    if args.time < 0:
        return _fail("usage_error", "--time must be a non-negative integer", 2)
    try:
        outcome = fetch(
            url=args.url,
            db_path=args.db,
            now=args.time,
            headers=headers,
            timeout=args.timeout,
        )
    except FetchError as exc:
        return _fail(exc.kind, str(exc), 1)
    payload = outcome.as_json()
    payload["ok"] = True
    if args.show_body:
        # Keep machine-readable output: the JSON line carries everything,
        # then the raw body bytes follow on a separate stream.
        _emit(payload)
        sys.stdout.buffer.write(outcome.body)
        sys.stdout.buffer.flush()
        return 0
    _emit(payload)
    return 0


def cmd_inspect(args) -> int:
    with CacheStore(args.db) as store:
        if args.all:
            urls = store.all_urls()
        elif args.url:
            urls = [args.url]
        else:
            return _fail("usage_error", "inspect needs --url or --all", 2)
        payload = {
            "ok": True,
            "db": os.path.abspath(args.db),
            "urls": [store.inspect_url(url) for url in urls],
        }
    _emit(payload)
    return 0


def cmd_generate(args) -> int:
    output = Path(args.output) if args.output else SCENARIO_DIR
    written = write_scenarios(output)
    _emit(
        {
            "ok": True,
            "scenario_dir": str(output),
            "files": [str(path) for path in written],
        }
    )
    return 0


def cmd_verify(args) -> int:
    from .selftest import run_verification

    report = run_verification(
        output_dir=args.output,
        keep_artifacts=args.keep_artifacts,
    )
    _emit(report)
    return 0 if report["ok"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cachefetch",
        description=(
            "Local HTTP simulation and Vary-aware GET cache "
            "(max-age/no-store, strong ETag revalidation, test clock)."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the simulated scenario server")
    serve.add_argument("--scenario", required=True, help="scenario JSON path")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=0, help="0 picks a free port")
    serve.set_defaults(func=cmd_serve)

    fetch_p = sub.add_parser("fetch", help="GET a URL through the local cache")
    fetch_p.add_argument("--db", required=True, help="SQLite cache path")
    fetch_p.add_argument("--url", required=True)
    fetch_p.add_argument("--time", type=int, required=True, help="test clock (integer seconds)")
    fetch_p.add_argument("--header", action="append", metavar="'Name: Value'")
    fetch_p.add_argument("--header-json", metavar="JSON_OBJECT")
    fetch_p.add_argument("--timeout", type=float, default=10.0)
    fetch_p.add_argument(
        "--show-body", action="store_true", help="append the raw response body"
    )
    fetch_p.set_defaults(func=cmd_fetch)

    inspect_p = sub.add_parser("inspect", help="show stored state and variants")
    inspect_p.add_argument("--db", required=True)
    inspect_p.add_argument("--url")
    inspect_p.add_argument("--all", action="store_true")
    inspect_p.set_defaults(func=cmd_inspect)

    gen_p = sub.add_parser("generate", help="regenerate bundled scenario files")
    gen_p.add_argument("--output", help="directory (default: ./scenarios)")
    gen_p.set_defaults(func=cmd_generate)

    verify_p = sub.add_parser("verify", help="run the automated verification suite")
    verify_p.add_argument("--output", help="artifact directory (default: .runtime/verify)")
    verify_p.add_argument("--keep-artifacts", action="store_true")
    verify_p.set_defaults(func=cmd_verify)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
