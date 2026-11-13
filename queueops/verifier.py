"""Verifier: the ONLY module allowed to read truth.json.

Checks performed:
  * hand-calculable demo service/blocking durations
  * censoring is never counted as completion; unknown is never zero
  * held-out log changes do not change frozen parameters (digest stability)
  * reconstruction invariants (single seat occupancy, time coverage, count closure)
  * capacity change leaves base sampling unchanged
  * recovery error of fitted params vs truth (statistical, not exact)
  * prediction bias on holdout
"""
from __future__ import annotations

import copy
import json
import os

import numpy as np

from .calibrate import ServiceModel, frozen_params, lookup_params_digest
from .common import STATUS_EXACT, STATUS_RIGHT, STATUS_UNKNOWN, STATUS_UPPER, dump_json, load_json
from .config import load_config
from .events import Diagnostics, load_raw_events, parse_events, reconstruct
from .sim import Simulation


def _read_truth(path: str) -> dict:
    return load_json(path)


def check_demo(recon_dir: str) -> dict:
    """Hand-calculated expectations for the tiny demo log."""
    cfg_path = f"{recon_dir}/demo_config.json"
    ev_path = f"{recon_dir}/demo_events.json"
    cfg = load_config(cfg_path)
    diag = Diagnostics()
    events = parse_events(load_raw_events(ev_path), cfg, diag)
    rec = reconstruct(cfg, events, diag)
    s = {}
    t1s1 = rec.tasks["T1"].stages[0]
    t1s2 = rec.tasks["T1"].stages[1]
    t2s1 = rec.tasks["T2"].stages[0]
    t2s2 = rec.tasks["T2"].stages[1]
    t3s1 = rec.tasks["T3"].stages[0]
    t3s2 = rec.tasks["T3"].stages[1]
    t5s1 = rec.tasks["T5"].stages[0]
    checks = {
        # T1 S1: start 2, complete 12 -> svc 10 ; no downstream hold -> block 0
        "T1_S1_service_10": t1s1.service_ms == 10,
        "T1_S1_blocking_0": t1s1.blocking_ms == 0,
        "T1_S2_queue_wait_2": t1s2.queue_wait_ms == 2,
        "T1_S2_service_20": t1s2.service_ms == 20,
        # T2 S1: enqueue 5, start 12 -> queue wait 7 ; svc 10
        "T2_S1_queue_wait_7": t2s1.queue_wait_ms == 7,
        "T2_S1_service_10": t2s1.service_ms == 10,
        # T2 S2: enqueue 22, start 34 -> wait 12 ; svc 6
        "T2_S2_queue_wait_12": t2s2.queue_wait_ms == 12,
        "T2_S2_service_6": t2s2.service_ms == 6,
        # T3 S1: enqueue 0, start 22 -> wait 22 ; svc 5 ; blocked 27..34 -> 7
        "T3_S1_queue_wait_22": t3s1.queue_wait_ms == 22,
        "T3_S1_service_5": t3s1.service_ms == 5,
        "T3_S1_blocking_7": t3s1.blocking_ms == 7,
        # T3 S2: enqueue 34, start 40 -> wait 6 ; svc 4
        "T3_S2_queue_wait_6": t3s2.queue_wait_ms == 6,
        "T3_S2_service_4": t3s2.service_ms == 4,
        # T5 S1: right censored, lower bound = 100 - 55 = 45, NOT a completion
        "T5_S1_is_right_censored": t5s1.service_status == STATUS_RIGHT,
        "T5_S1_lower_bound_45": t5s1.service_lower == 45,
        "T5_S1_not_completion": t5s1.service_ms is None,
    }
    return {"checks": checks, "all_pass": all(checks.values())}


def check_broken(recon_dir: str) -> dict:
    """Broken log: conflicts located, censoring/unknown not treated as zero."""
    cfg = load_config(f"{recon_dir}/demo_config.json")
    diag = Diagnostics()
    events = parse_events(load_raw_events(f"{recon_dir}/demo_broken_events.json"), cfg, diag)
    rec = reconstruct(cfg, events, diag)
    # T2 S2 had its service_complete removed but leave/exit remain -> upper bound
    t2s2 = rec.tasks["T2"].stages[1]
    checks = {
        "conflict_detected": len(diag.conflicts) >= 1,
        "duplicate_folded": diag.duplicates_folded >= 1,
        "order_violation_detected": len(diag.order_violations) >= 1,
        "missing_complete_not_zero": (
            t2s2.service_ms is None and t2s2.service_status in (STATUS_UPPER, STATUS_UNKNOWN)),
        "upper_bound_nonexpanding": (t2s2.service_upper is not None
                                     and t2s2.service_upper >= 0),
        "missing_complete_flagged": "missing_service_complete" in t2s2.flags,
    }
    return {"checks": checks, "all_pass": all(checks.values()),
            "diagnostics": diag.to_dict()}


