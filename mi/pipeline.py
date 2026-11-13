"""Run pipeline: diagnostics -> imputation -> fits -> pooling -> artifacts."""
from __future__ import annotations

import csv
import json
import platform
from pathlib import Path

import numpy as np
import scipy
import statsmodels

from .dataset import (
    CellState,
    STATE_LABELS,
    compute_eligibility,
    load_dataset,
)
from .errors import DiagnosticError
from .impute import PMM_DONORS, run_chains
from .model import (
    fit_complete_case,
    fit_completed,
    model_terms,
    pool_rubin,
)
from .plan import load_plan
from .report import render_report


def _fmt(x):
    """Shortest round-trip float formatting for CSV output."""
    return repr(float(x))


def _write_csv(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(header)
        w.writerows(rows)


def _distributions(ds, plan, chains):
    """Observed vs imputed distribution summaries per incomplete variable."""
    out = {}
    for v in ds.variables:
        obs = ds.values[v][ds.states[v] == CellState.OBSERVED]
        imp_cells = np.concatenate(
            [
                ch.completed[v][ds.states[v] == CellState.MISSING]
                for ch in chains
            ]
        ) if chains else np.array([])
        entry = {
            "observed": {
                "n": int(len(obs)),
                "mean": float(np.mean(obs)),
                "sd": float(np.std(obs, ddof=1)) if len(obs) > 1 else 0.0,
                "min": float(np.min(obs)),
                "max": float(np.max(obs)),
            }
        }
        if len(imp_cells):
            entry["imputed"] = {
                "n": int(len(imp_cells)),
                "mean": float(np.mean(imp_cells)),
                "sd": float(np.std(imp_cells, ddof=1)) if len(imp_cells) > 1 else 0.0,
                "min": float(np.min(imp_cells)),
                "max": float(np.max(imp_cells)),
            }
        out[v] = entry
    return out


def run_pipeline(plan_path, survey_path, applicability_path, out_dir) -> dict:
    """Execute the full pipeline and write all artifacts.

    Raises DiagnosticError on any pre-run diagnostic or run-time failure;
    nothing is silently dropped or coerced.
    """
    out = Path(out_dir)
    plan = load_plan(plan_path)
    ds = load_dataset(plan, survey_path, applicability_path)
    elig = compute_eligibility(ds, plan)

    chains = run_chains(ds, plan)
    failed = [c for c in chains if c.status == "failed"]
    if failed:
        raise DiagnosticError(
            [f"imputation chain {c.chain} failed: "
             f"{[e['detail'] for e in c.events if e['kind'] == 'chain_failure']}"
             for c in failed]
        )

    terms = model_terms(plan)
    fits = [
        fit_completed(ch.completed, ds, plan, elig, label=f"dataset_{ch.chain + 1}")
        for ch in chains
    ]
    pooled = pool_rubin(fits)
    cc_fit, cc_note = fit_complete_case(ds, plan, elig)

    # ---------------- artifacts ----------------
    (out / "completed").mkdir(parents=True, exist_ok=True)
    (out / "estimates").mkdir(parents=True, exist_ok=True)

    for ch in chains:
        rows = []
        for i, rid in enumerate(ds.row_ids):
            row = [rid]
            for v in ds.variables:
                if ds.states[v][i] == CellState.NOT_APPLICABLE:
                    row.append("")
                else:
                    row.append(_fmt(ch.completed[v][i]))
            rows.append(row)
        _write_csv(
            out / "completed" / f"completed_{ch.chain + 1}.csv",
            ["row_id"] + ds.variables,
            rows,
        )

    _write_csv(
        out / "cell_states.csv",
        ["row_id", "column", "state"],
        [
            [rid, v, STATE_LABELS[CellState(ds.states[v][i])]]
            for i, rid in enumerate(ds.row_ids)
            for v in ds.variables
        ],
    )

    _write_csv(
        out / "analysis_rows.csv",
        ["row_id", "eligible", "reason"],
        [
            [rid, "true" if elig.eligible[i] else "false", elig.reasons[i]]
            for i, rid in enumerate(ds.row_ids)
        ],
    )

    _write_csv(
        out / "estimates" / "per_dataset_estimates.csv",
        ["dataset", "term", "estimate", "std_error", "n", "df_resid"],
        [
            [f.label, term, _fmt(f.params[j]), _fmt(f.std_errors[j]), f.n, f.df_resid]
            for f in fits
            for j, term in enumerate(f.terms)
        ],
    )
    vcov_doc = {
        "terms": terms,
        "datasets": {
            f.label: {
                "n": f.n,
                "df_resid": f.df_resid,
                "params": [float(x) for x in f.params],
                "vcov": [[float(x) for x in row] for row in f.vcov],
                "row_ids": f.row_ids,
            }
            for f in fits
        },
    }
    (out / "estimates" / "per_dataset_vcov.json").write_text(
        json.dumps(vcov_doc, indent=2) + "\n", encoding="utf-8"
    )

    (out / "estimates" / "pooled.json").write_text(
        json.dumps(pooled, indent=2) + "\n", encoding="utf-8"
    )
    _write_csv(
        out / "estimates" / "pooled.csv",
        ["term", "estimate", "std_error", "within_var", "between_var",
         "total_var", "df", "fmi", "ci_lo", "ci_hi", "p_value"],
        [
            [r["term"], _fmt(r["estimate"]), _fmt(r["std_error"]),
             _fmt(r["within_var"]), _fmt(r["between_var"]), _fmt(r["total_var"]),
             "inf" if r["df_infinite"] else _fmt(r["df"]),
             _fmt(r["fmi"]), _fmt(r["ci_lo"]), _fmt(r["ci_hi"]),
             _fmt(r["p_value"])]
            for r in pooled["results"]
        ],
    )

    if cc_fit is not None:
        _write_csv(
            out / "estimates" / "complete_case.csv",
            ["term", "estimate", "std_error", "n", "df_resid"],
            [
                [term, _fmt(cc_fit.params[j]), _fmt(cc_fit.std_errors[j]),
                 cc_fit.n, cc_fit.df_resid]
                for j, term in enumerate(cc_fit.terms)
            ],
        )
        cc_rows = cc_fit.row_ids
    else:
        (out / "estimates" / "complete_case.csv").write_text(
            "complete-case fit not available: " + cc_note + "\n", encoding="utf-8"
        )
        cc_rows = []

    chains_doc = {
        "chains": [
            {
                "chain": ch.chain + 1,
                "status": ch.status,
                "events": ch.events,
                "traces": ch.traces,
            }
            for ch in chains
        ]
    }
    (out / "chains.json").write_text(
        json.dumps(chains_doc, indent=2) + "\n", encoding="utf-8"
    )

    distributions = _distributions(ds, plan, chains)

    # ---------------- limitations ----------------
    limitations = [
        "插补值是基于模型的随机抽取,不是真实观测;不得当作实测数据分析。",
        "本流程不识别缺失机制;结论成立需要标准的可忽略性(MAR 型)假设。",
        "含结构性不适用的行按计划策略排除在分析样本之外;其普通缺失单元仍"
        "在完成数据集中插补(预测列不适用时为边际随机抽取),但不进入任何拟合。",
        "合并 MI 结果为正式结果;完整病例拟合仅供对照,未按显著性取舍。",
        "链轨迹仅供检查;达到计划轮数不构成收敛证据。",
    ]
    flagged = [c for c in chains if c.events]
    if flagged:
        limitations.append(
            "部分链出现事件标记(见 chains.json);相关估计应谨慎对待。"
        )
    max_fmi = max((r["fmi"] for r in pooled["results"]), default=0.0)
    if max_fmi > 0.3:
        limitations.append(
            f"至少一个项的缺失信息比例(FMI)达 {max_fmi:.2f};插补间不确定"
            "性较大,可能需要更多完成数据集。"
        )
    if cc_fit is not None and cc_fit.n < elig.n_eligible:
        limitations.append(
            f"完整病例对照仅用 {cc_fit.n} / {elig.n_eligible} 合格行;"
            "若缺失非 MCAR,该对照可能有偏。"
        )

    diagnostics = {
        "plan": {
            "variables": {
                v.name: {"range": [v.lo, v.hi]} for v in plan.variables
            },
            "allowed_predictors": list(plan.allowed_predictors),
            "response": plan.response,
            "covariates": list(plan.covariates),
            "intercept": plan.intercept,
            "completed_datasets": plan.n_datasets,
            "iterations": plan.n_iterations,
            "seed": plan.seed,
        },
        "dataset": {
            "n_rows": ds.n_rows,
            "variables": {v: ds.state_counts(v) for v in ds.variables},
        },
        "eligibility": {
            "n_eligible": elig.n_eligible,
            "n_excluded": ds.n_rows - elig.n_eligible,
            "excluded": [
                {"row_id": rid, "reason": elig.reasons[i]}
                for i, rid in enumerate(ds.row_ids)
                if not elig.eligible[i]
            ],
        },
        "chains": {
            "n_chains": len(chains),
            "statuses": {str(c.chain + 1): c.status for c in chains},
            "n_flagged": len(flagged),
        },
        "distributions": distributions,
        "complete_case": {
            "status": cc_note,
            "n": cc_fit.n if cc_fit else 0,
            "row_ids": cc_rows,
        },
        "limitations": limitations,
    }
    (out / "diagnostics.json").write_text(
        json.dumps(diagnostics, indent=2) + "\n", encoding="utf-8"
    )

    design = {
        "format_version": 1,
        "plan": plan.raw,
        "canonical_row_order": "rows sorted ascending by row_id; all random "
        "streams are functions of (seed, chain, sweep, variable, purpose), "
        "so input row permutation cannot change results",
        "variables": ds.variables,
        "terms": terms,
        "eligibility_rule": (
            "row eligible iff response is applicable+observed and every "
            "analysis covariate is applicable; ordinary-missing covariates "
            "are imputed, not excluded"
        ),
        "imputation": {
            "method": "fully conditional specification: Bayesian linear "
            "regression draws + predictive mean matching",
            "pmm_donors": PMM_DONORS,
            "sweep_order": "variables by ascending missing count, then "
            "declaration order",
            "chains": plan.n_datasets,
            "iterations": plan.n_iterations,
            "seed": plan.seed,
            "rng": "numpy SeedSequence([seed, chain, sweep, variable_index, "
            "purpose]); independent stream per chain",
            "range_handling": "PMM returns observed donor values (validated "
            "against plan ranges at load); defensive clip to declared range "
            "counted as an event",
        },
        "pooling": pooled["method"],
        "environment": {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "scipy": scipy.__version__,
            "statsmodels": statsmodels.__version__,
        },
    }
    (out / "design.json").write_text(
        json.dumps(design, indent=2) + "\n", encoding="utf-8"
    )

    report = render_report(
        plan=plan,
        ds=ds,
        elig=elig,
        chains=chains,
        pooled=pooled,
        cc_fit=cc_fit,
        cc_note=cc_note,
        distributions=distributions,
        limitations=limitations,
        paths={
            "plan": str(plan_path),
            "survey": str(survey_path),
            "applicability": str(applicability_path),
            "out": str(out),
        },
    )
    (out / "report.md").write_text(report, encoding="utf-8")

    return {
        "plan": plan,
        "dataset": ds,
        "eligibility": elig,
        "chains": chains,
        "fits": fits,
        "pooled": pooled,
        "complete_case": cc_fit,
        "out_dir": out,
    }
