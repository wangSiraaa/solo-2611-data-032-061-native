"""End-to-end orchestration: plan -> validate -> precheck -> QC -> filter ->
normalize -> NB fit -> contrasts -> report. All outputs land in a single
self-contained run directory so the analysis reruns identically later.
"""
from __future__ import annotations

import json
import logging
import platform
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from . import __version__
from .design import build_design
from .dispersion import (moderate_dispersions, pearson_damping,
                         refit_with_moderated_alpha)
from .filtering import apply_filter
from .fitting import fit_gene
from .inference import run_contrasts, normalized_expression
from .io import load_dataset
from .normalize import median_of_ratios
from .plan import Plan, load_plan
from .plots import (plot_dispersion, plot_gene_counts, plot_library_sizes,
                    plot_ma, plot_pca, plot_volcano, plot_zero_fraction)
from .qc import sample_qc
from .ql import quasi_likelihood
from .reporting import (write_contrast_page, write_gene_index, write_gene_page,
                        write_run_index)

log = logging.getLogger("deworkbench")


def _dependency_versions() -> dict:
    import matplotlib
    import scipy
    import statsmodels
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "deworkbench": __version__,
        "numpy": np.__version__,
        "scipy": scipy.__version__,
        "pandas": pd.__version__,
        "statsmodels": statsmodels.__version__,
        "matplotlib": matplotlib.__version__,
    }


