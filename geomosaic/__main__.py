"""命令行入口:plan / mosaic / query-source。

退出码:0 成功;2 计划被拒绝(校验失败);3 镶嵌验证未通过;4 查询不一致。
"""
from __future__ import annotations

import argparse
import json
import sys

from .engine import plan_diagnostics, run_mosaic
from .errors import PlanError
from .plan import load_plan
from .query import query_point


def _load_or_reject(path):
    try:
        return load_plan(path)
    except PlanError as e:
        print(f"计划被拒绝: {e}", file=sys.stderr)
        sys.exit(2)


def cmd_plan(args) -> int:
    plan = _load_or_reject(args.plan)
    diag = plan_diagnostics(plan)
    print(json.dumps(diag, ensure_ascii=False, indent=2))
    return 0


def cmd_mosaic(args) -> int:
    plan = _load_or_reject(args.plan)
    wshape = tuple(args.window_shape) if args.window_shape else None
    report = run_mosaic(plan, args.out, window_shape=wshape,
                        verify=not args.no_verify)
    g = report["grid"]
    r = report["results"]
    print(f"目标网格: {g['width']}x{g['height']} @ {g['resolution']} {g['crs']}")
    print(f"实际边界: {g['actual_bounds']}")
    print(f"窗口: {len(report['windows'])} 个 (window_shape={report['window_shape']})")
    print(f"有效像元: {r['valid_pixels']},无效: {r['invalid_pixels']},"
          f"按来源: {r['per_source_pixels']}")
    ver = report["verification"]
    if ver.get("skipped"):
        print("验证: 已跳过 (--no-verify)")
    elif ver["passed"]:
        print(f"验证: 通过 (最大数值偏差 {ver['value_max_abs_diff']:.3g})")
    else:
        print(f"验证: 未通过 {ver}", file=sys.stderr)
        return 3
    print(f"产品目录: {args.out} (mosaic.tif / mask.tif / sources.tif / "
          f"sources_index.json / report.json)")
    return 0


def cmd_query_source(args) -> int:
    plan = _load_or_reject(args.plan)
    result = query_point(plan, args.x, args.y, crs=args.crs, out_dir=args.out)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("agreement") is False:
        return 4
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="geomosaic",
        description="连续指标 GeoTIFF 镶嵌:统一网格、无缝窗口处理、按优先级逐像元裁决")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_plan = sub.add_parser("plan", help="校验计划并输出网格推算与逐窗口读取计划")
    p_plan.add_argument("--plan", required=True, help="plan.json 路径")
    p_plan.set_defaults(func=cmd_plan)

    p_mos = sub.add_parser("mosaic", help="执行镶嵌,产出指标影像/mask/来源栅格/报告")
    p_mos.add_argument("--plan", required=True, help="plan.json 路径")
    p_mos.add_argument("--out", default="out", help="输出目录(默认 out)")
    p_mos.add_argument("--window-shape", nargs=2, type=int, metavar=("H", "W"),
                       help="覆盖计划的输出窗口 [高 宽](像元)")
    p_mos.add_argument("--no-verify", action="store_true",
                       help="跳过独立逐像元参考验证")
    p_mos.set_defaults(func=cmd_mosaic)

    p_q = sub.add_parser("query-source", help="按地理坐标查询数值与来源,并与产物核对")
    p_q.add_argument("--plan", required=True, help="plan.json 路径")
    p_q.add_argument("--x", required=True, type=float)
    p_q.add_argument("--y", required=True, type=float)
    p_q.add_argument("--crs", default=None, help="坐标 CRS(默认目标 CRS)")
    p_q.add_argument("--out", default="out", help="产物目录(默认 out;缺失时仅重算)")
    p_q.set_defaults(func=cmd_query_source)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
