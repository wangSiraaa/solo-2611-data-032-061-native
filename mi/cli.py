"""Command line interface: run / inspect / verify."""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from .errors import DiagnosticError


def _cmd_run(args):
    from .pipeline import run_pipeline

    try:
        res = run_pipeline(args.plan, args.survey, args.applicability, args.out)
    except DiagnosticError as exc:
        print("运行前诊断失败,未产生任何插补:", file=sys.stderr)
        for f in exc.findings:
            print(f"  - {f}", file=sys.stderr)
        return 2
    pooled = res["pooled"]
    elig = res["eligibility"]
    chains = res["chains"]
    print(f"运行完成,产物目录:{res['out_dir']}")
    print(f"分析样本:{elig.n_eligible} / {res['dataset'].n_rows} 行合格")
    print("链状态:" + ", ".join(f"#{c.chain + 1}={c.status}" for c in chains))
    print("合并估计(Rubin):")
    for r in pooled["results"]:
        df = "Inf" if r["df_infinite"] else f"{r['df']:.1f}"
        print(f"  {r['term']:<12} {r['estimate']:>10.4f}  (SE {r['std_error']:.4f}, "
              f"df {df}, FMI {r['fmi']:.3f})")
    return 0


def _cmd_inspect(args):
    out = Path(args.out)
    required = ["design.json", "diagnostics.json", "chains.json",
                "estimates/pooled.json", "analysis_rows.csv"]
    missing = [r for r in required if not (out / r).exists()]
    if missing:
        print(f"产物缺失:{missing};请先运行 run 命令", file=sys.stderr)
        return 2

    diag = json.loads((out / "diagnostics.json").read_text(encoding="utf-8"))
    chains = json.loads((out / "chains.json").read_text(encoding="utf-8"))
    pooled = json.loads((out / "estimates" / "pooled.json").read_text(encoding="utf-8"))
    design = json.loads((out / "design.json").read_text(encoding="utf-8"))

    print("== 设计 ==")
    print(f"方法:{design['imputation']['method']}")
    print(f"m={design['imputation']['chains']}, iterations="
          f"{design['imputation']['iterations']}, seed={design['imputation']['seed']}")
    print(f"模型项:{design['terms']}")
    print()
    print("== 资格 ==")
    e = diag["eligibility"]
    print(f"合格 {e['n_eligible']},排除 {e['n_excluded']}")
    for x in e["excluded"]:
        print(f"  排除 {x['row_id']}: {x['reason']}")
    print()
    print("== 链 ==")
    for c in chains["chains"]:
        ev = "; ".join(f"{ev['kind']}({ev['variable'] or '-'})"
                       for ev in c["events"]) or "-"
        print(f"  链 {c['chain']}: {c['status']}  事件: {ev}")
        for var, tr in c["traces"].items():
            first, last = tr[0], tr[-1]
            print(f"    {var}: 首轮均值 {first['mean']:.4g} -> "
                  f"末轮均值 {last['mean']:.4g}(n={last['n']})")
    print()
    print("== 分布(观测 vs 插补)==")
    for var, d in diag["distributions"].items():
        o = d["observed"]
        line = f"  {var}: 观测 n={o['n']} 均值={o['mean']:.4g} SD={o['sd']:.4g}"
        if "imputed" in d:
            im = d["imputed"]
            line += (f"  | 插补 n={im['n']} 均值={im['mean']:.4g} "
                     f"SD={im['sd']:.4g}")
        print(line)
    print()
    print("== 合并结果(正式)==")
    print(f"  {'项':<12} {'估计':>10} {'SE':>9} {'U':>9} {'B':>9} {'df':>8} {'FMI':>6}")
    for r in pooled["results"]:
        df = "Inf" if r["df_infinite"] else f"{r['df']:.1f}"
        print(f"  {r['term']:<12} {r['estimate']:>10.4f} {r['std_error']:>9.4f} "
              f"{r['within_var']:>9.4f} {r['between_var']:>9.4f} {df:>8} "
              f"{r['fmi']:>6.3f}")
    cc_path = out / "estimates" / "complete_case.csv"
    print()
    print("== 完整病例(对照)==")
    if cc_path.exists():
        with open(cc_path, newline="", encoding="utf-8") as fh:
            for row in csv.reader(fh):
                print("  " + "  ".join(row))
    print()
    print("== 限制 ==")
    for lim in diag["limitations"]:
        print(f"  - {lim}")
    return 0


def _cmd_verify(args):
    from .verify import verify_run

    try:
        checks = verify_run(args.plan, args.survey, args.applicability, args.out)
    except DiagnosticError as exc:
        print("验证时诊断失败:", file=sys.stderr)
        for f in exc.findings:
            print(f"  - {f}", file=sys.stderr)
        return 2
    ok_all = True
    for name, ok, detail in checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {name} -- {detail}")
        ok_all &= ok
    return 0 if ok_all else 1


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="mi",
        description="多重插补分析:迭代条件插补 + Rubin 合并",
    )
    sub = ap.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="诊断、插补、拟合并写出全部产物")
    p_run.add_argument("--plan", required=True)
    p_run.add_argument("--survey", required=True)
    p_run.add_argument("--applicability", required=True)
    p_run.add_argument("--out", required=True)
    p_run.set_defaults(func=_cmd_run)

    p_ins = sub.add_parser("inspect", help="读取产物目录并打印摘要")
    p_ins.add_argument("--out", required=True)
    p_ins.set_defaults(func=_cmd_inspect)

    p_ver = sub.add_parser("verify", help="独立复核产物(单元不变性/重新合并)")
    p_ver.add_argument("--plan", required=True)
    p_ver.add_argument("--survey", required=True)
    p_ver.add_argument("--applicability", required=True)
    p_ver.add_argument("--out", required=True)
    p_ver.set_defaults(func=_cmd_verify)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
