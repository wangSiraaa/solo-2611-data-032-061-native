"""Command line interface: the offline ``compare`` command."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .analyze import analyze
from .manifest import load_capture
from .report import render_markdown


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="plancompare",
        description="离线对比两份 PostgreSQL EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) 计划；"
                    "不连接数据库、不推荐索引、不改写 SQL。",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser("compare", help="对比 before/after 两份计划文件")
    c.add_argument("before", help="before 计划 JSON 文件")
    c.add_argument("after", help="after 计划 JSON 文件")
    c.add_argument("--capture",
                   help="覆盖两侧的采集清单（含 SQL 身份、参数、数据快照、采集设置）")
    c.add_argument("--before-capture", help="before 侧单独的采集清单")
    c.add_argument("--after-capture", help="after 侧单独的采集清单")
    c.add_argument("--json-out", default="plan_compare.json",
                   help="机器可读对应表输出路径（默认 plan_compare.json）")
    c.add_argument("--md-out", default="plan_compare.md",
                   help="Markdown 报告输出路径（默认 plan_compare.md）")
    return parser


def _cmd_compare(args: argparse.Namespace) -> int:
    if args.capture and (args.before_capture or args.after_capture):
        print("错误：--capture 与 --before-capture/--after-capture 不能同时使用",
              file=sys.stderr)
        return 1

    before_cap = after_cap = None
    if args.capture:
        before_cap = after_cap = load_capture(args.capture)
    else:
        if args.before_capture:
            before_cap = load_capture(args.before_capture)
        if args.after_capture:
            after_cap = load_capture(args.after_capture)

    result = analyze(args.before, args.after, before_cap, after_cap)

    # Cross-check file names recorded in a shared manifest.
    if args.capture and before_cap is not None:
        for side, given in (("before", args.before), ("after", args.after)):
            recorded = before_cap.file_for(side)
            if recorded and Path(given).name != Path(recorded).name:
                result["comparability"]["caveats"].append(
                    f"清单记录 {side}.file={recorded}，实际传入 {Path(given).name}，请确认文件对应关系")

    json_path = Path(args.json_out)
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
    md_path = Path(args.md_out)
    md_path.write_text(render_markdown(result), encoding="utf-8")

    counts = {"matched": 0, "added": 0, "removed": 0, "ambiguous": 0}
    for p in result["correspondence"]:
        counts[p["status"]] += 1
    comp = result["comparability"]
    print(f"可比性：{comp['status']}（directly_comparable={comp['directly_comparable']}，"
          f"can_attribute_to_plan={comp['can_attribute_to_plan']}）")
    print(f"节点对应：{counts['matched']} 对应 / {counts['added']} 新增 / "
          f"{counts['removed']} 消失 / {counts['ambiguous']} 组无法唯一对应")
    if comp["status"] == "identity_mismatch":
        print("警告：SQL 身份不一致，结果不可直接比较", file=sys.stderr)
    print(f"机器可读对应表：{json_path}")
    print(f"Markdown 报告：{md_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "compare":
        return _cmd_compare(args)
    return 1  # pragma: no cover