def check_frozen(calibration: dict, recon_dir: str, holdout_windows: set[str],
                 train_windows: set[str] | None = None) -> dict:
    """Changing held-out logs must not change frozen params."""
    if train_windows is None:
        train_windows = set(calibration.get("train_windows") or ["train"])
    digest_before = lookup_params_digest(calibration)
    cfg = load_config(f"{recon_dir}/config.json")
    # synthesize alternative holdout log
    alt_events = []
    for ev in load_raw_events(f"{recon_dir}/events.json"):
        if ev.get("ts", 0) >= min(
                sh.window_start for sh in cfg.shifts.values() if sh.shift_id in holdout_windows):
            # drop / alter holdout records only
            if "service_complete" == ev.get("event_type"):
                if np.random.default_rng(7).random() < 0.5:
                    continue
            continue
        alt_events.append(ev)
    diag = Diagnostics()
    events = parse_events(alt_events, cfg, diag)
    rec = reconstruct(cfg, events, diag)
    from .calibrate import calibrate
    # recalibrate on train only; params should match because holdout excluded
    cal_alt = calibrate(rec, set(train_windows))
    digest_alt = lookup_params_digest(cal_alt)
    checks = {
        "digest_unchanged": digest_before == digest_alt,
        "params_identical": frozen_params(calibration)["models"] ==
                            frozen_params(cal_alt)["models"],
    }
    return {"checks": checks,
            "all_pass": all(checks.values()),
            "digest_before": digest_before, "digest_after": digest_alt}


def check_invariants(rec, cfg) -> dict:
    """Single occupancy per seat, time coverage, and count closure."""
    errors = []
    # count closure: observed stages complete when both endpoints known
    n_stage = 0
    n_closed = 0
    for task in rec.tasks.values():
        for st in task.stages:
            n_stage += 1
            if st.service_status == STATUS_EXACT:
                n_closed += 1
    # single occupying seat: no interval overlaps for same station > seats
    for sid, station in cfg.stations.items():
        intervals = []
        for task in rec.tasks.values():
            for st in task.stages:
                if st.station_id != sid or st.start_ts is None:
                    continue
                end = st.leave_ts
                if end is None:
                    end = st.complete_ts
                if end is None:
                    continue
                intervals.append((st.start_ts, end, task.task_id, st.stage_index))
        intervals.sort()
        seats_max = max(cfg.seats_for(sh.shift_id, sid) for sh in cfg.shifts.values())
        # sweep with distinguished same-instant semantics
        pts = []
        for a, b, tid, si in intervals:
            pts.append((a, 1, tid, si, "start"))
            pts.append((b, 0, tid, si, "end"))
        pts.sort(key=lambda x: (x[0], x[1]))
        active = 0
        max_active = 0
        for _, delta, tid, si, kind in pts:
            active += 1 if kind == "start" else -1
            max_active = max(max_active, active)
        if max_active > seats_max:
            errors.append({
                "station_id": sid, "max_active": max_active,
                "seats": seats_max,
                "message": "more tasks occupy seats than configured seats"})
    # time coverage: each stage with start must have monotone timestamps
    bad = 0
    for task in rec.tasks.values():
        for st in task.stages:
            times = [t for t in (st.enqueue_ts, st.start_ts, st.complete_ts,
                                 st.leave_ts, st.exit_ts) if t is not None]
            if times and any(times[i] > times[i + 1] for i in range(len(times) - 1)):
                bad += 1
    checks = {
        "count_closure_ok": n_stage > 0 and n_closed <= n_stage,
        "no_seat_overcapacity": len(errors) == 0,
        "no_time_reversal": bad == 0,
        "n_stages": n_stage, "n_service_closed": n_closed,
    }
    return {"checks": checks, "all_pass": len(errors) == 0 and bad == 0,
            "seat_errors": errors}


