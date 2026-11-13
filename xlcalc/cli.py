"""命令行入口：calculate / explain / verify。

calculate: 一次性计算整个工作簿，产出结果副本（保留公式与布局）、
           值视图 JSON、单元格解释 JSON，并自动执行 verify。
explain:   打印单个 Sheet!A1 或矩形范围（按格逐行）的依赖与计算来源。
verify:    对已有产物做独立 ZIP/XML + openpyxl 双入口验证。

源文件始终以只读方式打开，绝不原地写回。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from .engine import Engine
from .ooxml import read_workbook, split_coord
from .verify import VerificationError, sha256_file, verify_copy
from .views import build_explain_view, build_value_view, explain_one
from .writer import write_result_copy


def _json_default(obj):
    if isinstance(obj, (set, frozenset)):
        return sorted(obj)
    return str(obj)


def _dump_json(path: str, payload: dict) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, default=_json_default)
        fh.write("\n")


def _load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _resolve_sheet_coord(token: str, sheet_order: list[str]) -> tuple[str, str]:
    """解析 'Sheet'!A1 / 'Sheet'!A1:B2 / Sheet!A1；无表名时取首表。"""
    token = token.strip()
    if "!" in token:
        sheet_part, coord = token.split("!", 1)
        sheet_part = sheet_part.strip()
        if sheet_part.startswith("'") and sheet_part.endswith("'"):
            sheet_part = sheet_part[1:-1].replace("''", "'")
        return sheet_part, coord.strip()
    return sheet_order[0], token


def cmd_calculate(args: argparse.Namespace) -> int:
    source = args.source
    if not os.path.exists(source):
        print(f"源文件不存在: {source}", file=sys.stderr)
        return 2
    digest_before = sha256_file(source)
    wb = read_workbook(source)

    engine = Engine(wb)
    results = engine.evaluate_all()

    os.makedirs(args.outdir, exist_ok=True)
    base = os.path.splitext(os.path.basename(source))[0]
    copy_path = args.copy_out or os.path.join(args.outdir, f"{base}.calculated.xlsx")
    values_path = args.values_out or os.path.join(args.outdir, f"{base}.values.json")
    explain_path = args.explain_out or os.path.join(args.outdir, f"{base}.explain.json")

    stats = write_result_copy(source, copy_path, wb, results)
    value_view = build_value_view(wb, results)
    explain_view = build_explain_view(wb, results)
    _dump_json(values_path, value_view)
    _dump_json(explain_path, explain_view)

    print(f"源文件 (只读):       {source}")
    print(f"  sha256:            {digest_before}")
    print(f"结果副本:            {copy_path}")
    print(f"值视图:              {values_path}")
    print(f"单元格解释:          {explain_path}")
    print(f"公式格 {stats['formula_cells']} 个，"
          f"更新缓存 {stats['updated']} 个，"
          f"错误结果 {stats['errors']} 个（其中不支持 {stats['unsupported']} 个）")

    if not args.no_verify:
        report = verify_copy(source, copy_path, wb, results, value_view, digest_before)
        _dump_json(os.path.join(args.outdir, f"{base}.verify.json"),
                   report.to_json_data())
        failed = [c for c in report.checks if c["result"] != "PASS"]
        print(f"verify: {'全部通过' if not failed else f'{len(failed)} 项失败'}")
        for check in report.checks:
            detail = f" — {check['detail']}" if check["detail"] else ""
            print(f"  [{check['result']}] {check['check']}{detail}")
        return 0 if report.passed else 1
    return 0


def cmd_explain(args: argparse.Namespace) -> int:
    wb = read_workbook(args.source)
    engine = Engine(wb)
    engine.evaluate_all()
    sheet_name, ref = _resolve_sheet_coord(args.target, wb.sheet_order)
    if sheet_name not in wb.sheets:
        print(f"工作表不存在: {sheet_name!r}（可用: {wb.sheet_order}）", file=sys.stderr)
        return 2

    coords = _expand_target(ref)
    lines = []
    for coord in coords:
        entry = explain_one(engine, wb, sheet_name, coord)
        lines.append(json.dumps(entry, ensure_ascii=False, indent=2))
    print("\n".join(lines))
    return 0


def _expand_target(ref: str) -> list[str]:
    if ":" not in ref:
        return [ref]
    left, right = ref.split(":", 1)
    c1, r1 = split_coord(left)
    c2, r2 = split_coord(right)
    if c1 > c2 or r1 > r2:
        c1, c2 = min(c1, c2), max(c1, c2)
        r1, r2 = min(r1, r2), max(r1, r2)
    from .parser import col_to_letters

    out = []
    for r in range(r1, r2 + 1):
        for c in range(c1, c2 + 1):
            out.append(f"{col_to_letters(c)}{r}")
    return out


def cmd_verify(args: argparse.Namespace) -> int:
    digest_before = sha256_file(args.source)
    wb = read_workbook(args.source)
    engine = Engine(wb)
    results = engine.evaluate_all()
    value_view = _load_json(args.values)
    try:
        report = verify_copy(
            args.source, args.copy_out, wb, results, value_view, digest_before
        )
    except VerificationError as exc:
        print(f"验证失败: {exc}", file=sys.stderr)
        return 1
    for check in report.checks:
        detail = f" — {check['detail']}" if check["detail"] else ""
        print(f"  [{check['result']}] {check['check']}{detail}")
    print(f"verify: {'全部通过' if report.passed else '存在失败项'}")
    return 0 if report.passed else 1

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="xlcalc",
        description="一次性 OOXML 工作簿计算工具（受限公式子集，不使用 eval）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_calc = sub.add_parser("calculate", help="计算工作簿并产出副本/值视图/解释")
    p_calc.add_argument("source", help="源 .xlsx（只读，不会被修改）")
    p_calc.add_argument("--outdir", default="out", help="产物目录（默认 ./out）")
    p_calc.add_argument("--copy-out", help="结果副本路径")
    p_calc.add_argument("--values-out", help="值视图 JSON 路径")
    p_calc.add_argument("--explain-out", help="解释 JSON 路径")
    p_calc.add_argument("--no-verify", action="store_true", help="跳过自动 verify")
    p_calc.set_defaults(func=cmd_calculate)

    p_ex = sub.add_parser("explain", help="解释单个格或矩形范围")
    p_ex.add_argument("source", help="源 .xlsx")
    p_ex.add_argument("target", help="如 \"'Sales Detail'!D2:D5\" 或 Summary!B2 或 B2")
    p_ex.set_defaults(func=cmd_explain)

    p_v = sub.add_parser("verify", help="对已有产物做双入口独立验证")
    p_v.add_argument("source", help="源 .xlsx（只读）")
    p_v.add_argument("copy_out", help="待验证的结果副本 .xlsx")
    p_v.add_argument("values", help="calculate 产出的值视图 JSON")
    p_v.set_defaults(func=cmd_verify)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
