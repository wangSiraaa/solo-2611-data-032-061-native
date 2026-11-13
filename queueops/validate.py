"""Held-out validation: drive the calibrated model with held-out real arrivals.

Contract
--------
* Only the *last* window(s) designated as hold-out are used here.
* Calibration parameters are frozen before validation; their digest is checked
  afterwards, so held-out data cannot silently leak into the fit.
* Simulation uses the same shift seat/wait configuration as the held-out shift.
* We compare throughput, per-station queue waits, service occupancy and
  blocking against reconstructed held-out observations and list differences
  the model cannot explain (boundary carry-over, drift, censoring, missing
  events).
"""
from __future__ import annotations

import numpy as np

from .calibrate import ServiceModel, frozen_params, lookup_params_digest
from .common import STATUS_EXACT
from .config import Config
from .sim import Simulation, stable_uniform

# relative mean-service gap above which a station is flagged as unexplained drift
SIM_SERVICE_DIFF_WARN = 0.10


def _arrivals_from_recon(rec, cfg: Config, window_start: int, window_end: int) -> list[dict]:
    """Real held-out arrivals: stage-0 arrival (or inferred entry) in the window."""
    out = []
    for task in rec.tasks.values():
        st0 = task.stages[0]
        ts = st0.arrive_ts
        if ts is None:
            ts = st0.enqueue_ts
        if ts is None:
            continue
        if window_start <= ts < window_end:
            out.append({"task_id": task.task_id, "arrive_ts": int(ts)})
    return out


def _observed_metrics(rec, cfg: Config, window_start: int, window_end: int) -> dict:
    """Reconstructed held-out truth used ONLY for comparison."""
    done = []
    queue: dict[str, list[int]] = {}
    blocking: dict[str, list[int]] = {}
    service: dict[str, list[int]] = {}
    service_sc: dict[str, list[int]] = {}
    for task in rec.tasks.values():
        st0 = task.stages[0]
        ts0 = st0.arrive_ts if st0.arrive_ts is not None else st0.enqueue_ts
        if ts0 is None or not (window_start <= ts0 < window_end):
            continue
        final = task.stages[-1]
        if final.exit_ts is not None and final.exit_ts <= window_end:
            done.append(task.task_id)
        for st in task.stages:
            if st.start_ts is not None and window_start <= st.start_ts < window_end:
                if st.queue_status == STATUS_EXACT and st.queue_wait_ms is not None:
                    queue.setdefault(st.station_id, []).append(st.queue_wait_ms)
                if st.service_status == STATUS_EXACT and st.service_ms is not None:
                    service.setdefault(st.station_id, []).append(st.service_ms)
                    service_sc.setdefault(f"{st.station_id}|{st.category}",
                                          []).append(st.service_ms)
                if (st.blocking_status == STATUS_EXACT and st.blocking_ms is not None):
                    blocking.setdefault(st.station_id, []).append(st.blocking_ms)
    # carried-over tasks: genuinely active across the window start, i.e. a task
    # with at least one event at/after window_start but that entered earlier.
    # (Censored backlog from an earlier, independently-run shift is NOT counted.)
    carried = 0
    for task in rec.tasks.values():
        st0 = task.stages[0]
        ts0 = st0.arrive_ts if st0.arrive_ts is not None else st0.enqueue_ts
        if ts0 is None or ts0 >= window_start:
            continue
        has_event_after = any(
            ev.ts >= window_start for st in task.stages for ev in st.events)
        if not has_event_after:
            continue
        still_active = any(
            st.start_ts is not None and st.complete_ts is None
            for st in task.stages)
        if still_active or task.final_status not in ("finished",):
            carried += 1

    def q(vals):
        if not vals:
            return {"n": 0}
        a = np.asarray(vals, dtype=float)
        return {"n": int(a.size), "mean": float(a.mean()),
                "p50": float(np.percentile(a, 50)), "p90": float(np.percentile(a, 90))}

    # observed seat occupancy: service and blocked seat-ms over the window,
    # divided by configured available seat-ms for the shift.
    shift = None
    for sh in cfg.shifts.values():
        if sh.window_start == window_start and sh.window_end == window_end:
            shift = sh
            break
    horizon_ms = max(1, window_end - window_start)
    occ = {}
    for sid in cfg.station_order:
        seats = cfg.seats_for(shift.shift_id, sid) if shift else cfg.stations[sid].seats_capacity
        service_ms = 0
        blocked_ms = 0
        for task in rec.tasks.values():
            for st in task.stages:
                if st.station_id != sid or st.start_ts is None:
                    continue
                # only stages that START inside this window contribute; earlier
                # shifts' censored stages are not part of this window's occupancy
                if not (window_start <= st.start_ts < window_end):
                    continue
                end_svc = st.complete_ts
                if end_svc is None:
                    # no observed completion: bound occupancy by the first known
                    # release (leave/exit) if present, else the window end
                    release = [t for t in (st.leave_ts, st.exit_ts) if t is not None]
                    end_svc = min(release) if release else window_end
                lo = max(st.start_ts, window_start)
                hi = min(end_svc, window_end)
                if hi > lo:
                    service_ms += hi - lo
                if st.complete_ts is not None and st.has_next:
                    end_blk = st.leave_ts if st.leave_ts is not None else window_end
                    lo = max(st.complete_ts, window_start)
                    hi = min(end_blk, window_end)
                    if hi > lo:
                        blocked_ms += hi - lo
        seat_ms = horizon_ms * seats
        occ[sid] = {
            "seats": seats,
            "service_occupancy_ratio": service_ms / seat_ms if seat_ms else None,
            "blocked_occupancy_ratio": blocked_ms / seat_ms if seat_ms else None,
        }

    return {
        "occupancy": occ,
        "throughput": len(done),
        "queue_wait": {s: q(v) for s, v in queue.items()},
        "service_time": {s: q(v) for s, v in service.items()},
        "service_time_by_category": {s: q(v) for s, v in service_sc.items()},
        "blocking": {s: q(v) for s, v in blocking.items()},
        "carried_over_tasks": carried,
    }


