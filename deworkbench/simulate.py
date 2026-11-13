"""Repeated-simulation performance reporting.

For each scenario the full pipeline is rerun over many independent seeds. We
report empirical detection power per implanted effect size and the false
positive rate on truly null genes, with binomial uncertainty. Seeds are fixed
ahead of time and every replicate is reported — there is no mechanism for
selecting favorable runs, and finite-sample non-detection is expected.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from .design import build_design
from .dispersion import moderate_dispersions, refit_with_moderated_alpha
from .filtering import apply_filter
from .fitting import fit_gene
from .generate import generate_dataset
from .inference import run_contrasts
from .normalize import median_of_ratios
from .plan import parse_plan
from .ql import quasi_likelihood


def _run_once(data_dir: Path, plan_dict: dict, contrast_name: str):
    plan = parse_plan(plan_dict)
    from .io import load_dataset
    ds = load_dataset(data_dir / "counts.csv", data_dir / "samples.csv", plan)
    design = build_design(ds.samples, plan)
    fr = apply_filter(ds.counts, ds.samples, plan)
    norm = median_of_ratios(ds.counts, fr.keep)
    fits = [None] * ds.n_genes
    for i in range(ds.n_genes):
        if fr.status[i] == "tested":
            fits[i] = fit_gene(str(ds.gene_ids[i]), ds.counts[i], design,
                               norm.log_size_factors)
    if plan.moderate_dispersion and int(fr.keep.sum()) >= 8:
        base_means = (ds.counts / norm.size_factors[None, :]).mean(axis=1)
        mod = moderate_dispersions(fits, base_means, design.residual_df)
        refit_with_moderated_alpha(fits, mod, ds.counts, design.X,
                                   norm.log_size_factors, design.residual_df,
                                   use_pearson_correction=True)
    ql = quasi_likelihood(fits, fr.status, ds.gene_ids, design.residual_df)
    res = run_contrasts(plan, design, fits, fr.status, ds.gene_ids, ql=ql)
    table = next(r for r in res if r.contrast_name == contrast_name).table
    return table, fr


def simulate_scenario(out_dir: str | Path, *, scenario: str,
                      n_replicates: int = 20, base_seed: int = 1000,
                      n_genes: int = 250, n_effect_genes: int = 25,
                      design: str = "independent",
                      n_per_condition: int = 8,
                      paired: bool = False,
                      effect_log2fc: float = 1.0,
                      extreme_gene: bool = False,
                      alpha_level: float = 0.05) -> dict:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    seeds = [base_seed + k for k in range(n_replicates)]
    plan_dict = {
        "design": "paired" if paired else "independent",
        "conditions": ["control", "treated"],
        "contrasts": [{"name": "trt_vs_ctrl", "numerator": "treated",
                       "denominator": "control"}],
        "filter_rule": {"type": "cpm", "min_value": 1.0},
        "random_seed": 0,
    }
    rows, per_rep = [], []
    for rep, seed in enumerate(seeds):
        data_dir = out_dir / f"rep_{rep:03d}_data"
        gen = generate_dataset(
            data_dir, design=plan_dict["design"], n_genes=n_genes,
            n_per_condition=n_per_condition, n_effect_genes=n_effect_genes,
            log2fc_range=(effect_log2fc, effect_log2fc),
            extreme_gene=extreme_gene, seed=seed, prefix=f"sim{rep}")
        truth = gen.truth
        table, fr = _run_once(data_dir, plan_dict, "trt_vs_ctrl")
        effect_set = {g for g, fc in truth["effect_genes_log2fc"].items()
                      if abs(fc) > 0}
        t = table.set_index("gene_id")
        tested = t[t["status"] == "tested"]
        sig = tested["p_value_bh"] < alpha_level

        null_genes = [g for g in tested.index if g not in effect_set]
        effect_genes = [g for g in effect_set if g in tested.index]
        fpr = float(sig.loc[null_genes].mean()) if null_genes else np.nan
        power = float(sig.loc[effect_genes].mean()) if effect_genes else np.nan
        # Direction accuracy among detected effect genes.
        det = sig.loc[effect_genes]
        detected = det[det].index
        if len(detected):
            true_fc = pd.Series(truth["effect_genes_log2fc"])
            dir_ok = float(np.mean(
                np.sign(tested.loc[detected, "log2_fold_change"])
                == np.sign(true_fc.loc[detected])))
        else:
            dir_ok = np.nan
        per_rep.append({
            "replicate": rep, "seed": seed,
            "n_tested": len(tested),
            "n_fit_failed": int((t["status"] == "fit_failed").sum()),
            "false_positive_rate": fpr,
            "detection_rate": power,
            "direction_accuracy_detected": dir_ok,
        })
        # Per-gene rows for power-vs-effect summaries.
        for g in effect_genes:
            rows.append({"replicate": rep, "gene_id": g,
                         "true_log2fc": truth["effect_genes_log2fc"][g],
                         "detected": bool(sig.loc[g]),
                         "estimated_log2fc": tested.loc[g, "log2_fold_change"]})

    rep_df = pd.DataFrame(per_rep)
    rep_df.to_csv(out_dir / "per_replicate.csv", index=False)
    pd.DataFrame(rows).to_csv(out_dir / "per_gene_detection.csv", index=False)

    def _wilson(p, n):
        if n == 0 or np.isnan(p):
            return [np.nan, np.nan]
        z = 1.959963984540054
        denom = 1 + z ** 2 / n
        center = (p + z ** 2 / (2 * n)) / denom
        half = z * np.sqrt(p * (1 - p) / n + z ** 2 / (4 * n ** 2)) / denom
        return [center - half, center + half]

    fpr_vals = rep_df["false_positive_rate"].dropna()
    pow_vals = rep_df["detection_rate"].dropna()
    n_null_avg = float((rep_df["n_tested"].mean() - n_effect_genes))
    summary = {
        "scenario": scenario,
        "n_replicates": n_replicates,
        "seeds": seeds,
        "alpha_bh": alpha_level,
        "implanted_log2fc": effect_log2fc,
        "mean_detection_rate": float(pow_vals.mean()),
        "detection_rate_sd": float(pow_vals.std(ddof=1)),
        "mean_false_positive_rate": float(fpr_vals.mean()),
        "fpr_sd": float(fpr_vals.std(ddof=1)),
        "fpr_wilson95_pooled": _wilson(
            float(fpr_vals.mean()), max(int(round(n_null_avg)), 1)),
        "mean_n_tested": float(rep_df["n_tested"].mean()),
        "total_fit_failures": int(rep_df["n_fit_failed"].sum()),
        "note": ("All predeclared seeds are included; finite-sample power below 1 "
                 "is expected and no seed was selected post hoc."),
    }
    (out_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return summary


def run_all_simulations(root: str | Path, n_replicates: int = 20) -> pd.DataFrame:
    root = Path(root)
    common = dict(n_genes=150)
    scenarios = [
        ("null_balanced", dict(effect_log2fc=0.0, n_effect_genes=0,
                               design="independent", **common)),
        ("known_foldchange_independent",
         dict(effect_log2fc=1.0, design="independent", **common)),
        ("paired_subject_effects",
         dict(effect_log2fc=1.0, paired=True, design="paired", **common)),
        ("extreme_count_gene",
         dict(effect_log2fc=1.0, extreme_gene=True, **common)),
        ("small_effect", dict(effect_log2fc=0.35, **common)),
    ]
    summaries = []
    for name, kw in scenarios:
        summaries.append(simulate_scenario(
            root / name, scenario=name, n_replicates=n_replicates, **kw))
    df = pd.DataFrame(summaries)
    df.to_csv(root / "simulation_summary.csv", index=False)
    return df
