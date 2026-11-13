"""Dispersion moderation (edgeR-style empirical-Bayes shrinkage).

Per-gene ML dispersions are noisy when residual degrees of freedom are small
(e.g. paired designs with few subjects). Each estimate is therefore shrunk
toward a mean-vs-dispersion trend. Everything is archived: the raw ML value,
the trend target, the prior strength and the moderated value actually used for
inference. Genes whose fit failed are left untouched and keep their status.

Variance model on the log-dispersion scale:
  sampling variance of log(alpha_hat) ~ 2 / df_resid          (NB likelihood)
  between-gene variance s2 is observed around the trend
  prior variance tau2 = max(s2 - median(2/df), floor)
  prior observations  n0 = 2 / tau2
  shrink weight       w  = n0 / (n0 + df)
  log(alpha_mod) = w * log(alpha_trend) + (1 - w) * log(alpha_hat)
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .fitting import GeneFit, _irls


@dataclass
class ModerationResult:
    moderated_alpha: np.ndarray          # NaN where not applicable
    trend_alpha: np.ndarray
    raw_alpha: np.ndarray
    prior_observations: float
    prior_log_variance: float
    weights: np.ndarray
    method_note: str


def moderate_dispersions(fits: list[GeneFit | None],
                         base_means: np.ndarray,
                         df_resid: int) -> ModerationResult:
    n = len(fits)
    raw = np.full(n, np.nan)
    for i, f in enumerate(fits):
        if f is not None and f.converged:
            raw[i] = f.alpha
    ok = np.isfinite(raw)
    trend = np.full(n, np.nan)
    moderated = raw.copy()
    weights = np.zeros(n)

    log_mean = np.log(np.maximum(base_means, 1e-8))
    log_alpha = np.log(raw[ok])
    # Linear trend of log-dispersion on log-abundance (robust to scale).
    x = log_mean[ok]
    coeffs = np.polyfit(x, log_alpha, deg=1)
    trend_log = np.polyval(coeffs, log_mean)
    trend[ok] = np.exp(np.clip(trend_log[ok], -12, 12))

    resid_var = float(np.var(log_alpha - np.polyval(coeffs, x), ddof=2))
    sampling_var = 2.0 / max(df_resid, 1)
    tau2 = max(resid_var - sampling_var, 0.05)
    n0 = 2.0 / tau2
    w = n0 / (n0 + max(df_resid, 1))
    weights[ok] = w
    moderated[ok] = np.exp(
        w * np.log(trend[ok]) + (1.0 - w) * np.log(raw[ok]))

    note = (
        f"edgeR-style log-scale shrinkage toward a linear abundance trend; "
        f"residual df={df_resid}, prior observations n0={n0:.2f}, "
        f"shrink weight w={w:.3f} (w=0 means no shrinkage). "
        "Raw ML and moderated dispersions are both archived.")
    return ModerationResult(moderated_alpha=moderated, trend_alpha=trend,
                            raw_alpha=raw, prior_observations=n0,
                            prior_log_variance=tau2, weights=weights,
                            method_note=note)


def pearson_damping(df_resid: int) -> float:
    """Adaptive Pearson-correction damping used for a given residual df."""
    return float(min(1.0, max(0.3, 8.0 / max(df_resid, 1))))


def pearson_bias_correct_alpha(y: np.ndarray, X: np.ndarray,
                               log_offset: np.ndarray, alpha_ml: float,
                               df_resid: int, max_iter: int = 6,
                               damping: float | None = None
                               ) -> tuple[float, np.ndarray, np.ndarray, bool]:
    """Rescale the NB2 dispersion toward matching the Pearson statistic to its
    expectation (residual df). The damping factor controls how far the ML
    dispersion moves toward that target; it is chosen adaptively from the
    residual degrees of freedom because the finite-sample downward bias of ML
    dispersion scales like 1/df — small-df paired fits need close to the full
    correction while well-replicated independent fits need little. A fixed
    value in (0,1] overrides the adaptive choice.
    Returns (alpha_corrected, beta, vcov, converged).
    """
    if damping is None:
        # Full correction at df <= 8, tapering toward ~0.3 by df ~ 25.
        damping = pearson_damping(df_resid)
    alpha = max(float(alpha_ml), 1e-6)
    beta = vcov = None
    converged = False
    for _ in range(max_iter):
        beta, _, err = _irls(y, X, log_offset, alpha)
        if beta is None:
            break
        mu = np.exp(log_offset + X @ beta)
        var = mu + alpha * mu ** 2
        chi2 = float(np.sum((y - mu) ** 2 / var))
        factor = max(chi2 / max(df_resid, 1), 1e-12)
        new_alpha = max(alpha * factor ** damping, 1e-6)
        if abs(new_alpha - alpha) / max(alpha, 1e-12) < 1e-4:
            converged = True
        alpha = new_alpha
    if beta is None:
        return float(alpha_ml), None, None, False
    beta, _, err = _irls(y, X, log_offset, alpha)
    if beta is None:
        return float(alpha_ml), None, None, False
    mu = np.exp(log_offset + X @ beta)
    wgt = mu / (1.0 + alpha * mu)
    try:
        vcov = np.linalg.inv((X * wgt[:, None]).T @ X)
    except np.linalg.LinAlgError:
        return float(alpha_ml), beta, None, False
    return alpha, beta, vcov, converged


def refit_with_moderated_alpha(fits: list[GeneFit | None],
                               moderation: ModerationResult,
                               counts: np.ndarray,
                               X: np.ndarray,
                               log_offset: np.ndarray,
                               df_resid: int,
                               use_pearson_correction: bool = True) -> None:
    """Refit beta at the moderated alpha, optionally with Pearson bias fix.

    The original ML fit is preserved on the gene object for audit; only the
    values used for inference are replaced, and only when the fixed-alpha IRLS
    converges cleanly. Otherwise the gene keeps its ML alpha and fit.
    """
    for i, f in enumerate(fits):
        if f is None or not f.converged:
            continue
        alpha_target = float(moderation.moderated_alpha[i])
        y = counts[i].astype(float)
        if use_pearson_correction:
            alpha_use, beta, vcov, ok = pearson_bias_correct_alpha(
                y, X, log_offset, alpha_target, df_resid)
            if beta is None or vcov is None:
                f.warnings.append(
                    "Pearson correction failed; kept moderated-ml fit")
                beta, it, err = _irls(y, X, log_offset, alpha_target)
                if beta is None:
                    continue
                mu = np.exp(log_offset + X @ beta)
                wgt = mu / (1.0 + alpha_target * mu)
                vcov = np.linalg.inv((X * wgt[:, None]).T @ X)
                alpha_use = alpha_target
        else:
            beta, it, err = _irls(y, X, log_offset, alpha_target)
            if beta is None or err:
                f.warnings.append(
                    f"moderated-alpha IRLS failed ({err}); kept ML alpha {f.alpha:.4g}")
                continue
            mu = np.exp(log_offset + X @ beta)
            wgt = mu / (1.0 + alpha_target * mu)
            vcov = np.linalg.inv((X * wgt[:, None]).T @ X)
            alpha_use = alpha_target
        if not np.isfinite(vcov).all():
            f.warnings.append("moderated vcov non-finite; kept ML alpha")
            continue
        mu = np.exp(log_offset + X @ beta)
        f.warnings.append(
            f"alpha ML={f.alpha:.4g} -> moderated/corrected={alpha_use:.4g}")
        f.beta, f.vcov, f.mu, f.alpha = beta, vcov, mu, alpha_use
        f.dispersion = alpha_use
        f.method = f.method + "+moderated_pearson_corrected"
        f.iterations += 1
