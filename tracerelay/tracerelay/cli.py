"""命令行入口：generate / import / analyze / report / view / verify / export / serve。"""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__
from .analyze import analyze_all
from .generator import CASES, build_all, build_bulk
from .model import COV_AWAIT, COV_GAP, COV_QUEUE, COV_SELF, fmt_ns
from .report import build_report, render_text
from .storage import connect, get_raw, init_db
from .validate import load_trace

CAT_CN = {COV_SELF: "自身活动", COV_AWAIT: "显式等待", COV_QUEUE: "排队等待",
          COV_GAP: "不可解释空档"}


def cmd_generate(args):
    if args.what in ("cases", "all"):
        paths = build_all(args.data_dir)
        for k, v in paths.items():
            print(f"[case] {k:10s} -> {v}")
    if args.what in ("bulk", "all"):
        info = build_bulk(args.bulk_path, args.bulk_traces)
        print("[bulk]", json.dumps(info, ensure_ascii=False))


def cmd_import(args):
    conn = connect(args.db)
    init_db(conn)
    total = {"span": 0, "event": 0, "await": 0, "dup": 0, "conflict": 0, "bad": 0}
    for path in args.inputs:
        from .storage import import_jsonl
        c = import_jsonl(conn, path)
        for k in total:
            total[k] += c[k]
        print(f"[import] {path}: {c}")
    print("[import] total:", total)


def cmd_analyze(args):
    conn = connect(args.db)
    summaries = analyze_all(conn)
    bad = [s for s in summaries if s.get("conservation_ok") == 0]
    for s in summaries:
        win = s.get("root_window_ns")
        print(f"[trace] {s['trace_id']:12s} root={s.get('root_span_id')} "
              f"window={fmt_ns(win)} self={fmt_ns(s['self_ns'])} "
              f"await={fmt_ns(s['await_ns'])} queue={fmt_ns(s['queue_ns'])} "
              f"gap={fmt_ns(s['gap_ns'])} complete={s['complete_flag']} "
              f"conservation={'OK' if s['conservation_ok'] else 'FAIL'} "
              f"background={fmt_ns(s['background_ns'])}")
    if bad:
        print("守恒校验失败：", [s["trace_id"] for s in bad])
        sys.exit(2)


def cmd_report(args):
    conn = connect(args.db)
    rep = build_report(conn)
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        print(render_text(rep))


def cmd_view(args):
    conn = connect(args.db)
    g = load_trace(conn, args.trace_id)
    summ = conn.execute("SELECT * FROM analysis_trace WHERE trace_id=?",
                        (args.trace_id,)).fetchone()
    if not summ:
        print(f"trace {args.trace_id} 尚未分析或不存在", file=sys.stderr)
        sys.exit(1)
    print(f"trace {args.trace_id}  根 {summ['root_service']}/{summ['root_operation']}  "
          f"窗口 {fmt_ns(summ['root_window_ns'])}  "
          f"完整={bool(summ['complete_flag'])} 守恒={'OK' if summ['conservation_ok'] else 'FAIL'}")
    print("-" * 78)
    print(f"{'#':>3} {'起(ms)':>9} {'止(ms)':>9} {'时长':>10}  类别 / 子标签 / 证据")
    for r in conn.execute(
            "SELECT * FROM timeline_slices WHERE trace_id=? ORDER BY slice_no",
            (args.trace_id,)):
        print(f"{r['slice_no']:>3} {r['start_ns'] / 1e6:>9.2f} {r['end_ns'] / 1e6:>9.2f} "
              f"{fmt_ns(r['duration_ns']):>10}  {CAT_CN[r['category']]} | "
              f"{r['sublabel']} | {r['ref_kind']}:{r['ref_id']} "
              f"{r['service']}/{r['operation']}")
    print("-" * 78)
    print("await 解除关系：")
    for r in conn.execute("SELECT * FROM await_causes WHERE trace_id=?", (args.trace_id,)):
        print(f"  {r['await_id']} mode={r['mode']} "
              f"window=[{fmt_ns(r['start_ns'])},{fmt_ns(r['end_ns'])}] "
              f"blocker={r['blocker_phase']} winners={r['resolved_by']} "
              f"detail={r['blocker_detail']}")
    print("RPC 配对：")
    for r in conn.execute("SELECT * FROM rpc_pairs WHERE trace_id=?", (args.trace_id,)):
        print(f"  client={r['client_span_id']} server={r['server_span_id']} "
              f"basis={r['match_basis']} confidence={r['confidence']}")
    print("诊断：")
    for r in conn.execute("SELECT * FROM diagnostics WHERE trace_id=? ORDER BY diag_code",
                          (args.trace_id,)):
        print(f"  [{r['certainty']:7s}] {r['diag_code']} {r['title']}")
    if args.raw_ref:
        rec = get_raw(conn, *args.raw_ref)
        print("原始记录：", json.dumps(rec, ensure_ascii=False, indent=2))


def cmd_verify(args):
    """独立核验：守恒不变量 + 覆盖和 == 根窗口 + 资源账本不要求等于根窗口。"""
    from .verify import run_verification
    conn = connect(args.db)
    result = run_verification(conn)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["all_ok"]:
        sys.exit(2)


def cmd_export(args):
    from .exporting import export_bundle
    conn = connect(args.db)
    n = export_bundle(conn, args.out_dir, trace_id=args.trace_id)
    print(f"[export] 写出 {n} 个文件到 {args.out_dir}（分析输入/诊断/可复算中间片段）")


def cmd_serve(args):
    from .server import serve
    serve(args.db, args.host, args.port)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="tracerelay",
                                 description=f"TraceRelay v{__version__} 离线合成追踪复盘工具")
    ap.add_argument("--db", default="var/tracerelay.db")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("generate", help="合成追踪数据（本地合成，不采集真实系统）")
    p.add_argument("--what", choices=["cases", "bulk", "all"], default="all")
    p.add_argument("--data-dir", default="var/data")
    p.add_argument("--bulk-path", default="var/data/bulk.jsonl")
    p.add_argument("--bulk-traces", type=int, default=9500)
    p.set_defaults(func=cmd_generate)

    p = sub.add_parser("import", help="导入 JSONL span/event/await 到 SQLite")
    p.add_argument("inputs", nargs="+")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("analyze", help="重建关系并计算两套账本")
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("report", help="批量报告（池化分位数）")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("view", help="查看单 trace 时间线/await/RPC/诊断")
    p.add_argument("trace_id")
    p.add_argument("--raw-ref", nargs=2, metavar=("SOURCE_FILE", "LINE_NO"))
    p.set_defaults(func=cmd_view)

    p = sub.add_parser("verify", help="独立核验守恒与不重计")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("export", help="导出分析输入、诊断与可复算中间片段")
    p.add_argument("--out-dir", default="var/export")
    p.add_argument("--trace-id")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("serve", help="启动本地时间线 Web 界面")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.set_defaults(func=cmd_serve)
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
