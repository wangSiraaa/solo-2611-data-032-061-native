"""Command-line entry points.

  deworkbench generate   — create the five required synthetic demo datasets
  deworkbench precheck   — validate inputs and design identifiability only
  deworkbench analyze    — full pipeline into a self-contained run directory
  deworkbench verify     — independent verification checks
  deworkbench simulate   — repeated-seed detection / false-positive study
  deworkbench view       — print machine-readable results from an archived run
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import pandas as pd

from .errors import WorkbenchError
from .generate import generate_dataset, make_confounded_dataset
from .io import load_dataset
from .design import build_design
from .plan import load_plan
from .pipeline import run_analysis
from .simulate import run_all_simulations
from .verify import run_verification


def _write_plan(path: Path, content: dict) -> None:
    import yaml
    path.write_text(yaml.safe_dump(content, sort_keys=False, allow_unicode=True),
                    encoding="utf-8")


def _demo_plans(data_root: Path, plan_root: Path) -> None:
    plan_root.mkdir(parents=True, exist_ok=True)
    common = {
        "conditions": ["control", "treated"],
        "contrasts": [{"name": "trt_vs_ctrl", "numerator": "treated",
                       "denominator": "control"}],
        "filter_rule": {"type": "cpm", "min_value": 1.0},
        "include_batch": True,
    }
    _write_plan(plan_root / "plan_independent.yaml", {
        "project": "demo_independent", "design": "independent",
        "encoding": "treatment", "random_seed": 20261113, **common})
    _write_plan(plan_root / "plan_paired.yaml", {
        "project": "demo_paired", "design": "paired",
        "encoding": "treatment", "random_seed": 20261113,
        "duplicate_subject_policy": "error",
        "missing_pair_policy": "error", **common})
    _write_plan(plan_root / "plan_sumcoded.yaml", {
        "project": "demo_sumcoded", "design": "independent",
        "encoding": "sum", "random_seed": 20261113, **common})
    _write_plan(plan_root / "plan_confounded.yaml", {
        "project": "demo_confounded", "design": "independent",
        "encoding": "treatment", "random_seed": 20261113, **common})


def cmd_generate(args) -> int:
    root = Path(args.out)
    data = root / "data"
    # 1. Balanced-batch null data (no implanted differences).
    generate_dataset(data / "null_balanced", design="independent",
                     n_effect_genes=0, seed=20261101, prefix="null")
    # 2. Known fold changes across balanced batches.
    generate_dataset(data / "foldchange", design="independent",
                     n_effect_genes=40, log2fc_range=(0.8, 1.8),
                     seed=20261102, prefix="fc")
    # 3. Paired individuals with sizeable subject-level variation.
    generate_dataset(data / "paired", design="paired", n_subjects=10,
                     n_per_condition=10, n_effect_genes=30,
                     log2fc_range=(0.8, 1.6), subject_sd=0.6,
                     seed=20261103, prefix="pair")
    # 4. Extreme high-count gene scenario.
    generate_dataset(data / "extreme", design="independent",
                     extreme_gene=True, n_effect_genes=20,
                     seed=20261104, prefix="extr")
    # 5. Fully confounded condition/batch design (precheck must reject).
    make_confounded_dataset(data / "confounded", seed=20261105)
    _demo_plans(data, root / "plans")
    print(f"generated 5 scenarios under {data} and plans under {root / 'plans'}")
    return 0


def cmd_precheck(args) -> int:
    plan = load_plan(args.plan)
    ds = load_dataset(args.counts, args.samples, plan,
                      args.genes if args.genes else None)
    design = build_design(ds.samples, plan)
    out = {
        "status": "PASS",
        "n_genes": ds.n_genes,
        "n_samples": ds.n_samples,
        "design": plan.design,
        "design_columns": design.columns,
        "rank": design.rank,
        "residual_df": design.residual_df,
        "coding": design.coding,
        "exclusions": [e.__dict__ for e in ds.exclusions],
        "collapse_records": ds.collapse_records,
        "dropped_subjects": ds.dropped_subjects,
        "replicate_records": ds.replicate_records,
        "condition_counts": ds.samples.groupby("condition").size().to_dict(),
        "batch_counts": ds.samples.groupby("batch").size().to_dict(),
    }
    print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    return 0


def cmd_analyze(args) -> int:
    manifest = run_analysis(args.plan, args.counts, args.samples,
                            args.run_dir, args.genes if args.genes else None,
                            seed=args.seed)
    print(json.dumps({"status": "ok", "run_dir": str(args.run_dir),
                      "plan_hash": manifest["plan_hash"],
                      "fit": manifest["fit"],
                      "contrasts": manifest["contrasts"]}, indent=2))
    return 0


def cmd_verify(args) -> int:
    report = run_verification(args.plan, args.counts, args.samples, args.work)
    for c in report["checks"]:
        print(f"[{'PASS' if c['passed'] else 'FAIL'}] {c['name']}: {c['detail']}")
    return 0 if report["all_passed"] else 1


def cmd_simulate(args) -> int:
    df = run_all_simulations(args.out, n_replicates=args.replicates)
    cols = ["scenario", "n_replicates", "mean_detection_rate",
            "mean_false_positive_rate", "total_fit_failures"]
    print(df[cols].to_string(index=False))
    return 0


def cmd_view(args) -> int:
    run_dir = Path(args.run_dir)
    if args.what == "manifest":
        print((run_dir / "manifest.json").read_text(encoding="utf-8"))
    elif args.what == "contrast":
        df = pd.read_csv(run_dir / f"contrast_{args.name}.csv")
        print(df.to_string(index=False, max_rows=args.head or None))
    elif args.what == "qc":
        print(pd.read_csv(run_dir / "sample_qc_metrics.csv").to_string(index=False))
    elif args.what == "gene":
        path = run_dir / "genes" / f"{args.name}.html"
        print(f"open {path} in a browser; machine-readable rows:")
        fit = pd.read_csv(run_dir / "fit_diagnostics.csv")
        print(fit[fit["gene_id"] == args.name].to_string(index=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="deworkbench",
                                description="Reproducible synthetic count DE workbench")
    sub = p.add_subparsers(dest="command", required=True)

    g = sub.add_parser("generate", help="generate synthetic demo datasets")
    g.add_argument("--out", required=True)
    g.set_defaults(func=cmd_generate)

    g = sub.add_parser("precheck", help="validate inputs and design only")
    g.add_argument("--plan", required=True)
    g.add_argument("--counts", required=True)
    g.add_argument("--samples", required=True)
    g.add_argument("--genes", default=None)
    g.set_defaults(func=cmd_precheck)

    g = sub.add_parser("analyze", help="run full analysis pipeline")
    g.add_argument("--plan", required=True)
    g.add_argument("--counts", required=True)
    g.add_argument("--samples", required=True)
    g.add_argument("--genes", default=None)
    g.add_argument("--run-dir", required=True)
    g.add_argument("--seed", type=int, default=None)
    g.set_defaults(func=cmd_analyze)

    g = sub.add_parser("verify", help="independent verification checks")
    g.add_argument("--plan", required=True)
    g.add_argument("--counts", required=True)
    g.add_argument("--samples", required=True)
    g.add_argument("--work", required=True)
    g.set_defaults(func=cmd_verify)

    g = sub.add_parser("simulate", help="multi-seed performance study")
    g.add_argument("--out", required=True)
    g.add_argument("--replicates", type=int, default=20)
    g.set_defaults(func=cmd_simulate)

    g = sub.add_parser("view", help="read archived machine-readable results")
    g.add_argument("--run-dir", required=True)
    g.add_argument("--what", choices=["manifest", "contrast", "qc", "gene"],
                   required=True)
    g.add_argument("--name", default=None)
    g.add_argument("--head", type=int, default=None)
    g.set_defaults(func=cmd_view)
    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except WorkbenchError as exc:
        print(f"BLOCKED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"BLOCKED: missing file: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
