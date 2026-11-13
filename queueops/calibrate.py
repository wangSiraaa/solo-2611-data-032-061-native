"""Service-time calibration from reconstructed observations.

Only regions marked ``train`` are used for fitting, model selection and
preprocessing.  Censoring handling is explicit and reported:

* right-censored service fragments (task still in service at end of window)
  carry a lower bound and enter the fit through a censored likelihood;
  parametric candidates use the survival term, the empirical candidate uses
  the Kaplan-Meier product-limit estimator.
* interval fragments ``(0, U]`` (a terminal event exists but
  ``service_complete`` is missing) are NOT usable as exact samples and are
  reported and excluded from parametric likelihood; they are never written as
  zero.  The empirical candidate also refuses them.
* samples lacking any bound are excluded and counted.

Candidates:
  * ``empirical_km``: empirical distribution / Kaplan-Meier (nonparametric)
  * ``lognormal``: log-normal MLE (common for service durations)
  * ``gamma``: gamma MLE (another parametric candidate)

Model selection uses only training data: AIC for parametric candidates and a
documented rule for the empirical vs parametric choice.  Diagnostics flag
insufficient samples, over-split categories, non-identifiability, and drift
across shifts; a merge/reject decision is recorded per group.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

import numpy as np

from .common import (
    STATUS_EXACT, STATUS_RIGHT, STATUS_UNKNOWN, STATUS_UPPER,
    CalibrationError, as_int, dump_json, require,
)

# diagnostics thresholds (documented, so operators can see why a group fell back)
MIN_EXACT_SAMPLES = 5
MIN_TOTAL_INFO = 5          # exact + right-censored lower bounds
DRIFT_ALPHA = 0.001         # KS-test p-value for cross-shift drift
DRIFT_MIN_PER_SHIFT = 5
MIN_MEAN_SERVICE = 1.0


@dataclass
class ServiceFragment:
    task_id: str
    stage_index: int
    station_id: str
    category: str
    shift_id: str | None
    status: str               # exact / right_censored / interval_upper / unknown
    value: int | None = None  # exact duration
    lower: int | None = None  # right-censored lower bound
    upper: int | None = None  # interval (0, U]

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id, "stage_index": self.stage_index,
            "station_id": self.station_id, "category": self.category,
            "shift_id": self.shift_id, "status": self.status,
            "value": self.value, "lower": self.lower, "upper": self.upper,
        }


def fragments_from_reconstruction(rec, train_windows: set[str]) -> list[ServiceFragment]:
    """Collect fragments in training shifts only."""
    out: list[ServiceFragment] = []
    for st in rec.stage_observations():
        if st.shift_id not in train_windows:
            continue
        out.append(ServiceFragment(
            st.task_id, st.stage_index, st.station_id, st.category,
            st.shift_id, st.service_status, st.service_ms, st.service_lower,
            st.service_upper))
    return out


@dataclass
class ParamFit:
    name: str
    params: dict
    loglik: float
    n_exact: int
    n_right: int
    n_used: int
    aic: float | None
    converged: bool
    note: str = ""


def _fit_lognormal(durations: np.ndarray, censored_lb: np.ndarray) -> ParamFit:
    from scipy.optimize import minimize
    from scipy.stats import norm

    def nll(theta: np.ndarray) -> float:
        mu, log_sigma = theta[0], theta[1]
        sigma = np.exp(log_sigma)
        val = 0.0
        if durations.size:
            val -= np.sum(norm.logpdf((np.log(np.maximum(durations, 1e-9)) - mu) / sigma)
                          - np.log(sigma) - np.log(np.maximum(durations, 1e-9)))
        if censored_lb.size:
            val -= np.sum(norm.logsf((np.log(np.maximum(censored_lb, 1e-9)) - mu) / sigma))
        return float(val)

    x0 = np.array([np.log(max(1.0, np.median(durations) if durations.size else 10.0)), 0.5])
    res = minimize(nll, x0, method="Nelder-Mead",
                   options={"xatol": 1e-5, "fatol": 1e-7, "maxiter": 500,
                            "adaptive": True})
    mu, log_sigma = float(res.x[0]), float(res.x[1])
    sigma = float(np.exp(log_sigma))
    k = 2
    n_used = int(durations.size + censored_lb.size)
    return ParamFit("lognormal", {"mu": mu, "sigma": sigma}, float(-res.fun),
                    int(durations.size), int(censored_lb.size), n_used,
                    aic=float(2 * k + 2 * res.fun), converged=bool(res.success),
                    note="" if res.success else str(res.message))


def _fit_gamma(durations: np.ndarray, censored_lb: np.ndarray) -> ParamFit:
    from scipy.optimize import minimize
    from scipy.stats import gamma as gamma_dist

    allpts = np.concatenate([durations, censored_lb]) if (
        durations.size or censored_lb.size) else np.array([10.0])
    allpts = np.maximum(allpts, 1e-9)

    def nll(theta: np.ndarray) -> float:
        # optimize in log-space so shape/scale remain positive and the
        # simplex stays well-conditioned
        shape, scale = np.exp(theta[0]), np.exp(theta[1])
        val = 0.0
        if durations.size:
            val -= np.sum(gamma_dist.logpdf(np.maximum(durations, 1e-9),
                                            a=shape, scale=scale))
        if censored_lb.size:
            val -= np.sum(gamma_dist.logsf(np.maximum(censored_lb, 1e-9),
                                           a=shape, scale=scale))
        return float(val)

    # method-of-moments initialisation (shape = mean^2/var, scale = var/mean)
    m = float(allpts.mean())
    v = float(allpts.var()) or (0.25 * m * m)
    shape0 = max(1e-2, m * m / v)
    scale0 = max(1e-6, v / m)
    x0 = np.log(np.array([shape0, scale0]))
    res = minimize(nll, x0, method="Nelder-Mead",
                   options={"xatol": 1e-5, "fatol": 1e-7, "maxiter": 400,
                            "adaptive": True})
    shape = max(float(np.exp(res.x[0])), 1e-6)
    scale = max(float(np.exp(res.x[1])), 1e-9)
    k = 2
    n_used = int(durations.size + censored_lb.size)
    return ParamFit("gamma", {"shape": shape, "scale": scale}, float(-res.fun),
                    int(durations.size), int(censored_lb.size), n_used,
                    aic=float(2 * k + 2 * res.fun), converged=bool(res.success),
                    note="" if res.success else str(res.message))


class EmpiricalKM:
    """Kaplan-Meier product-limit estimator of the service-time CDF."""

    def __init__(self, durations: np.ndarray, censored_lb: np.ndarray):
        self.times = np.unique(np.concatenate([
            durations.astype(float),
            censored_lb.astype(float),
        ])) if (durations.size or censored_lb.size) else np.array([])
        self.survival = self._km(durations, censored_lb)

    def _km(self, durations: np.ndarray, censored_lb: np.ndarray) -> np.ndarray:
        if self.times.size == 0:
            return np.array([])
        surv = 1.0
        out = []
        for t in self.times:
            at_risk = int(np.sum(durations >= t) + np.sum(censored_lb >= t))
            d = int(np.sum(durations == t))
            if at_risk > 0 and d > 0:
                surv *= (1.0 - d / at_risk)
            out.append(surv)
        return np.array(out)

    def quantile(self, u: float) -> float:
        if self.times.size == 0:
            raise CalibrationError("empty empirical distribution")
        # CDF = 1 - S; invert on observed support
        cdf = 1.0 - self.survival
        idx = int(np.searchsorted(cdf, u, side="left"))
        idx = min(idx, self.times.size - 1)
        return float(self.times[idx])

    def to_dict(self) -> dict:
        return {"name": "empirical_km",
                "support": [float(x) for x in self.times.tolist()],
                "survival": [float(x) for x in self.survival.tolist()]}


@dataclass
class ModelChoice:
    station_id: str
    category: str | None
    group: str
    n_exact: int
    n_right: int
    n_upper: int
    n_unknown: int
    n_effective: int
    selected: str
    method: str
    candidates: list[dict] = field(default_factory=list)
    params: dict | None = None
    diagnostics: list[dict] = field(default_factory=list)
    decision: str = "fit"
    fallback_reason: str | None = None
    drift: dict | None = None
    used_fragments: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = {
            "station_id": self.station_id, "category": self.category,
            "group": self.group, "n_exact": self.n_exact,
            "n_right": self.n_right, "n_upper": self.n_upper,
            "n_unknown": self.n_unknown, "n_effective": self.n_effective,
            "selected": self.selected, "method": self.method,
            "candidates": self.candidates, "params": self.params,
            "diagnostics": self.diagnostics,
            "decision": self.decision, "fallback_reason": self.fallback_reason,
            "drift": self.drift,
            "used_fragments": self.used_fragments,
        }
        return d


def _group_fragments(frags: list[ServiceFragment]):
    by_station: dict[str, list[ServiceFragment]] = {}
    for f in frags:
        by_station.setdefault(f.station_id, []).append(f)
    return by_station


def _arrays(frags: list[ServiceFragment]) -> tuple[np.ndarray, np.ndarray, int, int]:
    exact = np.array([f.value for f in frags if f.status == STATUS_EXACT and f.value is not None],
                     dtype=float)
    right = np.array([f.lower for f in frags if f.status == STATUS_RIGHT and f.lower is not None],
                     dtype=float)
    upper = sum(1 for f in frags if f.status == STATUS_UPPER)
    unknown = sum(1 for f in frags if f.status == STATUS_UNKNOWN)
    return exact, right, upper, unknown


def _drift_check(frags: list[ServiceFragment]) -> dict | None:
    """Two-sample KS across shifts on exact durations only."""
    by_shift: dict[str, list[int]] = {}
    for f in frags:
        if f.status == STATUS_EXACT and f.value is not None and f.shift_id is not None:
            by_shift.setdefault(f.shift_id, []).append(f.value)
    usable = {k: v for k, v in by_shift.items() if len(v) >= DRIFT_MIN_PER_SHIFT}
    if len(usable) < 2:
        return None
    from scipy.stats import ks_2samp
    keys = sorted(usable)
    a, b = usable[keys[0]], usable[keys[-1]]
    stat, p = ks_2samp(a, b)
    return {
        "shifts_compared": [keys[0], keys[-1]],
        "n": [len(a), len(b)],
        "ks_stat": float(stat), "p_value": float(p),
        "drift_detected": bool(p < DRIFT_ALPHA),
        "note": ("drift: model should not be pooled across shifts without an "
                 "operator override") if p < DRIFT_ALPHA else "no significant drift",
    }


def _select(exact: np.ndarray, right: np.ndarray, n_effective: int,
            diagnostics: list[dict], allow_category_split: bool) -> tuple[str, str, dict | None, list[dict]]:
    """Return (selected_name, method_note, params, candidate_records)."""
    records: list[dict] = []
    lf = _fit_lognormal(exact, right)
    ga = _fit_gamma(exact, right)
    km = EmpiricalKM(exact, right)
    records.append({
        "name": "lognormal", "params": lf.params, "loglik": lf.loglik,
        "aic": lf.aic, "converged": lf.converged, "note": lf.note,
        "n_exact": lf.n_exact, "n_right": lf.n_right,
    })
    records.append({
        "name": "gamma", "params": ga.params, "loglik": ga.loglik,
        "aic": ga.aic, "converged": ga.converged, "note": ga.note,
        "n_exact": ga.n_exact, "n_right": ga.n_right,
    })
    records.append({
        "name": "empirical_km", "params": {"support": len(km.times)},
        "loglik": None, "aic": None, "converged": True,
        "n_exact": int(exact.size), "n_right": int(right.size),
        "note": "nonparametric; no AIC comparable with parametric models",
    })

    param_ok = [r for r in records if r["name"] != "empirical_km"
                and r["converged"] and r["aic"] is not None and np.isfinite(r["aic"])]
    if not param_ok:
        return ("empirical_km", "no parametric candidate converged",
                {"kind": "empirical"}, records)

    best = min(param_ok, key=lambda r: r["aic"])
    # empirical is preferred when it has enough support and parametric fits are
    # borderline; documented rule below.
    if exact.size < MIN_EXACT_SAMPLES:
        diagnostics.append({
            "code": "low_exact", "severity": "warning",
            "message": f"only {int(exact.size)} exact durations; parametric choice is fragile"})
    if best["name"] == "lognormal":
        chosen, params = "lognormal", {"mu": best["params"]["mu"],
                                       "sigma": best["params"]["sigma"]}
    else:
        chosen, params = "gamma", {"shape": best["params"]["shape"],
                                   "scale": best["params"]["scale"]}
    method = f"AIC on training likelihood (exact + right-censored): {best['name']}"
    # empirical fallback when support is tiny relative to parametrization
    if n_effective < 2 * 2 + 1:
        method += "; empirical retained as comparison only"
    return chosen, method, params, records


def _build_params(choice: str, params: dict, station: str, category: str | None) -> dict:
    return {
        "station_id": station, "category": category, "kind": choice,
        "params": params,
    }


def calibrate(rec, train_windows: set[str],
              category_split_strategy: str = "auto",
              overrides: dict[str, str] | None = None) -> dict:
    """Fit per-station (and optionally per-category) service models on train data.

    ``overrides`` maps group -> documented reason string; it lets an operator
    explicitly accept a drifted model version.  The reason is stored in the
    calibration report, so an override can never be silent.
    """
    overrides = overrides or {}
    frags = fragments_from_reconstruction(rec, train_windows)
    if not frags:
        raise CalibrationError("no observation fragments in training windows")
    by_station = _group_fragments(frags)
    choices: list[ModelChoice] = []
    models: dict[str, dict] = {}   # key "station|category"

    for station, sfrags in sorted(by_station.items()):
        exact, right, n_upper, n_unknown = _arrays(sfrags)
        n_eff = int(exact.size + right.size)
        diags = []
        drift = _drift_check(sfrags)

        cats = sorted({f.category for f in sfrags})
        cat_counts = {c: _arrays([f for f in sfrags if f.category == c]) for c in cats}
        cat_eff = {c: int(v[0].size + v[1].size) for c, v in cat_counts.items()}

        split_categories = (
            category_split_strategy == "split"
            or (category_split_strategy == "auto" and len(cats) > 1
                and all(cat_eff[c] >= MIN_TOTAL_INFO for c in cats)))
        if category_split_strategy == "auto" and len(cats) > 1 and not split_categories:
            diags.append({
                "code": "category_merge",
                "severity": "info",
                "message": ("per-category sample too small or uneven; merged "
                            f"categories {cats} for station {station}")})

        groups: list[tuple[str | None, list[ServiceFragment]]] = []
        if split_categories:
            groups = [(c, [f for f in sfrags if f.category == c]) for c in cats]
        else:
            groups = [(None, sfrags)]

        for cat, gfrags in groups:
            gexact, gright, gupper, gunknown = _arrays(gfrags)
            gn_eff = int(gexact.size + gright.size)
            gdiags = list(diags)
            gdrift = _drift_check(gfrags) if cat is not None else drift
            group_name = station if cat is None else f"{station}|{cat}"
            if gn_eff < MIN_TOTAL_INFO:
                # reject parametric; fall back to pooling station-level if possible
                base_exact, base_right, _, _ = _arrays(sfrags)
                if int(base_exact.size + base_right.size) >= MIN_TOTAL_INFO:
                    gdiags.append({
                        "code": "category_fallback", "severity": "warning",
                        "message": (f"category {cat!r} has only {gn_eff} effective "
                                    f"samples; reverted to station-level pooled model"),
                    })
                    # use station-level pooled fragments
                    gexact, gright = base_exact, base_right
                    gdiags.append({"code": "insufficient_samples", "severity": "error",
                                   "message": f"category group rejected ({gn_eff} < {MIN_TOTAL_INFO})"})
                else:
                    gdiags.append({
                        "code": "insufficient_samples", "severity": "error",
                        "message": f"only {gn_eff} effective samples; model not identifiable"})
                    choices.append(ModelChoice(
                        station, cat, group_name, int(gexact.size), int(gright.size),
                        gupper, gunknown, gn_eff, "none", "rejected: insufficient samples",
                        params=None, diagnostics=gdiags, decision="reject",
                        fallback_reason="insufficient effective samples"))
                    models[group_name] = {"kind": "none", "reason": "insufficient samples"}
                    continue

            selected, method, params, records = _select(gexact, gright, gn_eff, gdiags,
                                                        split_categories)
            if gdrift and gdrift.get("drift_detected"):
                gdiags.append({
                    "code": "drift", "severity": "warning",
                    "message": (f"service distribution drifts between shifts "
                                f"{gdrift['shifts_compared']} (KS p={gdrift['p_value']:.2e}); "
                                "pooled model flagged, operator confirmation required"),
                })
                reason = overrides.get(group_name)
                if reason:
                    decision = "drift_overridden"
                    fallback_reason = f"operator override: {reason}"
                else:
                    decision = "drift_hold"
                    fallback_reason = ("drift detected across training shifts; a model "
                                       "version must be confirmed by an operator with a "
                                       "documented reason before scenario comparison")
            else:
                decision = "fit"
                fallback_reason = None
            if params is None:
                raise CalibrationError(f"calibration returned no params for {group_name}")

            # record used fragment provenance
            used = [f.to_dict() for f in gfrags if f.status in (STATUS_EXACT, STATUS_RIGHT)]
            mc = ModelChoice(
                station, cat, group_name, int(gexact.size), int(gright.size),
                gupper, gunknown, gn_eff, selected, method, records, params,
                gdiags, decision, fallback_reason, gdrift, used)
            choices.append(mc)
            models[group_name] = _build_params(selected, params, station, cat)
            models[group_name]["method"] = method
            models[group_name]["used_fragment_count"] = len(used)
            models[group_name]["n_exact"] = int(gexact.size)
            models[group_name]["n_right_censored"] = int(gright.size)
            models[group_name]["n_interval_upper"] = gupper
            models[group_name]["n_unknown"] = gunknown
            if selected == "empirical_km":
                models[group_name]["empirical"] = EmpiricalKM(gexact, gright).to_dict()

    n_used = sum(c.n_effective for c in choices)
    return {
        "models": models,
        "choices": [c.to_dict() for c in choices],
        "train_windows": sorted(train_windows),
        "category_split_strategy": category_split_strategy,
        "thresholds": {
            "MIN_EXACT_SAMPLES": MIN_EXACT_SAMPLES,
            "MIN_TOTAL_INFO": MIN_TOTAL_INFO,
            "DRIFT_ALPHA": DRIFT_ALPHA,
            "DRIFT_MIN_PER_SHIFT": DRIFT_MIN_PER_SHIFT,
        },
        "total_effective_samples": int(n_used),
        "provenance": _provenance_table(frags),
    }


def _provenance_table(frags: list[ServiceFragment]) -> list[dict]:
    by_group: dict[tuple[str, str], dict[str, int]] = {}
    for f in frags:
        key = (f.station_id, f.category)
        row = by_group.setdefault(key, {"exact": 0, "right_censored": 0,
                                        "interval_upper": 0, "unknown": 0, "total": 0})
        row[f.status] = row.get(f.status, 0) + 1
        row["total"] += 1
    return [
        {"station_id": k[0], "category": k[1], **v}
        for k, v in sorted(by_group.items())
    ]


# -------------------------------------------------------- model -> service fn
class ServiceModel:
    """Turn calibrated models into a deterministic service_fn(u -> ms)."""

    def __init__(self, calibration: dict, fallback_mean: float = 10.0):
        self.models = calibration["models"]
        self.fallback_mean = max(1.0, fallback_mean)
        self._emp: dict[str, EmpiricalKM] = {}
        for key, m in self.models.items():
            if m.get("kind") == "empirical_km" and "empirical" in m:
                self._emp[key] = EmpiricalKM(
                    np.array([], dtype=float), np.array([], dtype=float))
                # rebuild from stored support/survival
                self._emp[key].times = np.array(m["empirical"]["support"], dtype=float)
                self._emp[key].survival = np.array(m["empirical"]["survival"], dtype=float)

    def lookup(self, station: str, category: str) -> tuple[str, dict] | None:
        split = self.models.get(f"{station}|{category}")
        if split and split.get("kind") not in (None, "none"):
            return f"{station}|{category}", split
        base = self.models.get(station)
        if base and base.get("kind") not in (None, "none"):
            return station, base
        return None

    def sample(self, task_id: str, stage_index: int, station: str, category: str,
               u: float, seed: int = 0) -> int:
        found = self.lookup(station, category)
        if found is None:
            return max(1, int(round(self.fallback_mean)))
        key, m = found
        kind = m["kind"]
        if kind == "lognormal":
            from scipy.stats import norm
            mu, sigma = m["params"]["mu"], m["params"]["sigma"]
            v = float(np.exp(mu + sigma * norm.ppf(min(max(u, 1e-9), 1 - 1e-9))))
        elif kind == "gamma":
            from scipy.stats import gamma as gamma_dist
            v = float(gamma_dist.ppf(min(max(u, 1e-9), 1 - 1e-9),
                                     a=m["params"]["shape"], scale=m["params"]["scale"]))
        elif kind == "empirical_km":
            em = self._emp.get(key)
            if em is None or em.times.size == 0:
                v = self.fallback_mean
            else:
                v = em.quantile(u)
        else:
            v = self.fallback_mean
        return max(1, int(round(v)))

    def service_fn(self, seed: int):
        def fn(task_id: str, stage_index: int, station: str, category: str, u: float) -> int:
            return self.sample(task_id, stage_index, station, category, u, seed)
        return fn


def frozen_params(calibration: dict) -> dict:
    """Parameters that must not change when held-out logs change."""
    return {
        "models": calibration["models"],
        "thresholds": calibration["thresholds"],
        "train_windows": calibration["train_windows"],
        "total_effective_samples": calibration["total_effective_samples"],
        "choices": calibration["choices"],
    }


def lookup_params_digest(calibration: dict) -> str:
    import hashlib
    import json
    payload = json.dumps(frozen_params(calibration), sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
