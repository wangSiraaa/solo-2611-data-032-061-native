"""Contrast inference and multiple-testing adjustment.

For every planned contrast and every *tested* gene we report:
  log2 fold change, its standard error and 95% Wald interval, raw p-value,
  BH-adjusted p-value, and the explicit hypothesis family used for adjustment.
Filtered, all-zero and failed genes keep independent statuses and never get
p-values filled in.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy import stats

from .design import DesignResult, contrast_vector
from .fitting import GeneFit
from .plan import Plan

LOG2 = np.log(2.0)
STATUS_ORDER = ("tested", "filtered_low_expression", "all_zero", "fit_failed")


@dataclass
class ContrastResults:
    contrast_name: str
    table: pd.DataFrame
    family_size: int
    n_adjusted: int


def _bh(pvals: np.ndarray) -> np.ndarray:
    """Benjamini-Hochberg step-up, robust to NaN and ties."""
    out = np.full_like(pvals, np.nan, dtype=float)
    valid = np.isfinite(pvals)
    p = pvals[valid]
    m = len(p)
    if m == 0:
        return out
    order = np.argsort(p, kind="stable")
    ranked = p[order]
    adj = ranked * m / (np.arange(m) + 1)
    adj = np.minimum.accumulate(adj[::-1])[::-1]
    adj = np.minimum(adj, 1.0)
    restored = np.empty(m)
    restored[order] = adj
    out[valid] = restored
    return out


def run_contrasts(plan: Plan, design: DesignResult,
                  fits: list[GeneFit | None],
                  filter_status: np.ndarray,
                  gene_ids: np.ndarray,
                  ql=None) -> list[ContrastResults]:
    vectors = {c.name: contrast_vector(c, plan, design.columns)
               for c in plan.contrasts}
    n = len(gene_ids)
    results: list[ContrastResults] = []

    for contrast in plan.contrasts:
        cvec = vectors[contrast.name]
        rows = []
        raw_p = np.full(n, np.nan)
        wald_p = np.full(n, np.nan)
        ql_p = np.full(n, np.nan)
        f_stats = np.full(n, np.nan)
        log2fc = np.full(n, np.nan)
        se = np.full(n, np.nan)
        lo = np.full(n, np.nan)
        hi = np.full(n, np.nan)
        status = filter_status.copy()
        methods = np.array([""] * n, dtype=object)
        dispersions = np.full(n, np.nan)
        ql_vals = np.full(n, np.nan)
        converged = np.zeros(n, dtype=bool)
        failure = np.array([""] * n, dtype=object)

        for i, fit in enumerate(fits):
            if filter_status[i] != "tested":
                continue
            if fit is None or not fit.converged:
                status[i] = "fit_failed"
                failure[i] = "" if fit is None else fit.failure_reason
                continue
            est = float(cvec @ fit.beta)
            var = float(cvec @ fit.vcov @ cvec)
            if var <= 0 or not np.isfinite(var):
                status[i] = "fit_failed"
                failure[i] = f"non-positive contrast variance ({var})"
                continue
            s = np.sqrt(var)
            t_stat = est / s
            # Primary test: t reference on residual df, which stays calibrated
            # when dispersions receive the Pearson small-sample correction.
            p_wald = min(max(
                2.0 * stats.t.sf(abs(t_stat), df=max(design.residual_df, 1)),
                0.0), 1.0)
            log2fc[i] = est / LOG2
            se[i] = s / LOG2
            lo[i] = (est - 1.959963984540054 * s) / LOG2
            hi[i] = (est + 1.959963984540054 * s) / LOG2
            methods[i] = fit.method
            dispersions[i] = fit.dispersion
            converged[i] = True
            if ql is not None and np.isfinite(ql.ql_moderated[i]):
                from .ql import ql_contrast_pvalue
                p_ql, fstat = ql_contrast_pvalue(
                    est, var, ql.ql_moderated[i], ql.df_total)
                wald_p[i] = p_wald
                raw_p[i] = p_wald                 # Wald t remains primary
                f_stats[i] = fstat
                ql_vals[i] = ql.ql_moderated[i]
                ql_p[i] = p_ql                    # QL kept as diagnostic column
            else:
                wald_p[i] = p_wald
                raw_p[i] = p_wald

        tested_mask = status == "tested"
        padj = np.full(n, np.nan)
        padj[tested_mask] = _bh(raw_p[tested_mask])

        table = pd.DataFrame({
            "gene_id": gene_ids,
            "status": status,
            "converged": converged,
            "log2_fold_change": log2fc,
            "se_log2fc": se,
            "ci95_low_log2fc": lo,
            "ci95_high_log2fc": hi,
            "p_value": raw_p,
            "p_value_test": np.array(["Wald_t"] * n, dtype=object),
            "p_value_wald_t": wald_p,
            "p_value_ql_f_diagnostic": ql_p,
            "f_statistic": f_stats,
            "ql_dispersion_moderated": ql_vals,
            "p_value_bh": padj,
            "dispersion": dispersions,
            "fit_method": methods,
            "failure_reason": failure,
        })
        table.attrs["contrast"] = {
            "name": contrast.name,
            "numerator": contrast.numerator,
            "denominator": contrast.denominator,
            "vector": dict(zip(design.columns, cvec.tolist())),
            "p_value_test": "Wald_t",
            "ql": (None if ql is None else {
                "note": "QL F p-values are diagnostic only; Wald t is primary",
                "df_prior": ql.df_prior, "df_total": ql.df_total,
                "common_ql": ql.common_ql, "detail": ql.note}),
        }
        results.append(ContrastResults(
            contrast_name=contrast.name, table=table,
            family_size=int(tested_mask.sum()),
            n_adjusted=int(np.isfinite(padj[tested_mask]).sum())))
    return results


def normalized_expression(counts: np.ndarray, size_factors: np.ndarray,
                          gene_ids: np.ndarray, sample_ids: np.ndarray) -> pd.DataFrame:
    """Normalized counts (raw / size factor) and log2 CPM of those — same run."""
    norm = counts / size_factors[None, :]
    df = pd.DataFrame(norm, index=gene_ids, columns=sample_ids)
    df.index.name = "gene_id"
    return df
