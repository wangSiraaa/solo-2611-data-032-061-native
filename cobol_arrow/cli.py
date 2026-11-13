"""Command line interface: compile / convert / explain-record."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .arrowio import convert_file
from .compiler import compile_copybook
from .explain import UserError, explain_layout, explain_record, layout_json
from .model import CopybookError, Encoding, EncodingError


def _load_layout(copybook: str):
    try:
        text = Path(copybook).read_text(encoding="utf-8")
    except OSError as exc:
        raise UserError(f"cannot read copybook {copybook}: {exc}") from exc
    return compile_copybook(text, copybook)


def _load_encoding(path: str | None) -> Encoding:
    if path is None:
        return Encoding.default()
    return Encoding.load(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cobol_arrow",
        description="Restricted COBOL copybook compiler and EBCDIC/packed-decimal "
                    "to Arrow/Parquet converter.")
    sub = parser.add_subparsers(dest="command", required=True)

    comp = sub.add_parser("compile", help="compile a copybook and print the explain-layout")
    comp.add_argument("--copybook", required=True, help="path to the .cpy copybook")
    comp.add_argument("--json", action="store_true",
                      help="emit the inspectable layout (field tree, lengths, count "
                           "dependency, paths) as JSON instead of text")

    conv = sub.add_parser("convert", help="convert a binary file to Parquet + reject Parquet")
    conv.add_argument("--copybook", required=True)
    conv.add_argument("--input", required=True, help="binary records file")
    conv.add_argument("--encoding", help="encoding.json declaration "
                                         "(default: built-in cp037/4-byte big-endian prefix)")
    conv.add_argument("--out", required=True, help="output Parquet for good records")
    conv.add_argument("--rejects", help="output Parquet for rejected records "
                                        "(default: <out with .rejects.parquet suffix>)")

    exp = sub.add_parser("explain-record",
                         help="show the raw bytes and decoding of one field of one record")
    exp.add_argument("--copybook", required=True)
    exp.add_argument("--input", required=True, help="binary records file")
    exp.add_argument("--encoding", help="encoding.json declaration")
    exp.add_argument("--record", required=True, type=int,
                     help="0-based record number (matches the reject table)")
    exp.add_argument("--field", required=True,
                     help="field path, e.g. ACCOUNT-ID or DETAIL-LINES[2].PRICE "
                          "(subscripts are 1-based)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "compile":
            layout = _load_layout(args.copybook)
            print(layout_json(layout) if args.json else explain_layout(layout))
        elif args.command == "convert":
            layout = _load_layout(args.copybook)
            enc = _load_encoding(args.encoding)
            rejects = args.rejects
            if rejects is None:
                out = Path(args.out)
                rejects = str(out.with_suffix("")) + ".rejects.parquet"
            ok, bad = convert_file(layout, enc, args.input, args.out, rejects)
            print(f"converted {ok} record(s), rejected {bad} -> {args.out} "
                  f"(rejects: {rejects})")
        elif args.command == "explain-record":
            layout = _load_layout(args.copybook)
            enc = _load_encoding(args.encoding)
            print(explain_record(layout, enc, args.input, args.record, args.field))
    except CopybookError as exc:
        print(f"copybook error: {exc}", file=sys.stderr)
        return 2
    except (EncodingError, UserError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
