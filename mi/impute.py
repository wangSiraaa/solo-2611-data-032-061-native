"""Iterative conditional imputation (fully conditional specification).

Method, per completed dataset (= one independent chain):

1.  Initialisation: every ordinary-missing cell is filled with a random
    draw from the observed values of its variable (stochastic, not a mean).
2.  Sweeps: for ``iterations`` rounds, visit each incomplete variable in
    ascending missing-count order.  For variable j:

    * fit a Bayesian linear regression of j on the other allowed
      predictors, using rows where j is observed and all predictors are
      applicable (current values used for predictors that are themselves
      imputed -- this is the "conditional" update);
    * draw (sigma^2, beta) from the posterior under a flat prior:
      sigma^2 ~ df * s^2 / chi2_df,  beta | sigma^2 ~ N(beta_hat, sigma^2 (X'X)^-1);
    * predictive mean matching: recipients are matched to the k nearest
      donors by predicted mean (donors scored with beta_hat, recipients
      with the drawn beta*), and one donor is chosen uniformly at random;
      the imputed value is that donor's OBSERVED value.

Randomness therefore has three genuine sources per sweep: the posterior
draw, the donor lottery, and the stochastic initialisation.  Nothing
deterministic is ever copied across chains.

Range handling: PMM only returns observed donor values, which were
validated against the plan ranges at load time, so imputations inherit
the observed support.  A defensive clip to the declared range is applied
on top and counted (expected count: zero).

Update order: variables sorted by ascending number of missing cells
(ties broken by plan declaration order); fixed for the whole run.

Random streams: ``SeedSequence([seed, chain, sweep, variable_index,
purpose])`` -- a pure function of those coordinates, independent of loop
nesting.  Rows are processed in canonical (sorted row_id) order, so
permuting the input files cannot change any draw.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .dataset import CellState, Dataset
from .plan import Plan

PMM_DONORS = 5  # candidate donors per recipient
_PURPOSE_INIT = 0
_PURPOSE_SWEEP = 1


def _rng(seed: int, chain: int, sweep: int, var_index: int, purpose: int):
    return np.random.default_rng(
        np.random.SeedSequence([seed, chain, sweep, var_index, purpose])
    )


@dataclass
class ChainResult:
    chain: int
    completed: dict  # var -> float64 array (NaN only at NOT_APPLICABLE cells)
    traces: dict  # var -> list of {"iteration", "mean", "sd", "n"}
    events: list  # list of {"variable", "kind", "detail", "iterations"}
    status: str  # "ok" | "ok_with_flags" | "failed"


def _event(events, variable, kind, detail, iteration):
    """Record a chain event, aggregating iterations per (variable, kind)."""
    for ev in events:
        if ev["variable"] == variable and ev["kind"] == kind and ev["detail"] == detail:
            ev["iterations"].append(iteration)
            return
    events.append(
        {
            "variable": variable,
            "kind": kind,
            "detail": detail,
            "iterations": [iteration],
        }
    )


def _design(work, predictors, rows):
    X = np.empty((len(rows), 1 + len(predictors)))
    X[:, 0] = 1.0
    for j, p in enumerate(predictors):
        X[:, 1 + j] = work[p][rows]
    return X


def _bayesian_draw(X, y, rng):
    """Posterior draw for a Gaussian linear model with flat prior.

    Returns (beta_hat, beta_star, mode) with mode in
    {"full", "degenerate", "single"}.
    """
    n, p = X.shape
    if n == 1:
        beta_hat = np.zeros(p)
        beta_hat[0] = y[0]
        return beta_hat, None, "single"
    beta_hat, _, rank, _ = np.linalg.lstsq(X, y, rcond=None)
    if rank < p or n <= rank:
        # Degenerate design (rank deficient or no residual df): fall back
        # to an intercept-only marginal model, still with a posterior draw.
        ybar = float(np.mean(y))
        s2 = float(np.var(y, ddof=1))
        df = n - 1
        sigma2 = df * s2 / rng.chisquare(df) if s2 > 0.0 else 0.0
        beta_star = np.zeros(p)
        beta_star[0] = ybar + np.sqrt(sigma2 / n) * rng.standard_normal()
        bh = np.zeros(p)
        bh[0] = ybar
        return bh, beta_star, "degenerate"
    df = n - rank
    resid = y - X @ beta_hat
    s2 = float(resid @ resid) / df
    sigma2 = df * s2 / rng.chisquare(df)
    cov = np.linalg.solve(X.T @ X, np.eye(p))
    # symmetric matrix square root via eigendecomposition
    w, V = np.linalg.eigh(cov)
    root = (V * np.sqrt(np.clip(w, 0.0, None))) @ V.T
    beta_star = beta_hat + np.sqrt(sigma2) * (root @ rng.standard_normal(p))
    return beta_hat, beta_star, "full"


def _pmm_values(yhat_donors, donor_values, yhat_recips, rng, k=PMM_DONORS):
    """Predictive mean matching: uniform pick among the k nearest donors."""
    k = min(k, len(donor_values))
    out = np.empty(len(yhat_recips))
    for i, yh in enumerate(yhat_recips):
        d = np.abs(yhat_donors - yh)
        nearest = np.argsort(d, kind="stable")[:k]
        out[i] = donor_values[nearest[rng.integers(k)]]
    return out


def run_chains(ds: Dataset, plan: Plan) -> list:
    """Run ``plan.n_datasets`` independent imputation chains."""
    variables = ds.variables
    var_index = {v: i for i, v in enumerate(variables)}
    n = ds.n_rows

    # incomplete variables, sweep order: ascending missing count, then
    # declaration order (deterministic).
    impute_vars = sorted(
        (v for v in variables if np.any(ds.states[v] == CellState.MISSING)),
        key=lambda v: (int(np.sum(ds.states[v] == CellState.MISSING)), var_index[v]),
    )
    predictors_of = {
        v: [p for p in plan.allowed_predictors if p != v] for v in impute_vars
    }

    results = []
    for chain in range(plan.n_datasets):
        work = {v: ds.values[v].copy() for v in variables}
        events = []
        traces = {v: [] for v in impute_vars}
        failed = None

        # ---- stochastic initialisation --------------------------------
        for v in impute_vars:
            rng = _rng(plan.seed, chain, 0, var_index[v], _PURPOSE_INIT)
            obs = ds.values[v][ds.states[v] == CellState.OBSERVED]
            miss_rows = np.flatnonzero(ds.states[v] == CellState.MISSING)
            work[v][miss_rows] = obs[rng.integers(0, len(obs), size=len(miss_rows))]

        def record_trace(iteration):
            for v in impute_vars:
                cells = work[v][ds.states[v] == CellState.MISSING]
                traces[v].append(
                    {
                        "iteration": iteration,
                        "mean": float(np.mean(cells)),
                        "sd": float(np.std(cells, ddof=1)) if len(cells) > 1 else 0.0,
                        "n": int(len(cells)),
                    }
                )

        record_trace(0)

        # ---- sweeps ----------------------------------------------------
        for t in range(1, plan.n_iterations + 1):
            if failed:
                break
            for v in impute_vars:
                rng = _rng(plan.seed, chain, t, var_index[v], _PURPOSE_SWEEP)
                preds = predictors_of[v]
                spec = plan.spec(v)

                pred_ok = np.ones(n, dtype=bool)
                for p in preds:
                    pred_ok &= ds.states[p] != CellState.NOT_APPLICABLE

                donor_rows = np.flatnonzero(
                    (ds.states[v] == CellState.OBSERVED) & pred_ok
                )
                recip_model = np.flatnonzero(
                    (ds.states[v] == CellState.MISSING) & pred_ok
                )
                recip_fallback = np.flatnonzero(
                    (ds.states[v] == CellState.MISSING) & ~pred_ok
                )

                obs_vals = ds.values[v][ds.states[v] == CellState.OBSERVED]

                # recipients whose predictors are structurally inapplicable
                # cannot be scored by the conditional model: draw from the
                # observed marginal instead (flagged; such rows are excluded
                # from the analysis sample by policy anyway).
                if len(recip_fallback):
                    work[v][recip_fallback] = obs_vals[
                        rng.integers(0, len(obs_vals), size=len(recip_fallback))
                    ]
                    _event(
                        events,
                        v,
                        "marginal_fallback",
                        f"{len(recip_fallback)} recipient(s) have inapplicable "
                        "predictors; imputed from the observed marginal",
                        t,
                    )

                if len(recip_model):
                    if len(donor_rows) == 0:
                        failed = f"variable {v!r}: no donor rows available"
                        break
                    Xd = _design(work, preds, donor_rows)
                    yd = work[v][donor_rows]
                    beta_hat, beta_star, mode = _bayesian_draw(Xd, yd, rng)
                    if mode != "full":
                        _event(
                            events,
                            v,
                            "degenerate_model",
                            f"conditional model degenerate (mode={mode}, "
                            f"{len(donor_rows)} donors); fell back to a "
                            "stochastic marginal draw",
                            t,
                        )
                        work[v][recip_model] = obs_vals[
                            rng.integers(0, len(obs_vals), size=len(recip_model))
                        ]
                    else:
                        if len(donor_rows) < PMM_DONORS:
                            _event(
                                events,
                                v,
                                "donor_pool_reduced",
                                f"only {len(donor_rows)} donors available "
                                f"(< {PMM_DONORS})",
                                t,
                            )
                        yhat_d = Xd @ beta_hat
                        Xm = _design(work, preds, recip_model)
                        yhat_m = Xm @ beta_star
                        vals = _pmm_values(yhat_d, yd, yhat_m, rng)
                        # safety net: PMM picks observed donors, so clipping
                        # should never fire; count it if it does.
                        clipped = np.clip(vals, spec.lo, spec.hi)
                        if not np.array_equal(clipped, vals):
                            _event(
                                events,
                                v,
                                "range_clip",
                                "imputed value(s) clipped to the declared range",
                                t,
                            )
                        work[v][recip_model] = clipped

                check_rows = np.flatnonzero(ds.states[v] != CellState.NOT_APPLICABLE)
                if not np.all(np.isfinite(work[v][check_rows])):
                    failed = f"variable {v!r}: non-finite imputed value produced"
                    break
            record_trace(t)

        # ---- chain status ----------------------------------------------
        if failed:
            status = "failed"
            events.append(
                {"variable": None, "kind": "chain_failure", "detail": failed,
                 "iterations": []}
            )
        else:
            status = "ok"
            for v in impute_vars:
                obs = ds.values[v][ds.states[v] == CellState.OBSERVED]
                imp = work[v][ds.states[v] == CellState.MISSING]
                sd_obs = float(np.std(obs, ddof=1)) if len(obs) > 1 else 0.0
                if sd_obs > 0 and abs(float(np.mean(imp)) - float(np.mean(obs))) > 5 * sd_obs:
                    _event(
                        events,
                        v,
                        "drift_warning",
                        "imputed mean is more than 5 observed SDs from the "
                        "observed mean",
                        plan.n_iterations,
                    )
            if events:
                status = "ok_with_flags"

        results.append(
            ChainResult(
                chain=chain,
                completed=work,
                traces=traces,
                events=events,
                status=status,
            )
        )
    return results
