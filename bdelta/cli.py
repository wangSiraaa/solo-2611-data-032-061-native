"""bdelta 命令行入口。

用法：
    python -m bdelta signature --old FILE --sig FILE [--block-size B]
    python -m bdelta delta     --sig FILE --new FILE --patch FILE
    python -m bdelta apply     --old FILE --patch FILE --out FILE
    python -m bdelta verify    --old FILE --patch FILE --new FILE
    python -m bdelta generate  {corpus|large} --dir DIR [--block-size B]

所有统计以单行 JSON 打印到 stdout；失败时错误信息走 stderr 并返回非零码。
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import sys

from . import DEFAULT_BLOCK_SIZE, MAX_BLOCK_SIZE, MIN_BLOCK_SIZE
from .apply import ApplyError, apply_patch, verify_patch
from .delta import make_delta
from .frozen import InputChangedError
from .generator import LARGE_SIZE, make_corpus, make_large_pair
from .patch import PatchFormatError
from .signature import build_signature, validate_block_size


def peak_rss_mib() -> float:
    # Linux ru_maxrss 单位为 KiB。
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def _print(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False, sort_keys=True))


def _human_error(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"


def cmd_signature(args: argparse.Namespace) -> int:
    validate_block_size(args.block_size)
    stats = build_signature(args.old, args.sig, args.block_size)
    stats["peak_rss_mib"] = round(peak_rss_mib(), 3)
    _print(stats)
    return 0


def cmd_delta(args: argparse.Namespace) -> int:
    stats = make_delta(args.sig, args.new, args.patch)
    patch_size = os.path.getsize(args.patch)
    _print(
        {
            "patch_path": args.patch,
            "patch_size_bytes": patch_size,
            "block_size": stats.block_size,
            "new_size": stats.new_size,
            "new_sha256": stats.new_sha256,
            "bytes_scanned": stats.bytes_scanned,
            "strong_checks": stats.strong_checks,
            "weak_hits": stats.weak_hits,
            "copy_bytes": stats.copy_bytes,
            "add_bytes": stats.add_bytes,
            "copy_instructions": stats.copy_instructions,
            "add_chunks": stats.add_chunks,
            "reuse_ratio": round(stats.copy_bytes / stats.new_size, 6)
            if stats.new_size
            else 0.0,
            "peak_rss_mib": round(peak_rss_mib(), 3),
        }
    )
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    stats = apply_patch(args.old, args.patch, args.out)
    stats["peak_rss_mib"] = round(peak_rss_mib(), 3)
    _print(stats)
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    stats = verify_patch(args.old, args.patch, args.new)
    _print(stats)
    return 0


def cmd_generate(args: argparse.Namespace) -> int:
    os.makedirs(args.dir, exist_ok=True)
    if args.kind == "corpus":
        info = make_corpus(args.dir, args.block_size)
        _print(
            {
                "kind": "corpus",
                "dir": args.dir,
                "block_size": info["block_size"],
                "cases": {
                    name: {"old": p[0], "new": p[1], "desc": p[2]}
                    for name, p in info["paths"].items()
                },
            }
        )
    else:
        info = make_large_pair(args.dir, args.size)
        _print({"kind": "large", **info})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bdelta", description="标准库离线差量工具（COPY/ADD 补丁）"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("signature", help="生成旧文件签名")
    p.add_argument("--old", required=True)
    p.add_argument("--sig", required=True)
    p.add_argument(
        "--block-size",
        type=int,
        default=DEFAULT_BLOCK_SIZE,
        help=f"B 取值 [{MIN_BLOCK_SIZE}, {MAX_BLOCK_SIZE}]，默认 {DEFAULT_BLOCK_SIZE}",
    )
    p.set_defaults(func=cmd_signature)

    p = sub.add_parser("delta", help="依据签名生成补丁")
    p.add_argument("--sig", required=True)
    p.add_argument("--new", required=True)
    p.add_argument("--patch", required=True)
    p.set_defaults(func=cmd_delta)

    p = sub.add_parser("apply", help="应用补丁原子发布目标文件")
    p.add_argument("--old", required=True)
    p.add_argument("--patch", required=True)
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_apply)

    p = sub.add_parser("verify", help="校验补丁并与目标文件逐字节比对")
    p.add_argument("--old", required=True)
    p.add_argument("--patch", required=True)
    p.add_argument("--new", required=True)
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("generate", help="本地合成测试数据")
    p.add_argument("kind", choices=["corpus", "large"])
    p.add_argument("--dir", required=True)
    p.add_argument("--block-size", type=int, default=1024)
    p.add_argument("--size", type=int, default=LARGE_SIZE)
    p.set_defaults(func=cmd_generate)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (
        ApplyError,
        PatchFormatError,
        InputChangedError,
        ValueError,
        OSError,
        RuntimeError,
    ) as exc:
        print(f"bdelta: 失败: {_human_error(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
