"""命令行入口。

子命令：
  generate   生成本地合成历史（固定套件或可复现随机历史）
  audit      审计单个历史文件，输出规范 JSON 报告，可选写入 SQLite
  verify     用独立复核器对照“历史 + 已生成报告”做交叉核验
  list-db    列出 SQLite 中已保存的审计记录

退出码：0 成功；1 复核未通过；2 用法/IO/JSON 解析错误。
注意：历史存在输入语义错误属于“成功产出一份错误报告”，退出码仍为 0。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any

from . import generate as gen
from .analysis import analyze
from .model import InputError, load_json, parse_and_validate
from .report import build_error_report, build_report, canonical_dumps, fingerprint
from .storage import AuditStore
from .verify import enumerate_legal_recursive, verify_analysis

# <=6 个事务时在报告内全量保留合法排列，并强制独立复核
FULL_ENUM_LIMIT = 6


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _input_summary_raw(data: dict[str, Any]) -> dict[str, Any]:
    tids = data.get("transactions")
    keys = data.get("keys")
    ops = data.get("operations")
    return {
        "n_transactions": len(tids) if isinstance(tids, list) else None,
        "transactions": sorted(tids) if isinstance(tids, list) else None,
        "n_keys": len(keys) if isinstance(keys, dict) else None,
        "keys": sorted(keys) if isinstance(keys, dict) else None,
        "n_operations": len(ops) if isinstance(ops, list) else None,
    }


# --------------------------------------------------------------------------- #
# generate
# --------------------------------------------------------------------------- #

def cmd_generate(args: argparse.Namespace) -> int:
    out = Path(args.out)
    if args.random:
        history = gen.generate_random_history(
            seed=args.seed,
            n_transactions=args.transactions,
            n_keys=args.keys,
            n_ops=args.ops,
        )
        target = out if out.suffix == ".json" else out / f"{history['history_id']}.json"
        _write_text(target, canonical_dumps(history))
        print(f"已生成随机历史（seed={args.seed}）：{target}")
        return 0

    if args.all:
        out.mkdir(parents=True, exist_ok=True)
        for name in gen.list_suites():
            target = out / f"{name}.json"
            _write_text(target, canonical_dumps(gen.FIXED_SUITES[name]))
            print(f"已生成固定样例：{target}")
        print(f"共 {len(gen.FIXED_SUITES)} 个样例 -> {out}")
        return 0

    if args.suite not in gen.FIXED_SUITES:
        print(f"未知样例 {args.suite!r}，可选：{', '.join(gen.list_suites())}", file=sys.stderr)
        return 2
    history = gen.FIXED_SUITES[args.suite]
    target = out if out.suffix == ".json" else out / f"{args.suite}.json"
    _write_text(target, canonical_dumps(history))
    print(f"已生成固定样例：{target}")
    return 0


# --------------------------------------------------------------------------- #
# audit
# --------------------------------------------------------------------------- #

def run_audit(data: dict[str, Any], raw_text: str, *, do_verify: bool = True) -> dict[str, Any]:
    """核心审计流程，便于测试与 CLI 共用。"""
    input_fp = fingerprint(raw_text)
    history_id = data.get("history_id", "default") if isinstance(data.get("history_id", "default"), str) else "default"
    history, errors = parse_and_validate(data)

    if errors:
        return build_error_report(history_id, input_fp, errors, _input_summary_raw(data))

    assert history is not None
    analysis = analyze(history)
    include_all = len(history.tids) <= FULL_ENUM_LIMIT

    verification = None
    if do_verify:
        # 先组装一版不含验证结果的报告供复核器对照
        provisional = build_report(history, analysis, input_fp, None, include_all)
        vr = verify_analysis(history, provisional)
        verification = vr.to_dict()
        # 独立枚举的集合与主枚举再做一次集合级对照（<=6 事务时报告含全量集合）
        if include_all:
            independent_set = sorted(map(list, enumerate_legal_recursive(history)))
            if independent_set != analysis.view.legal_permutations:
                verification["passed"] = False
                verification["discrepancies"].append(
                    "[full_set_cross_check] 独立递归枚举集合与主枚举集合不一致")

    return build_report(history, analysis, input_fp, verification, include_all)


def cmd_audit(args: argparse.Namespace) -> int:
    input_path = Path(args.input)
    try:
        raw_text = input_path.read_text(encoding="utf-8")
        data = load_json(input_path)
    except (OSError, InputError) as exc:
        print(f"输入错误：{exc}", file=sys.stderr)
        return 2

    t0 = time.perf_counter()
    report = run_audit(data, raw_text, do_verify=not args.no_verify)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    report_text = canonical_dumps(report)

    if args.report:
        _write_text(Path(args.report), report_text)
        print(f"报告已写入：{args.report}")
    else:
        sys.stdout.write(report_text)

    if args.db:
        with AuditStore(args.db) as store:
            store.save(
                history_id=report["history_id"],
                fingerprint=report["input_fingerprint_sha256"],
                status=report["status"],
                input_json=raw_text if raw_text.endswith("\n") else raw_text + "\n",
                report_json=report_text,
            )
        print(f"已写入 SQLite：{args.db}")

    _print_summary(report, elapsed_ms)
    if report.get("independent_verification") and not report["independent_verification"]["passed"]:
        print("独立复核未通过！", file=sys.stderr)
        return 1
    return 0


def _print_summary(report: dict[str, Any], elapsed_ms: float) -> None:
    print("-" * 68, file=sys.stderr)
    print(f"历史 {report['history_id']}  状态={report['status']}  耗时={elapsed_ms:.2f} ms",
          file=sys.stderr)
    if report["status"] != "ok":
        for e in report["errors"]:
            loc = f"seq={e['seq']} " if e.get("seq") is not None else ""
            print(f"  错误 {e['code']}: {loc}{e['message']}", file=sys.stderr)
        return
    view = report["view_serializability"]
    verdict = report["verdict"]
    graph = report["conflict_graph"]
    print(f"  枚举排列总数={view['total_permutations_enumerated']}  "
          f"合法排列数={view['legal_count']}  最小合法排列={view['smallest_legal']}",
          file=sys.stderr)
    print(f"  冲突边数={len(graph['edges'])}  有环={graph['has_cycle']}  "
          f"环={[(e['from_tid'], e['to_tid']) for e in graph['cycle_edges']]}",
          file=sys.stderr)
    print(f"  视图可串行化={verdict['view_serializable']}  "
          f"冲突可串行化={verdict['conflict_serializable']}", file=sys.stderr)
    iv = report.get("independent_verification")
    if iv:
        print(f"  独立复核={'通过' if iv['passed'] else '未通过'} "
              f"（{len(iv['checks'])} 项检查）", file=sys.stderr)


# --------------------------------------------------------------------------- #
# verify
# --------------------------------------------------------------------------- #

def cmd_verify(args: argparse.Namespace) -> int:
    try:
        data = load_json(args.input)
        report_text = Path(args.report).read_text(encoding="utf-8")
        import json
        report = json.loads(report_text)
    except (OSError, InputError) as exc:
        print(f"输入错误：{exc}", file=sys.stderr)
        return 2

    history, errors = parse_and_validate(data)
    if errors:
        print("历史本身存在输入语义错误，无法复核：", file=sys.stderr)
        for e in errors:
            print(f"  {e.code}: {e.message}", file=sys.stderr)
        return 1
    assert history is not None

    vr = verify_analysis(history, report)
    print(canonical_dumps(vr.to_dict()))
    for c in vr.checks:
        mark = "PASS" if c["passed"] else "FAIL"
        print(f"[{mark}] {c['check']}: {c['detail']}", file=sys.stderr)
    return 0 if vr.passed else 1


# --------------------------------------------------------------------------- #
# list-db
# --------------------------------------------------------------------------- #

def cmd_list_db(args: argparse.Namespace) -> int:
    if not Path(args.db).exists():
        print(f"数据库不存在：{args.db}", file=sys.stderr)
        return 2
    with AuditStore(args.db) as store:
        rows = store.list_runs()
    if not rows:
        print("（无记录）")
        return 0
    for r in rows:
        print(f"{r['history_id']}\t{r['status']}\t{r['created_at_utc']}\t{r['input_fingerprint_sha256'][:12]}")
    return 0


# --------------------------------------------------------------------------- #
# 解析器
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m txaudit",
        description="离线事务历史审计：单版本语义校验、视图/冲突可串行化判定",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_gen = sub.add_parser("generate", help="生成本地合成历史")
    p_gen.add_argument("--suite", help="固定样例名")
    p_gen.add_argument("--all", action="store_true", help="生成全部固定样例")
    p_gen.add_argument("--random", action="store_true", help="生成可复现随机历史")
    p_gen.add_argument("--seed", type=int, default=20261113)
    p_gen.add_argument("--transactions", type=int, default=8)
    p_gen.add_argument("--keys", type=int, default=12)
    p_gen.add_argument("--ops", type=int, default=64)
    p_gen.add_argument("--out", required=True, help="输出文件或目录")
    p_gen.set_defaults(func=cmd_generate)

    p_audit = sub.add_parser("audit", help="审计历史并输出规范 JSON 报告")
    p_audit.add_argument("input", help="输入历史 JSON 文件")
    p_audit.add_argument("--report", help="报告输出路径（缺省打印到 stdout）")
    p_audit.add_argument("--db", help="SQLite 库文件路径，同时保存输入与报告")
    p_audit.add_argument("--no-verify", action="store_true", help="跳过独立复核")
    p_audit.set_defaults(func=cmd_audit)

    p_verify = sub.add_parser("verify", help="独立复核已生成的报告")
    p_verify.add_argument("input", help="输入历史 JSON 文件")
    p_verify.add_argument("--report", required=True, help="待复核的报告 JSON 文件")
    p_verify.set_defaults(func=cmd_verify)

    p_db = sub.add_parser("list-db", help="列出 SQLite 中的审计记录")
    p_db.add_argument("--db", required=True)
    p_db.set_defaults(func=cmd_list_db)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
