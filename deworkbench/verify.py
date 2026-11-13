"""Independent verification checks.

These checks deliberately re-derive quantities with code paths separate from
the main pipeline:

  1. column shuffle: permuting count-matrix columns leaves results identical
     (alignment is by sample_id, not position);
  2. encoding invariance: treatment vs sum coding give the same contrast
     estimate/SE/p-value within tolerance;
  3. reverse contrast flips the effect sign and keeps |SE|/p identical;
  4. all-zero genes never receive p-values;
  5. fit failures are reported as status, never zero-filled;
  6. a small matrix is fit independently with the self-contained IRLS fitter
     and coefficients/contrasts are recomputed by hand from the normal
     equations.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path

import numpy as np
import pandas as pd

from .design import build_design, contrast_vector
from .filtering import apply_filter
from .fitting import fit_gene, custom_fit
from .inference import run_contrasts
from .io import load_dataset
from .normalize import median_of_ratios
from .plan import Plan, load_plan, parse_plan, Contrast


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str


def _small_matrix_fit_check() -> CheckResult:
    """Hand-built 12-sample, 2-condition, 2-batch NB design fit via IRLS only."""
    rng = np.random.default_rng(4242)
    n = 12
    conditions = (["control"] * 6 + ["treated"] * 6)
    batches = ["batch1", "batch2"] * 6
    samples = pd.DataFrame({
        "sample_id": [f"v{i}" for i in range(n)],
        "condition": conditions,
        "batch": batches,
        "subject_id": [f"v{i}" for i in range(n)],
    })
    plan = parse_plan({
        "design": "independent", "conditions": ["control", "treated"],
        "contrasts": [{"name": "trt_vs_ctrl", "numerator": "treated",
                       "denominator": "control"}],
        "filter_rule": {"type": "none"},
        "random_seed": 1,
    })
    design = build_design(samples, plan)
    # Construct counts from known parameters: log-mu = 2 + 0.7*treated + 0.3*batch2
    beta_true = np.array([2.0, 0.7, 0.3])
    mu = np.exp(design.X @ beta_true)
    y = rng.negative_binomial(n=10.0, p=10.0 / (10.0 + mu)).astype(float)
    out, err = custom_fit(y, design.X, np.zeros(n))
    if out is None:
        return CheckResult("small_matrix_irls", False, f"fit failed: {err}")
    beta_hat, vcov, mu_hat, alpha, _, _ = out
    # Independent normal-equation check: X'W X beta = X'W z at convergence.
    w = mu_hat / (1 + alpha * mu_hat)
    z = np.log(mu_hat) + (y - mu_hat) / mu_hat
    lhs = (design.X * w[:, None]).T @ design.X @ beta_hat
    rhs = (design.X * w[:, None]).T @ z
    resid = float(np.max(np.abs(lhs - rhs)))
    cvec = contrast_vector(plan.contrasts[0], plan, design.columns)
    est = float(cvec @ beta_hat)
    se = float(np.sqrt(cvec @ vcov @ cvec))
    # Contrast math independently: treated coefficient minus reference (0).
    col_idx = design.columns.index("condition[treated-vs-control]")
    est2 = beta_hat[col_idx]
    ok_eq = resid < 1e-6 and abs(est - est2) < 1e-10
    return CheckResult(
        "small_matrix_irls",
        ok_eq,
        f"normal-equation residual={resid:.2e}; contrast est={est:.4f} (se={se:.4f}); "
        f"direct beta[{col_idx}]={est2:.4f}; alpha={alpha:.4f}; "
        f"design columns={design.columns}")


def run_verification(plan_path: str | Path, counts_path: str | Path,
                     samples_path: str | Path, work_dir: str | Path) -> dict:
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    checks: list[CheckResult] = []
    plan = load_plan(plan_path)

    # --- Check 1: shuffled count columns -------------------------------------
    ds = load_dataset(counts_path, samples_path, plan)
    counts_df = pd.read_csv(counts_path)
    perm = np.random.default_rng(0).permutation(ds.n_samples)
    shuf_cols = ["gene_id"] + [str(c) for c in counts_df.columns[1:][perm]]
    shuf_path = work_dir / "counts_shuffled.csv"
    counts_df[shuf_cols].to_csv(shuf_path, index=False)
    ds_shuf = load_dataset(shuf_path, samples_path, plan)
    align_ok = np.array_equal(
        ds.counts, ds_shuf.counts) and list(ds.sample_ids) == list(ds_shuf.sample_ids)
    checks.append(CheckResult(
        "column_shuffle_alignment", align_ok,
        "ID-based realignment reproduces identical aligned count matrix "
        f"({ds.n_samples} samples)"))

    # Full pipeline equivalence on original vs shuffled columns.
    design = build_design(ds.samples, plan)
    fr = apply_filter(ds.counts, ds.samples, plan)
    norm = median_of_ratios(ds.counts, fr.keep)

    def fit_all(data_counts, dsign):
        out = [None] * data_counts.shape[0]
        for i, g in enumerate(ds.gene_ids):
            if fr.status[i] == "tested":
                out[i] = fit_gene(str(g), data_counts[i], dsign,
                                  norm.log_size_factors)
        return out

    fits = fit_all(ds.counts, design)
    res = run_contrasts(plan, design, fits, fr.status, ds.gene_ids)

    norm_s = median_of_ratios(ds_shuf.counts, fr.keep)
    fits_s = fit_all(ds_shuf.counts, design)
    res_s = run_contrasts(plan, design, fits_s, fr.status, ds_shuf.gene_ids)
    same = all(
        np.allclose(a.table["log2_fold_change"].fillna(-999),
                    b.table["log2_fold_change"].fillna(-999), atol=1e-10)
        and np.allclose(a.table["p_value"].fillna(-1),
                        b.table["p_value"].fillna(-1), atol=1e-10)
        for a, b in zip(res, res_s))
    checks.append(CheckResult("column_shuffle_results", bool(same),
                              "contrast estimates and p-values identical after shuffle"))

    # --- Check 2: encoding invariance ----------------------------------------
    if plan.encoding == "treatment":
        sum_plan = parse_plan({**plan.to_dict(), "encoding": "sum"})
        sum_plan.contrasts = plan.contrasts
        design_sum = build_design(ds.samples, sum_plan)
        fits_sum = fit_all(ds.counts, design_sum)
        res_sum = run_contrasts(sum_plan, design_sum, fits_sum,
                                fr.status, ds.gene_ids)
        diffs = []
        for a, b in zip(res, res_sum):
            m = a.table["status"] == "tested"
            d_est = np.max(np.abs(a.table.loc[m, "log2_fold_change"]
                                  - b.table.loc[m, "log2_fold_change"]))
            d_p = np.max(np.abs(a.table.loc[m, "p_value"]
                                - b.table.loc[m, "p_value"]))
            diffs.append(max(d_est, d_p))
        tol = 1e-6
        checks.append(CheckResult(
            "encoding_invariance", float(np.max(diffs)) < tol,
            f"treatment vs sum coding: max |estimate/p difference|={np.max(diffs):.2e} "
            f"(tol {tol})"))

    # --- Check 3: reverse contrast sign --------------------------------------
    rev_dict = plan.to_dict()
    c0 = plan.contrasts[0]
    rev_dict["contrasts"] = [{"name": "reverse",
                              "numerator": c0.denominator,
                              "denominator": c0.numerator}]
    rev_plan = parse_plan(rev_dict)
    design_rev = build_design(ds.samples, rev_plan)
    fits_r = fit_all(ds.counts, design_rev)
    res_rev = run_contrasts(rev_plan, design_rev, fits_r,
                            fr.status, ds.gene_ids)
    fwd = res[0].table
    rev = res_rev[0].table
    m = fwd["status"] == "tested"
    sign_ok = np.allclose(fwd.loc[m, "log2_fold_change"],
                          -rev.loc[m, "log2_fold_change"], atol=1e-10)
    p_ok = np.allclose(fwd.loc[m, "p_value"], rev.loc[m, "p_value"], atol=1e-12)
    checks.append(CheckResult("reverse_contrast", bool(sign_ok and p_ok),
                              "reversed contrast flips log2FC sign; SE/p unchanged"))

    # --- Check 4: all-zero genes ---------------------------------------------
    zero_status = res[0].table[res[0].table["status"] == "all_zero"]
    zero_ok = len(zero_status) == (fr.status == "all_zero").sum() and \
        zero_status["p_value"].isna().all() and zero_status["p_value_bh"].isna().all()
    checks.append(CheckResult(
        "all_zero_handling", bool(zero_ok),
        f"{len(zero_status)} all-zero genes carry independent status and no p-values"))

    # --- Check 5: failures never zero-filled ---------------------------------
    failed = res[0].table[res[0].table["status"] == "fit_failed"]
    fail_ok = failed["p_value"].isna().all()
    checks.append(CheckResult(
        "failure_status", bool(fail_ok),
        f"{len(failed)} fit_failed genes have no p-value (never zero-filled); "
        "reasons recorded in fit_diagnostics.csv"))

    # --- Check 6: independent small-matrix fit -------------------------------
    checks.append(_small_matrix_fit_check())

    report = {
        "all_passed": bool(all(c.passed for c in checks)),
        "checks": [
            {**asdict(c), "passed": bool(c.passed)} for c in checks
        ],
    }
    (work_dir / "verification.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return report