def _sim_metrics(run: dict, station_order: list[str], window_start: int, window_end: int) -> dict:
    stats = run["stats"]
    return {
        "throughput": stats["tasks_done"],
        "queue_wait": {s: stats["stations"][s]["queue_wait"] for s in station_order},
        "service_time": {s: stats["stations"][s]["service"] for s in station_order},
        "service_by_category": stats.get("service_by_category", {}),
        "blocking": {s: stats["stations"][s]["blocking"] for s in station_order},
        "seat_utilization": stats["seat_utilization"],
        "censored": stats["tasks_censored"],
    }


def validate(rec, cfg: Config, calibration: dict, holdout_windows: set[str],
             seed: int = 12345) -> dict:
    digest_before = lookup_params_digest(calibration)
    frozen = frozen_params(calibration)   # snapshot; must be unchanged afterwards

    holdout_shifts = [sh for sh in cfg.shifts.values() if sh.shift_id in holdout_windows]
    if not holdout_shifts:
        raise ValueError("no holdout windows selected")
    results = []
    model = ServiceModel(calibration)

    for sh in sorted(holdout_shifts, key=lambda s: s.window_start):
        arrivals = _arrivals_from_recon(rec, cfg, sh.window_start, sh.window_end)
        observed = _observed_metrics(rec, cfg, sh.window_start, sh.window_end)
        sim = Simulation(cfg, model.service_fn(seed), seed, arrivals,
                         end_policy="cutoff",
                         start_time=sh.window_start, end_time=sh.window_end)
        run = sim.run()
        simm = _sim_metrics(run, list(cfg.station_order), sh.window_start, sh.window_end)

        unexplained = []
        if observed["carried_over_tasks"]:
            unexplained.append(
                f"{observed['carried_over_tasks']} task(s) were already in the system at "
                f"{sh.shift_id} start; the simulation starts empty and therefore cannot "
                f"reproduce their contribution (boundary effect).")
        if simm["censored"]:
            unexplained.append(
                f"{simm['censored']} task(s) unfinished at window end are right-censored, "
                "not counted as completions.")
        # service-time mean comparison per (station, category): a large gap
        # signals a service-distribution change (drift) that the frozen model
        # cannot reproduce.
        obs_sc = observed.get("service_time_by_category", {})
        sm_sc = simm.get("service_by_category", {})
        for key, o in obs_sc.items():
            m = sm_sc.get(key, {"n": 0})
            if o.get("n") and m.get("n") and o.get("mean") and m.get("mean"):
                rel = (m["mean"] - o["mean"]) / o["mean"]
                if abs(rel) > SIM_SERVICE_DIFF_WARN:
                    sid, cat = key.split("|", 1)
                    unexplained.append(
                        f"{sid}/{cat}: simulated mean service {m['mean']:.1f} ms vs "
                        f"observed {o['mean']:.1f} ms ({rel:+.0%}); likely a "
                        f"service-distribution change (drift) not represented by the "
                        f"frozen model.")
        # per-station deltas
        per_station = {}
        for s in cfg.station_order:
            obs_q = observed["queue_wait"].get(s, {"n": 0})
            sm_q = simm["queue_wait"][s]
            obs_b = observed["blocking"].get(s, {"n": 0})
            sm_b = simm["blocking"][s]
            per_station[s] = {
                "queue_wait_obs": obs_q, "queue_wait_sim": sm_q,
                "queue_mean_delta": (sm_q.get("mean") - obs_q.get("mean")
                                     if obs_q.get("n") and sm_q.get("n") else None),
                "blocking_obs": obs_b, "blocking_sim": sm_b,
                "blocking_mean_delta": (sm_b.get("mean") - obs_b.get("mean")
                                        if obs_b.get("n") and sm_b.get("n") else None),
                "service_occupancy_obs": observed["occupancy"].get(s, {}).get(
                    "service_occupancy_ratio"),
                "service_occupancy_sim": simm["seat_utilization"][s].get(
                    "service_occupancy_ratio"),
                "blocked_occupancy_obs": observed["occupancy"].get(s, {}).get(
                    "blocked_occupancy_ratio"),
                "blocked_occupancy_sim": simm["seat_utilization"][s].get(
                    "blocked_occupancy_ratio"),
            }
        results.append({
            "shift_id": sh.shift_id, "window": [sh.window_start, sh.window_end],
            "arrivals": len(arrivals),
            "observed": observed,
            "simulated": simm,
            "throughput_delta": simm["throughput"] - observed["throughput"],
            "per_station": per_station,
            "unexplained_differences": unexplained,
            "sim_run_meta": run["meta"],
            "deadlock": run["deadlock"],
        })

    digest_after = lookup_params_digest(calibration)
    if digest_after != digest_before:
        raise RuntimeError("calibration parameters changed during validation")
    # make sure the frozen snapshot equals the live object
    live = frozen_params(calibration)
    if live != frozen:
        raise RuntimeError("held-out data leaked into calibration")
    return {
        "holdout_windows": sorted(holdout_windows),
        "calibration_digest": digest_before,
        "calibration_digest_after": digest_after,
        "frozen": True,
        "results": results,
        "seed": seed,
    }


