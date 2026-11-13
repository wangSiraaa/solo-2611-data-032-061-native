"""命令行入口：generate / match / verify / rules。"""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__
from .automaton import AhoCorasick
from .codec import InvalidUTF8
from .config import ConfigError, load_patterns
from .engine import stream_match
from .generate import generate
from .rules_doc import RULES_TEXT
from .verify import VerifyFailure, verify

_DEFAULT_CHUNK = 1 << 20  # 1 MiB


def _cmd_generate(args: argparse.Namespace) -> int:
    out_dir = generate(args.out_dir)
    print(f"已生成测试数据：{out_dir}")
    return 0


def _cmd_match(args: argparse.Namespace) -> int:
    try:
        patterns = load_patterns(args.config)
    except ConfigError as exc:
        print(f"配置失败：{exc}", file=sys.stderr)
        return 2
    automaton = AhoCorasick(patterns)
    try:
        with open(args.input, "rb") as fh:
            count = stream_match(fh, automaton, args.output, args.chunk_size)
    except InvalidUTF8 as exc:
        print(f"匹配失败：{exc}", file=sys.stderr)
        return 3
    except FileNotFoundError as exc:
        print(f"匹配失败：{exc}", file=sys.stderr)
        return 4
    print(
        json.dumps(
            {
                "status": "ok",
                "output": os.path.abspath(args.output),
                "hits": count,
                "patterns": len(patterns),
            },
            ensure_ascii=False,
        )
    )
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    try:
        report = verify(args.data_dir)
    except VerifyFailure as exc:
        print(f"验证失败：{exc}", file=sys.stderr)
        return 1
    print(json.dumps({
        "status": "ok",
        "small_hits": report["total_hits_small"],
        "big_hits": report["total_hits_big"],
        "peak_rss_mib": report.get("peak_rss_mib", []),
        "checks": len(report["checks"]),
    }, ensure_ascii=False, indent=2))
    return 0


def _cmd_rules(_args: argparse.Namespace) -> int:
    sys.stdout.write(RULES_TEXT)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m normmatch",
        description="大型 UTF-8 文本规范化关键词流式匹配（纯标准库）",
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_gen = sub.add_parser("generate", help="生成测试语料、关键词与非法样本")
    p_gen.add_argument("out_dir", help="输出目录（不存在则创建）")
    p_gen.set_defaults(func=_cmd_generate)

    p_match = sub.add_parser("match", help="流式匹配并原子发布 JSON Lines 结果")
    p_match.add_argument("input", help="输入 UTF-8 文本文件")
    p_match.add_argument("config", help="关键词配置 JSON")
    p_match.add_argument("output", help="结果输出路径（JSON Lines）")
    p_match.add_argument(
        "--chunk-size", type=int, default=_DEFAULT_CHUNK,
        help="读取块大小（字节，默认 1048576）",
    )
    p_match.set_defaults(func=_cmd_match)

    p_verify = sub.add_parser("verify", help="运行全量验证套件")
    p_verify.add_argument("data_dir", help="generate 产出的数据目录")
    p_verify.set_defaults(func=_cmd_verify)

    p_rules = sub.add_parser("rules", help="打印规范化与定位规则说明")
    p_rules.set_defaults(func=_cmd_rules)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
