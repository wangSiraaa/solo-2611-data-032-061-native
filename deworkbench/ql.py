"""Quasi-likelihood F-test for small-sample count inference.

The NB2 GLM supplies the mean/variance model and coefficients. Per-gene
*residual* variability is then captured by a quasi-likelihood dispersion
(Pearson statistic / residual df), moderated across genes with Smyth (2004)
squeezeVar, and contrasts are tested with an F-test on the moderated degrees
of freedom. This is the edgeR glmQLFTest construction and, unlike a plain
Wald test, stays calibrated when residual df is very small (e.g. paired n=8).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import special, stats


@dataclass
class QLResult:
    ql_raw: np.ndarray            # Pearson chi2 / residual df per gene
    ql_moderated: np.ndarray      # squeezed variance factors
    df_prior: float
    df_total: float
    common_ql: float
    note: str


def squeeze_var(sample_vars: np.ndarray, df: int) -> tuple[np.ndarray, float,
                                                            float, float]:
    """Smyth (2004) limma::squeezeVar, equal-df case.

    On the log scale the sampling variance of log(s2) is trigamma(df/2). The
    between-gene (prior) component is what remains after subtracting that, so
    trigamma(d0/2) = observed_log_var - trigamma(df/2).
    """
    finite = np.isfinite(sample_vars)
    s2 = sample_vars[finite]
    z = np.log(s2)
    observed_log_var = np.var(z, ddof=1)
    sampling = special.polygamma(1, df / 2.0)
    between = observed_log_var - sampling
    # When the observed between-gene spread is at or below the pure sampling
    # floor (common when df is tiny and ML dispersions are downward biased),
    # cap the prior strength instead of forcing infinite shrinkage.
    d0_max = 10.0 * max(df, 1)
    if between <= 1e-12:
        d0 = d0_max
    else:
        d0 = min(max(2.0 * _invert_trigamma(between), 1e-3), d0_max)
    # limma prior scale (Smyth 2004 eq. 5.3): mean of z corrected for the
    # digamma bias between posterior and residual degrees of freedom.
    s2_prior = float(np.exp(
        np.mean(z) - special.polygamma(0, df / 2.0)
        + special.polygamma(0, (df + d0) / 2.0)))
    post = np.full_like(sample_vars, np.nan)
    post[finite] = (df * s2 + d0 * s2_prior) / (df + d0)
    df_total = df + d0
    return post, float(d0), s2_prior, float(df_total)


def _invert_trigamma(target: float) -> float:
    """Solve trigamma(x) = target for x > 0; returns x (caller maps d0 = 2x)."""
    lo, hi = 1e-4, 1e6
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if special.polygamma(1, mid) > target:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-6 * mid:
            break
    return mid


def quasi_likelihood(fits, filter_status: np.ndarray, gene_ids: np.ndarray,
                     residual_df: int) -> QLResult:
    n = len(gene_ids)
    ql_raw = np.full(n, np.nan)
    for i, f in enumerate(fits):
        if filter_status[i] == "tested" and f is not None and f.converged:
            ql_raw[i] = f.pearson_chi2 / max(residual_df, 1)
    post, d0, common, df_total = squeeze_var(ql_raw, residual_df)
    note = (
        f"QL F-test: raw QL dispersion = Pearson chi2 / df ({residual_df}); "
        f"squeezeVar prior df={d0:.2f}, prior scale={common:.3f}, "
        f"total df={df_total:.2f}. Contrast F=(c'b)^2/(c'Vc)/ql_moderated "
        "on F(1, df_total).")
    return QLResult(ql_raw=ql_raw, ql_moderated=post, df_prior=d0,
                    df_total=df_total, common_ql=common, note=note)


def ql_contrast_pvalue(est: float, var: float, ql_moderated: float,
                       df_total: float) -> tuple[float, float]:
    """Return (two-sided p-value, F statistic) for one contrast."""
    wald = est ** 2 / var
    f_stat = wald / max(ql_moderated, 1e-12)
    p = float(stats.f.sf(f_stat, dfn=1, dfd=df_total))
    return min(max(p, 0.0), 1.0), float(f_stat)
