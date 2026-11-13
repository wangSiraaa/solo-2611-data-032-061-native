"""Frozen analysis model: per-dataset OLS fits and Rubin pooling.

The same frozen linear model (response ~ covariates [+ intercept]) is
fitted independently on each completed dataset; coefficients and their
covariance matrices are then combined with Rubin's rules, keeping the
within-imputation (U) and between-imputation (B) components separate.
Completed datasets are never averaged before fitting.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import statsmodels.api as sm
from scipy import stats

from .dataset import CellState, Dataset, Eligibility
from .plan import Plan


def model_terms(plan: Plan) -> list:
    return (["Intercept"] if plan.intercept else []) + list(plan.covariates)


@dataclass
class FitResult:
    label: str  # e.g. "dataset_1" or "complete_case"
    terms: list
    params: np.ndarray
    vcov: np.ndarray
    n: int
    df_resid: int
    row_ids: list  # actual analysis rows, in canonical order

    @property
    def std_errors(self):
        return np.sqrt(np.diag(self.vcov))


def _design_matrix(values, ds: Dataset, plan: Plan, row_idx):
    cols = []
    if plan.intercept:
        cols.append(np.ones(len(row_idx)))
    for c in plan.covariates:
        cols.append(values[c][row_idx])
    X = np.column_stack(cols)
    y = values[plan.response][row_idx]
    return X, y


def fit_completed(values, ds: Dataset, plan: Plan, elig: Eligibility, label: str) -> FitResult:
    """Fit the frozen model on one completed dataset's analysis rows."""
    row_idx = np.flatnonzero(elig.eligible)
    X, y = _design_matrix(values, ds, plan, row_idx)
    res = sm.OLS(y, X).fit()
    return FitResult(
        label=label,
        terms=model_terms(plan),
        params=np.asarray(res.params, dtype=float),
        vcov=np.asarray(res.cov_params(), dtype=float),
        n=int(res.nobs),
        df_resid=int(res.df_resid),
        row_ids=[ds.row_ids[i] for i in row_idx],
    )


def fit_complete_case(ds: Dataset, plan: Plan, elig: Eligibility):
    """Complete-case fit on the original (unimputed) data, as a comparison.

    Returns (FitResult | None, status_note).
    """
    cc = elig.eligible.copy()
    for c in plan.covariates:
        cc &= ds.states[c] == CellState.OBSERVED
    n_params = len(model_terms(plan))
    n_cc = int(np.sum(cc))
    if n_cc <= n_params:
        return None, (
            f"only {n_cc} complete-case rows for {n_params} parameters; "
            "complete-case fit not identifiable"
        )
    row_idx = np.flatnonzero(cc)
    X, y = _design_matrix(ds.values, ds, plan, row_idx)
    res = sm.OLS(y, X).fit()
    fit = FitResult(
        label="complete_case",
        terms=model_terms(plan),
        params=np.asarray(res.params, dtype=float),
        vcov=np.asarray(res.cov_params(), dtype=float),
        n=int(res.nobs),
        df_resid=int(res.df_resid),
        row_ids=[ds.row_ids[i] for i in row_idx],
    )
    note = "ok"
    if n_cc < n_params + 2:
        note = f"only {n_cc} complete-case rows; residual df = {fit.df_resid}"
    return fit, note


def pool_rubin(fits: list) -> dict:
    """Combine per-dataset fits with Rubin's rules (Barnard-Rubin df).

    Keeps within-imputation variance U, between-imputation variance B and
    total variance T = U + (1 + 1/m) B separately for every term.
    """
    m = len(fits)
    terms = fits[0].terms
    Q = np.array([f.params for f in fits])  # m x p
    U_m = np.array([f.vcov for f in fits])  # m x p x p
    df_resid = fits[0].df_resid
    if any(f.df_resid != df_resid for f in fits):
        raise ValueError("per-dataset residual degrees of freedom differ")

    qbar = Q.mean(axis=0)
    Ubar = U_m.mean(axis=0)
    B = np.cov(Q, rowvar=False, ddof=1).reshape(Ubar.shape)
    T = Ubar + (1.0 + 1.0 / m) * B

    results = []
    for j, term in enumerate(terms):
        u = float(Ubar[j, j])
        b = float(B[j, j])
        t = float(T[j, j])
        se = float(np.sqrt(t))
        est = float(qbar[j])
        if b <= 0.0:
            # no between-imputation variance (e.g. nothing was imputed):
            # infinite df, normal reference.
            df = np.inf
            lam = 0.0
            riv = 0.0
            fmi = 0.0
        else:
            lam = (1.0 + 1.0 / m) * b / t
            df_old = (m - 1.0) / lam**2
            df_com = float(df_resid)
            df_obs = (1.0 - lam) * df_com * (df_com + 1.0) / (df_com + 3.0)
            df = df_old * df_obs / (df_old + df_obs) if df_obs > 0 else df_old
            riv = (1.0 + 1.0 / m) * b / u if u > 0 else np.inf
            fmi = riv / (1.0 + riv) if np.isfinite(riv) else 1.0
        if np.isfinite(df):
            crit = float(stats.t.ppf(0.975, df))
            pval = float(2.0 * stats.t.sf(abs(est / se), df)) if se > 0 else np.nan
        else:
            crit = float(stats.norm.ppf(0.975))
            pval = float(2.0 * stats.norm.sf(abs(est / se))) if se > 0 else np.nan
        results.append(
            {
                "term": term,
                "estimate": est,
                "std_error": se,
                "within_var": u,
                "between_var": b,
                "total_var": t,
                "df": float(df) if np.isfinite(df) else None,
                "df_infinite": not np.isfinite(df),
                "fmi": float(fmi),
                "riv": float(riv) if np.isfinite(riv) else None,
                "ci_lo": est - crit * se,
                "ci_hi": est + crit * se,
                "t": est / se if se > 0 else np.nan,
                "p_value": pval,
            }
        )
    return {
        "method": "rubin_rules_barnard_rubin_df",
        "m": m,
        "terms": terms,
        "results": results,
    }
