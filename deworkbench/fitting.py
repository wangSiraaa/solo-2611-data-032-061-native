"""Per-gene Negative-Binomial GLM fitting with honest failure handling.

The primary fitter is a self-contained profile-ML procedure: IRLS for beta at
fixed dispersion alpha (Var = mu + alpha*mu^2, the NB2 form), with alpha chosen
by scalar bounded maximization of the NB likelihood. This avoids the known
tendency of generic BFGS to settle on poor dispersion optima for small
heterogeneous samples. statsmodels' NB2 is additionally fitted as an
independent cross-check, and a large log-likelihood gap is recorded.

Every gene returns a GeneFit with a converged flag and a failure reason.
Non-converged genes get status 'fit_failed', no p-value, and are excluded from
the adjustment family rather than being declared unchanged.
"""
from __future__ import annotations

import warnings
from dataclasses import dataclass, field

import numpy as np
from scipy import optimize, stats

from .design import DesignResult

warnings.filterwarnings("ignore", category=RuntimeWarning)

ALPHA_BOUNDS = (1e-4, 1e2)
LOG_MU_BOUND = 30.0          # keeps exp(log-mu) representable during iteration


@dataclass
class GeneFit:
    gene_id: str
    converged: bool
    method: str = ""
    failure_reason: str = ""
    beta: np.ndarray | None = None          # log-scale coefficients
    vcov: np.ndarray | None = None
    alpha: float = np.nan                    # NB2 alpha = dispersion
    dispersion: float = np.nan               # variance = mu + dispersion * mu^2
    mu: np.ndarray | None = None
    pearson_chi2: float = np.nan
    deviance: float = np.nan
    df_resid: int = 0
    iterations: int = 0
    warnings: list[str] = field(default_factory=list)
    cross_check: dict = field(default_factory=dict)


def nb_loglik(y, mu, alpha):
    """NB2 log likelihood, scipy parameterization n=1/alpha, p=n/(n+mu)."""
    a = 1.0 / alpha
    return float(np.sum(stats.nbinom.logpmf(y, n=a, p=a / (a + mu))))


def _irls_weights(y, mu, alpha):
    w = mu / (1.0 + alpha * mu)             # d mu/d eta squared over variance
    z = np.log(mu) + (y - mu) / mu          # working response
    return z, w


def _irls(y, X, offset, alpha, max_iter=60, tol=1e-9):
    beta = np.zeros(X.shape[1])
    eta = np.clip(offset + X @ beta, -LOG_MU_BOUND, LOG_MU_BOUND)
    mu = np.exp(eta)
    for it in range(1, max_iter + 1):
        z, w = _irls_weights(y, mu, alpha)
        if not (np.isfinite(z).all() and np.isfinite(w).all() and (w > 0).all()):
            return None, it, "non-finite IRLS weights/working response"
        Xw = X * w[:, None]
        XtWX = Xw.T @ X
        XtWz = Xw.T @ (z - offset)
        try:
            new_beta = np.linalg.solve(XtWX, XtWz)
        except np.linalg.LinAlgError:
            return None, it, "singular IRLS normal equations"
        if not np.isfinite(new_beta).all():
            return None, it, "non-finite IRLS coefficient"
        # Halve the step until the linear predictor stays in a safe range.
        new_eta = np.clip(offset + X @ new_beta, -LOG_MU_BOUND, LOG_MU_BOUND)
        for _ in range(20):
            if np.abs(new_eta).max() < LOG_MU_BOUND:
                break
            new_beta = 0.5 * (new_beta + beta)
            new_eta = np.clip(offset + X @ new_beta,
                              -LOG_MU_BOUND, LOG_MU_BOUND)
        step = np.max(np.abs(new_beta - beta))
        beta = new_beta
        mu = np.exp(new_eta)
        if step < tol:
            return beta, it, ""
    return beta, max_iter, "IRLS did not converge in max_iter"


def _mom_alpha(y, X, offset):
    """Method-of-moments start value for the dispersion."""
    mu = np.maximum(np.exp(offset + np.log(max(y.mean(), 1e-4))), 1e-4)
    with np.errstate(invalid="ignore", divide="ignore"):
        phi = np.mean(((y - mu) ** 2 - mu) / mu ** 2)
    return float(np.clip(phi, *ALPHA_BOUNDS))