def _unconfirmed_models(calibration: dict, confirm: dict) -> list[dict]:
    """Groups whose version is uncertain and not accepted explicitly."""
    accepted = set(confirm.get("accept_groups") or [])
    blocked = []
    for c in calibration.get("choices", []):
        if c.get("decision") in ("drift_hold", "reject") or (
                c.get("decision") == "drift_overridden"):
            group = c["group"]
            if c.get("decision") == "drift_overridden":
                # already overridden at calibration time with a recorded reason
                continue
            if group in accepted:
                continue
            blocked.append({
                "group": group, "decision": c.get("decision"),
                "reason": c.get("fallback_reason"),
            })
    return blocked


def capacity_plan_comparison(rec, cfg: Config, calibration: dict,
                             base_shift_id: str, scenarios: list[dict],
                             arrivals_window: tuple[int, int] | None = None,
                             seeds: list[int] | None = None,
                             confirm: dict | None = None,
                             validation: dict | None = None) -> dict:
    """Compare capacity scenarios using identical arrivals and base draws.

    ``scenarios`` items: {"name", "shift_id", "seats": {station: n},
    "wait_capacity": {station: n}}.

    A model version whose groups are ``drift_hold`` or ``reject`` must be
    explicitly confirmed by an operator (``confirm={"by", "reason", "accept_groups"}``)
    after calibration diagnostics are reviewed; otherwise comparison refuses.
    """
    confirm = confirm or {}
    blocked = _unconfirmed_models(calibration, confirm)
    if blocked:
        return {
            "refused": True,
            "reason": ("uncertain model versions require operator confirmation "
                       "after reviewing calibration diagnostics"),
            "blocked_groups": blocked,
            "how_to_confirm": ("pass confirm={\"by\":..,\"reason\":..,"
                               "\"accept_groups\":[...]} or set the corresponding "
                               "override when calibrating"),
            "summary": [], "runs": [], "invariance": {},
        }
    seeds = seeds or [12345]
    sh = cfg.shifts[base_shift_id]
    ws, we = arrivals_window or (sh.window_start, sh.window_end)
    arrivals = _arrivals_from_recon(rec, cfg, ws, we)
    model = ServiceModel(calibration)
    all_runs = []
    for scen in scenarios:
        per_seed = []
        for seed in seeds:
            sim = Simulation(
                cfg, model.service_fn(seed), seed, arrivals, end_policy="cutoff",
                start_time=ws, end_time=we,
                shift_override={scen.get("shift_id", base_shift_id): {
                    "seats": scen.get("seats") or {},
                    "wait_capacity": scen.get("wait_capacity") or {},
                }})
            run = sim.run()
            # stable base sample check: same (task,stage) -> same base uniform
            base_samples = {(t["task_id"], st["stage_index"]): st["base_uniform"]
                            for t in run["tasks"] for st in t["stages"]
                            if st["base_uniform"] is not None}
            per_seed.append({
                "seed": seed, "run": run, "base_samples": base_samples,
            })
        all_runs.append({"scenario": scen, "per_seed": per_seed})

    # verify base-sample invariance across scenarios per seed
    invariance = {}
    for seed_idx, seed in enumerate(seeds):
        ref = None
        ok = True
        for entry in all_runs:
            samples = entry["per_seed"][seed_idx]["base_samples"]
            if ref is None:
                ref = samples
                continue
            common = set(ref) & set(samples)
            for k in common:
                if abs(ref[k] - samples[k]) > 1e-15:
                    ok = False
        invariance[str(seed)] = {"identical_base_draws": ok,
                                 "common_stages": len(set(ref or {}) &
                                                      set(all_runs[0]["per_seed"][seed_idx]["base_samples"]))}

    # aggregate across seeds
    summary = []
    for entry in all_runs:
        runs = [p["run"] for p in entry["per_seed"]]
        thr = [r["stats"]["tasks_done"] for r in runs]
        waits = {s: [r["stats"]["stations"][s]["queue_wait"]["mean"]
                     if r["stats"]["stations"][s]["queue_wait"]["n"] else None
                     for r in runs] for s in cfg.station_order}
        block = {s: [r["stats"]["stations"][s]["blocking"]["mean"]
                     if r["stats"]["stations"][s]["blocking"]["n"] else None
                     for r in runs] for s in cfg.station_order}
        summary.append({
            "scenario": entry["scenario"],
            "throughput_mean": float(np.mean(thr)),
            "throughput_std": float(np.std(thr)),
            "throughput_values": thr,
            "queue_wait_mean_by_station": {
                s: (float(np.nanmean([v for v in vals if v is not None]))
                    if any(v is not None for v in vals) else None)
                for s, vals in waits.items()},
            "blocking_mean_by_station": {
                s: (float(np.nanmean([v for v in vals if v is not None]))
                    if any(v is not None for v in vals) else None)
                for s, vals in block.items()},
            "censored_mean": float(np.mean([r["stats"]["tasks_censored"] for r in runs])),
        })
    # Explicit trade-off narrative vs the baseline scenario (first in list,
    # or the scenario literally named "baseline").
    base_name = scenarios[0]["name"]
    for sc in scenarios:
        if sc["name"] == "baseline":
            base_name = "baseline"
            break
    base_row = next((r for r in summary if r["scenario"]["name"] == base_name), None)
    tradeoffs = []
    if base_row is not None:
        for row in summary:
            if row is base_row:
                continue
            qw_delta = {}
            bl_delta = {}
            for s in cfg.station_order:
                bq = base_row["queue_wait_mean_by_station"].get(s)
                nq = row["queue_wait_mean_by_station"].get(s)
                qw_delta[s] = (None if (bq is None or nq is None) else nq - bq)
                bb = base_row["blocking_mean_by_station"].get(s)
                nb = row["blocking_mean_by_station"].get(s)
                bl_delta[s] = (None if (bb is None or nb is None) else nb - bb)
            improved = [s for s, d in qw_delta.items() if d is not None and d < -0.05]
            worsened = [s for s, d in qw_delta.items() if d is not None and d > 0.05]
            tradeoffs.append({
                "scenario": row["scenario"]["name"],
                "vs": base_name,
                "throughput_delta": row["throughput_mean"] - base_row["throughput_mean"],
                "censored_delta": row["censored_mean"] - base_row["censored_mean"],
                "queue_wait_delta": qw_delta,
                "blocking_delta": bl_delta,
                "wait_improved_at": improved,
                "wait_transferred_to": worsened,
                "explanation": _explain(base_row, row, qw_delta, bl_delta,
                                        improved, worsened, cfg.station_order),
            })
    return {
        "base_shift": base_shift_id,
        "window": [ws, we],
        "seeds": seeds,
        "arrivals": len(arrivals),
        "invariance": invariance,
        "summary": summary,
        "tradeoffs": tradeoffs,
        "runs": [{"scenario": e["scenario"],
                  "seed": p["seed"],
                  "stats": p["run"]["stats"],
                  # per-station stepwise occupancy intervals (service vs blocked)
                  "stations": p["run"]["stations"],
                  "deadlock": p["run"]["deadlock"]}
                 for e in all_runs for p in e["per_seed"]],
        # Per-task simulated events for independent recomputation are exported
        # by the `simulate` command (simulation_events.csv), not embedded here.
    }


def _explain(base_row, row, qw_delta, bl_delta, improved, worsened, order) -> str:
    name = row["scenario"]["name"]
    dt = row["throughput_mean"] - base_row["throughput_mean"]
    parts = [f"{name}: throughput {dt:+.1f} vs baseline"]
    if improved:
        parts.append("wait falls at " + ", ".join(
            f"{s} ({qw_delta[s]:+.2f} ms)" for s in improved))
    if worsened:
        parts.append("cost transfers to " + ", ".join(
            f"{s} ({qw_delta[s]:+.2f} ms)" for s in worsened))
    if not improved and not worsened:
        parts.append("no material wait change at any station")
    bl_up = [s for s in order if bl_delta.get(s) is not None and bl_delta[s] > 0.05]
    if bl_up:
        parts.append("blocking rises at " + ", ".join(
            f"{s} ({bl_delta[s]:+.2f} ms)" for s in bl_up))
    if row["censored_mean"] > base_row["censored_mean"] + 1:
        parts.append(f"more work censored (+{row['censored_mean'] - base_row['censored_mean']:.1f})")
    return "; ".join(parts)