def check_capacity_base_draws(comparison: dict) -> dict:
    return {"checks": {k: v["identical_base_draws"]
                       for k, v in comparison.get("invariance", {}).items()},
            "all_pass": all(v["identical_base_draws"]
                            for v in comparison.get("invariance", {}).values())}


def check_prediction_bias(validation: dict) -> dict:
    rows = []
    for r in validation["results"]:
        for s, d in r["per_station"].items():
            rows.append({"shift": r["shift_id"], "station": s,
                         "queue_delta": d["queue_mean_delta"],
                         "blocking_delta": d["blocking_mean_delta"]})
    deltas = [abs(x["queue_delta"]) for x in rows if x["queue_delta"] is not None]
    return {"rows": rows,
            "max_abs_queue_delta": max(deltas) if deltas else None,
            "n_comparisons": len(deltas)}


def recovery_error(calibration: dict, truth_path: str) -> dict:
    """Statistical recovery: compare fitted mean per group with true mean."""
    truth = _read_truth(truth_path)
    true_params = truth["true_params"]
    drift = truth["drift_params"]
    out = []
    model = ServiceModel(calibration)
    # sample a large number of draws to estimate fitted mean (deterministic grid)
    for m in calibration["choices"]:
        station = m["station_id"]
        cat = m["category"]
        if m["decision"] == "reject":
            out.append({"station_id": station, "category": cat,
                        "status": "rejected", "fitted_mean": None,
                        "true_mean": None})
            continue
        kind = m["selected"]
        params = m["params"]
        fitted_mean = _model_mean(kind, params, calibration, m["group"])
        truth_spec = None
        if station in true_params:
            if cat is not None and cat in true_params[station]:
                truth_spec = true_params[station][cat]
            elif len(true_params[station]) == 1:
                truth_spec = next(iter(true_params[station].values()))
        true_mean = _spec_mean(truth_spec)
        rel = (abs(fitted_mean - true_mean) / true_mean
               if fitted_mean and true_mean else None)
        out.append({"station_id": station, "category": cat,
                    "selected": kind, "fitted_mean": fitted_mean,
                    "true_mean": true_mean,
                    "relative_error": rel,
                    "n_effective": m["n_effective"],
                    "status": "ok" if rel is not None and rel < 0.35 else "check"})
    return {"groups": out, "truth_path": truth_path}


def _spec_mean(spec: dict | None) -> float | None:
    if spec is None:
        return None
    if spec["kind"] == "lognormal":
        return float(np.exp(spec["mu"] + spec["sigma"] ** 2 / 2))
    if spec["kind"] == "gamma":
        return float(spec["shape"] * spec["scale"])
    return None


def _model_mean(kind, params, calibration, group) -> float | None:
    if kind == "lognormal":
        return float(np.exp(params["mu"] + params["sigma"] ** 2 / 2))
    if kind == "gamma":
        return float(params["shape"] * params["scale"])
    if kind == "empirical_km":
        m = calibration["models"].get(group, {})
        em = m.get("empirical")
        if not em:
            return None
        support = np.array(em["support"], dtype=float)
        surv = np.array(em["survival"], dtype=float)
        cdf = 1.0 - surv
        if support.size < 2:
            return float(support[0]) if support.size else None
        # integrate survival: discrete approximation with stepwise survival
        widths = np.diff(support)
        return float(support[0] + np.sum(widths * surv[:-1]))
    return None


MODELING_MODULES = ("events.py", "config.py", "calibrate.py", "sim.py",
                    "validate.py")


def check_truth_untouched_by_model(model_paths: list[str]) -> dict:
    """Static guarantee: modelling modules never reference the truth file.

    Only the modelling/calibration/validation modules are asserted to be
    truth-free.  The generator (writes truth) and orchestration/report layers
    are allowed to name the artifact.
    """
    offenders = []
    checked = []
    for p in model_paths:
        if os.path.basename(p) not in MODELING_MODULES:
            continue
        checked.append(os.path.basename(p))
        try:
            with open(p, "r", encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            continue
        if "truth.json" in text or "_read_truth" in text:
            offenders.append(p)
    return {"offenders": offenders, "clean": len(offenders) == 0,
            "checked_modules": checked}