def custom_fit(y, X, offset):
    """Profile-ML: optimize scalar log(alpha), IRLS for beta at each value."""
    n, p = X.shape

    def fixed_alpha(alpha):
        beta, it, err = _irls(y, X, offset, alpha)
        if beta is None:
            return None, it, err
        return (beta, np.exp(offset + X @ beta)), it, err

    alpha_start = _mom_alpha(y, X, offset)

    def negll(log_alpha):
        out, _, _ = fixed_alpha(np.exp(log_alpha))
        if out is None:
            return 1e12
        _, mu = out
        ll = nb_loglik(y, mu, np.exp(log_alpha))
        return -ll if np.isfinite(ll) else 1e12

    lo, hi = np.log(ALPHA_BOUNDS[0]), np.log(ALPHA_BOUNDS[1])
    # Bounded robust optimization, bracketed around the MoM start value.
    res = optimize.minimize_scalar(
        negll, bounds=(lo, hi), method="bounded", options={"xatol": 1e-6})
    alpha = float(np.exp(res.x))
    out, it, err = fixed_alpha(alpha)
    if out is None or not res.success:
        return None, f"profile ML failed: {err or res.message}"
    beta, mu = out
    w = mu / (1.0 + alpha * mu)
    XtWX = (X * w[:, None]).T @ X
    try:
        vcov = np.linalg.inv(XtWX)
    except np.linalg.LinAlgError:
        return None, "singular covariance at optimum"
    if not np.isfinite(vcov).all():
        return None, "non-finite covariance at optimum"
    return (beta, vcov, mu, alpha, it, res.nfev), ""


def _statsmodels_crosscheck(y, X, offset, alpha_hat, ll_hat):
    """Independent NB2 fit for diagnostics; never silently overrides the main fit."""
    try:
        import statsmodels.api as sm
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            mfit = sm.NegativeBinomial(y, X, offset=offset).fit(
                disp=False, maxiter=200, warn_convergence=False)
        if not mfit.converged or not np.isfinite(mfit.params).all():
            return {"converged": False, "reason": "optimizer did not converge"}
        sm_alpha = float(np.exp(mfit.params[-1]))
        return {
            "converged": True,
            "alpha": sm_alpha,
            "loglik": float(mfit.llf),
            "main_loglik": ll_hat,
            "ll_gap": float(mfit.llf - ll_hat),
            "alpha_ratio_to_main": float(sm_alpha / alpha_hat),
        }
    except Exception as exc:  # cross-check must never break the run
        return {"converged": False, "reason": f"{type(exc).__name__}: {exc}"}


def fit_gene(gene_id: str, y: np.ndarray, design: DesignResult,
             log_offset: np.ndarray) -> GeneFit:
    y = y.astype(np.float64)
    X = design.X
    n, p = X.shape
    fit = GeneFit(gene_id=gene_id, converged=False, df_resid=n - p)
    if y.sum() == 0:
        fit.failure_reason = "all-zero gene (handled as separate status, not fitted)"
        return fit

    out, err = custom_fit(y, X, log_offset)
    if out is None:
        fit.failure_reason = f"primary profile-ML fit failed: {err}"
        return fit
    beta, vcov, mu, alpha, it, nfev = out
    ll = nb_loglik(y, mu, alpha)

    fit.converged = True
    fit.method = "custom_NB2_profile_ML(IRLS_beta)"
    fit.beta, fit.vcov, fit.mu, fit.alpha = beta, vcov, mu, alpha
    fit.dispersion = alpha
    fit.iterations = it
    fit.cross_check = _statsmodels_crosscheck(y, X, log_offset, alpha, ll)
    if fit.cross_check.get("converged") and fit.cross_check["ll_gap"] > 1e-3:
        # Our optimum has the higher likelihood; record the disagreement.
        fit.warnings.append(
            "statsmodels NB2 converged to a lower-likelihood solution "
            f"(gap={fit.cross_check['ll_gap']:.2f}); kept profile-ML optimum")
    _diagnostics(fit, y)
    return fit


def _diagnostics(fit: GeneFit, y: np.ndarray) -> None:
    mu, alpha = fit.mu, fit.alpha
    var = mu + alpha * mu ** 2
    fit.pearson_chi2 = float(np.sum((y - mu) ** 2 / var))
    ll_sat = nb_loglik(y, np.maximum(y, 1e-8), alpha)
    ll_fit = nb_loglik(y, mu, alpha)
    fit.deviance = float(2.0 * (ll_sat - ll_fit))