def run_analysis(plan_path: str | Path, counts_path: str | Path,
                 samples_path: str | Path, run_dir: str | Path,
                 genes_path: str | Path | None = None,
                 seed: int | None = None) -> dict:
    run_dir = Path(run_dir)
    (run_dir / "figures").mkdir(parents=True, exist_ok=True)
    (run_dir / "genes").mkdir(exist_ok=True)

    plan = load_plan(plan_path)
    if seed is not None:
        plan.random_seed = int(seed)
    np.random.seed(plan.random_seed)  # deterministic fallback paths / plots

    # ---- 1. Load + hard validation (ID-based alignment) ---------------------
    ds = load_dataset(counts_path, samples_path, plan, genes_path)
    log.info("loaded %d genes x %d samples", ds.n_genes, ds.n_samples)

    # ---- 2. Design identifiability precheck ---------------------------------
    design = build_design(ds.samples, plan)
    log.info("design rank %d / %d columns; residual df %d",
             design.rank, len(design.columns), design.residual_df)
    design.frame.to_csv(run_dir / "design_matrix.csv", index=False)
    with open(run_dir / "design_coding.json", "w", encoding="utf-8") as fh:
        json.dump({"columns": design.columns,
                   "term_of_column": design.term_of_column,
                   "rank": design.rank,
                   "residual_df": design.residual_df,
                   "coding": design.coding}, fh, indent=2, default=str)

    # ---- 3. QC on every retained sample (reporting only) --------------------
    # Size factors need a first pass using all non-trivial genes; recompute on
    # the filtered set afterwards for the final fit.
    prelim_eligible = (ds.counts > 0).sum(axis=1) >= max(2, ds.n_samples // 3)
    norm0 = median_of_ratios(ds.counts, prelim_eligible)
    qc = sample_qc(ds.counts, ds.samples, norm0.size_factors)
    qc.metrics.to_csv(run_dir / "sample_qc_metrics.csv", index=False)
    qc.flags.to_csv(run_dir / "sample_qc_flags.csv", index=False)
    qc.pca.to_csv(run_dir / "sample_pca.csv", index=False)
    plot_library_sizes(qc, run_dir / "figures" / "library_sizes.png")
    plot_zero_fraction(qc, run_dir / "figures" / "zero_fraction.png")
    plot_pca(qc, run_dir / "figures" / "pca.png")

    # ---- 4. Frozen low-expression filter (before any fit/p-value) -----------
    fr = apply_filter(ds.counts, ds.samples, plan)
    pd.DataFrame({"gene_id": ds.gene_ids, "status": fr.status,
                  "rule": fr.rule_description}).to_csv(
        run_dir / "filter_decisions.csv", index=False)
    log.info("filter: %d tested, %d low-expression, %d all-zero",
             fr.n_kept, fr.n_filtered, fr.n_all_zero)

    # ---- 5. Final normalization on the tested family ------------------------
    norm = median_of_ratios(ds.counts, fr.keep)
    pd.DataFrame({
        "sample_id": ds.sample_ids,
        "size_factor": norm.size_factors,
        "log_size_factor": norm.log_size_factors,
        "library_size": ds.library_sizes(),
    }).to_csv(run_dir / "normalization_factors.csv", index=False)
    with open(run_dir / "normalization_diagnostics.json", "w",
              encoding="utf-8") as fh:
        json.dump({**norm.diagnostics, "method": norm.method}, fh, indent=2)

    norm_expr = normalized_expression(ds.counts, norm.size_factors,
                                      ds.gene_ids, ds.sample_ids)
    norm_expr.to_csv(run_dir / "normalized_expression.csv")

    # ---- 6. Per-gene NB GLM -------------------------------------------------
    fits = [None] * ds.n_genes
    fit_rows, mu_matrix = [], np.full_like(ds.counts, np.nan, dtype=float)
    for i in range(ds.n_genes):
        if fr.status[i] != "tested":
            fit_rows.append({"gene_id": ds.gene_ids[i], "status": fr.status[i],
                             "converged": False, "method": "",
                             "dispersion": np.nan, "pearson_chi2": np.nan,
                             "deviance": np.nan, "iterations": 0,
                             "failure_reason": ""})
            continue
        f = fit_gene(str(ds.gene_ids[i]), ds.counts[i], design,
                     norm.log_size_factors)
        fits[i] = f
        if f.converged:
            mu_matrix[i] = f.mu
            fit_rows.append({"gene_id": f.gene_id, "status": "tested",
                             "converged": True, "method": f.method,
                             "dispersion": f.dispersion,
                             "pearson_chi2": f.pearson_chi2,
                             "deviance": f.deviance,
                             "iterations": f.iterations,
                             "failure_reason": ""})
        else:
            fit_rows.append({"gene_id": f.gene_id, "status": "fit_failed",
                             "converged": False, "method": f.method,
                             "dispersion": np.nan, "pearson_chi2": np.nan,
                             "deviance": np.nan, "iterations": f.iterations,
                             "failure_reason": f.failure_reason})
    fit_df = pd.DataFrame(fit_rows)
    fit_df.to_csv(run_dir / "fit_diagnostics.csv", index=False)
    pd.DataFrame(mu_matrix, index=ds.gene_ids,
                 columns=ds.sample_ids).to_csv(run_dir / "fitted_values.csv")
    n_fail = int((fit_df["status"] == "fit_failed").sum())
    log.info("fitting complete: %d failed (reported, not zero-filled)", n_fail)

    # ---- 6b. Dispersion moderation (frozen-plan controlled) ----------------
    base_mean_all = norm_expr.to_numpy().mean(axis=1)
    moderation_info = {"applied": False}
    if plan.moderate_dispersion and int(fr.keep.sum()) >= 8:
        mod = moderate_dispersions(fits, base_mean_all, design.residual_df)
        refit_with_moderated_alpha(fits, mod, ds.counts, design.X,
                                   norm.log_size_factors, design.residual_df,
                                   use_pearson_correction=True)
        pd.DataFrame({
            "gene_id": ds.gene_ids,
            "dispersion_ml": mod.raw_alpha,
            "dispersion_trend": mod.trend_alpha,
            "dispersion_moderated": mod.moderated_alpha,
            "shrink_weight": mod.weights,
        }).to_csv(run_dir / "dispersion_moderation.csv", index=False)
        moderation_info = {
            "applied": True,
            "prior_observations": mod.prior_observations,
            "prior_log_variance": mod.prior_log_variance,
            "pearson_correction_damping": pearson_damping(
                design.residual_df),
            "note": mod.method_note,
        }
        # Refresh fitted means/diagnostics rows after refit.
        for i, f in enumerate(fits):
            if f is not None and f.converged:
                mu_matrix[i] = f.mu
                fit_df.loc[i, ["method", "dispersion", "pearson_chi2",
                               "deviance", "iterations"]] = [
                    f.method, f.dispersion, f.pearson_chi2, f.deviance,
                    f.iterations]
        fit_df.to_csv(run_dir / "fit_diagnostics.csv", index=False)
        pd.DataFrame(mu_matrix, index=ds.gene_ids,
                     columns=ds.sample_ids).to_csv(
            run_dir / "fitted_values.csv", index=True)
        log.info("dispersion moderation applied: %s", mod.method_note)

    # ---- 7. Contrasts (Wald-t primary, QL F diagnostic) + BH adjustment ----
    ql = quasi_likelihood(fits, fr.status, ds.gene_ids, design.residual_df)
    pd.DataFrame({
        "gene_id": ds.gene_ids,
        "ql_dispersion_raw": ql.ql_raw,
        "ql_dispersion_moderated": ql.ql_moderated,
    }).to_csv(run_dir / "ql_dispersions.csv", index=False)
    moderation_info["ql"] = {
        "df_prior": ql.df_prior, "df_total": ql.df_total,
        "common_ql": ql.common_ql, "note": ql.note}
    contrast_results = run_contrasts(plan, design, fits, fr.status,
                                      ds.gene_ids, ql=ql)
    base_mean = pd.Series(
        np.nanmean(np.where(fr.keep[:, None], norm_expr.to_numpy(), np.nan),
                   axis=1), index=ds.gene_ids)
    contrast_tables = {}
    for cr in contrast_results:
        cr.table.to_csv(run_dir / f"contrast_{cr.contrast_name}.csv", index=False)
        with open(run_dir / f"contrast_{cr.contrast_name}_meta.json", "w",
                  encoding="utf-8") as fh:
            json.dump(cr.table.attrs["contrast"], fh, indent=2)
        contrast_tables[cr.contrast_name] = cr.table
        plot_volcano(cr.table, cr.contrast_name,
                     run_dir / "figures" / f"volcano_{cr.contrast_name}.png")
        plot_ma(cr.table, cr.contrast_name,
                run_dir / "figures" / f"ma_{cr.contrast_name}.png", base_mean)
        write_contrast_page(run_dir, cr.contrast_name, cr.table,
                            cr.table.attrs["contrast"])
    dispersions = fit_df["dispersion"].to_numpy()
    plot_dispersion(base_mean, dispersions, ds.gene_ids,
                    run_dir / "figures" / "dispersion.png")

    # ---- 8. Per-gene review pages ------------------------------------------
    status_by = {name: tab[["gene_id", "status"]].rename(
        columns={"status": f"status:{name}"})
        for name, tab in contrast_tables.items()}
    write_gene_index(run_dir, ds.gene_ids, contrast_tables)
    sample_base = ds.samples[["sample_id", "condition", "batch"]].copy()
    if "subject_id" in ds.samples.columns:
        sample_base["subject_id"] = ds.samples["subject_id"]
    for i, g in enumerate(ds.gene_ids):
        sample_table = sample_base.copy()
        sample_table["raw_count"] = ds.counts[i]
        sample_table["size_factor"] = norm.size_factors
        sample_table["normalized_count"] = ds.counts[i] / norm.size_factors
        if np.isfinite(mu_matrix[i]).all():
            sample_table["fitted_mean"] = mu_matrix[i]
            plot_gene_counts(str(g), ds.counts[i], mu_matrix[i], ds.samples,
                             norm.size_factors,
                             run_dir / "figures" / f"gene_{g}.png")
            figure = True
        else:
            figure = False
        diag = fit_df.iloc[i].to_dict()
        ctab = pd.concat([
            tab.loc[tab["gene_id"] == g,
                    ["log2_fold_change", "se_log2fc", "ci95_low_log2fc",
                     "ci95_high_log2fc", "p_value", "p_value_bh", "status"]]
            .assign(contrast=name)
            for name, tab in contrast_tables.items()], ignore_index=True)
        metas = [tab.attrs["contrast"] for tab in
                 [contrast_tables[n] for n in contrast_tables]]
        # attach stored meta (attrs may be lost on csv round trip)
        metas = []
        for name in contrast_tables:
            with open(run_dir / f"contrast_{name}_meta.json", encoding="utf-8") as fh:
                metas.append(json.load(fh))
        write_gene_page(run_dir, str(g), {
            "diagnostics": diag,
            "sample_table": sample_table,
            "figure": figure,
            "contrast_table": ctab,
            "contrast_meta": metas,
        })

    # ---- 9. Archive: manifest, plan, versions, exclusions ------------------
    manifest = {
        "run_id": run_dir.name,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "plan_hash": plan.freeze_hash(),
        "design": plan.design,
        "encoding": plan.encoding,
        "random_seed": plan.random_seed,
        "input_summary": {
            "counts_file": str(counts_path),
            "samples_file": str(samples_path),
            "genes_file": str(genes_path) if genes_path else None,
            "n_genes": int(ds.n_genes),
            "n_samples_analyzed": int(ds.n_samples),
            "conditions": plan.conditions,
            "batches": sorted(ds.samples["batch"].unique().tolist()),
            "n_subjects": (int(ds.samples["subject_id"].nunique())
                           if "subject_id" in ds.samples.columns else None),
        },
        "exclusions": ([e.__dict__ for e in ds.exclusions]
                       + [{"sample_id": sid,
                           "reason": (f"technical replicate collapse into "
                                      f"{r['resulting_sample_id']} "
                                      f"(subject {r['subject_id']}, {r['condition']})"),
                           "stage": "duplicate_subject_policy=sum"}
                          for r in ds.collapse_records
                          for sid in r["merged_sample_ids"][1:]]),
        "dropped_subjects": ds.dropped_subjects,
        "replicate_records": ds.replicate_records,
        "filter": {"rule": fr.rule_description, "n_tested": fr.n_kept,
                   "n_filtered": fr.n_filtered, "n_all_zero": fr.n_all_zero,
                   "family_note": fr.multiple_testing_family_note},
        "normalization": norm.diagnostics,
        "dispersion_moderation": moderation_info,
        "fit": {"n_failed": n_fail,
                "n_converged": int((fit_df["converged"]).sum())},
        "contrasts": {name: {
            "bh_family_size": int((tab["status"] == "tested").sum()),
            "n_bh_sig_0.05": int((tab["p_value_bh"] < 0.05).sum()),
            "n_fit_failed": int((tab["status"] == "fit_failed").sum()),
        } for name, tab in contrast_tables.items()},
        "dependency_versions": _dependency_versions(),
        "outputs": sorted(p.name for p in run_dir.iterdir() if p.is_file()),
    }
    (run_dir / "plan_frozen.json").write_text(
        json.dumps(plan.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8")
    write_run_index(run_dir, manifest, qc, fr, contrast_tables, design.columns)
    log.info("run archived at %s (plan hash %s)", run_dir, plan.freeze_hash())
    return manifest
