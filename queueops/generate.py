"""Synthetic data generator (ground truth lives in a separate file).

The generator is the only component that knows the true service parameters.
It writes ``truth.json`` which the verifier is allowed to read; all modelling
tools are restricted to ``config.json`` + ``events``.

Scenario
--------
3 stations, 2 shifts (train, holdout).  Two task categories with a genuine
mean difference at S2.  Finite wait capacities create downstream blocking.
The held-out shift has more arrivals, a service-time drift at S2, and a
different seat allocation, so a frozen model must be validated out of sample.
"""
from __future__ import annotations

import numpy as np

from .common import (
    EV_ARRIVE, EV_COMPLETE, EV_ENQUEUE, EV_EXIT, EV_LEAVE, EV_START, dump_json,
)
from .sim import Simulation, simulation_event_rows

STATIONS = ["S1", "S2", "S3"]

# station -> category -> true service distribution parameters
TRUE_PARAMS = {
    "S1": {
        "standard": {"kind": "lognormal", "mu": np.log(6.0), "sigma": 0.45},
    },
    "S2": {
        "standard": {"kind": "lognormal", "mu": np.log(9.0), "sigma": 0.40},
        "heavy": {"kind": "lognormal", "mu": np.log(15.0), "sigma": 0.35},
    },
    "S3": {
        "standard": {"kind": "lognormal", "mu": np.log(7.0), "sigma": 0.50},
    },
}

# heldout drift: S2 standard gets slower
DRIFT_PARAMS = {
    "S2": {
        "standard": {"kind": "lognormal", "mu": np.log(12.0), "sigma": 0.42},
    }
}

# mild drift between the two training shifts (S2 standard slower in train_b)
TRAIN_DRIFT_PARAMS = {
    "S2": {
        "standard": {"kind": "lognormal", "mu": np.log(10.5), "sigma": 0.41},
    }
}


def _true_service_fn(params: dict):
    from scipy.stats import norm

    def fn(task_id, stage_index, station, category, u):
        spec = params.get(station, {}).get(category)
        if spec is None:
            # fall back to the station's first declared category
            cats = params.get(station, {})
            spec = cats[sorted(cats)[0]] if cats else {"kind": "lognormal",
                                                        "mu": np.log(8.0), "sigma": 0.4}
        if spec["kind"] == "lognormal":
            uu = min(max(u, 1e-9), 1 - 1e-9)
            return float(np.exp(spec["mu"] + spec["sigma"] * norm.ppf(uu)))
        if spec["kind"] == "gamma":
            from scipy.stats import gamma as gd
            return float(gd.ppf(min(max(u, 1e-9), 1 - 1e-9), a=spec["shape"],
                                scale=spec["scale"]))
        raise ValueError(spec["kind"])
    return fn


# Every arrival is a distinct operational task (unique id).  Sizes are chosen
# so S3 (the single-seat bottleneck) runs hot in the training shift and hotter
# in the held-out shift, producing genuine queues and downstream blocking.
SHIFT_SPEC = {
    "train_a": {
        "window": (0, 16000), "n_tasks": 1300, "tail_burst": 60, "tag": "TA",
        "seats": {"S1": 2, "S2": 2, "S3": 1},
        "wait_capacity": {"S1": 3, "S2": 2, "S3": 2},
    },
    "train_b": {
        "window": (18000, 34000), "n_tasks": 1300, "tail_burst": 60, "tag": "TB",
        "seats": {"S1": 2, "S2": 2, "S3": 1},
        "wait_capacity": {"S1": 3, "S2": 2, "S3": 2},
    },
    "holdout": {
        "window": (38000, 56000), "n_tasks": 1550, "tail_burst": 250, "tag": "HO",
        "seats": {"S1": 2, "S2": 2, "S3": 1},
        "wait_capacity": {"S1": 3, "S2": 2, "S3": 2},
    },
}


def _route_for(i: int) -> tuple[str, tuple[str, ...]]:
    """Deterministic mix: standard, heavy, and rework routes."""
    m = i % 10
    if m < 7:
        cat = "standard"
    else:
        cat = "heavy"
    if i % 17 == 0:
        route = ("S1", "S2", "S2", "S3")   # rework back to S2
    else:
        route = ("S1", "S2", "S3")
    return cat, route


def build_config() -> dict:
    stations = [
        {"station_id": "S1", "seats_capacity": 2},
        {"station_id": "S2", "seats_capacity": 2},
        {"station_id": "S3", "seats_capacity": 1},
    ]
    shifts = []
    tasks = []
    for shift_id, spec in SHIFT_SPEC.items():
        ws, we = spec["window"]
        shifts.append({
            "shift_id": shift_id, "window_start": ws, "window_end": we,
            "seats": spec["seats"], "wait_capacity": spec["wait_capacity"],
        })
        tag = spec["tag"]
        for i in range(spec["n_tasks"]):
            cat, route = _route_for(i)
            tasks.append({
                "task_id": f"{tag}{i:04d}", "priority": (i * 7) % 4,
                "category": cat, "stages": list(route),
            })
    return {"stations": stations, "shifts": shifts, "tasks": tasks}


def build_arrivals(cfg: dict, shift_id: str, seed: int,
                   rate: float | None = None) -> list[dict]:
    """Assign one arrival time per task; ids are unique by construction.

    A small ``tail_burst`` of tasks arrives in the final ~3% of the window so
    that some services cannot finish before the window end: those stages are
    genuinely right-censored in the operational log.
    """
    spec = SHIFT_SPEC[shift_id]
    sh = next(s for s in cfg["shifts"] if s["shift_id"] == shift_id)
    ws, we = sh["window_start"], sh["window_end"]
    rng = np.random.default_rng(seed)
    tag = spec["tag"]
    ids = [t["task_id"] for t in cfg["tasks"] if t["task_id"].startswith(tag)]
    window_len = we - ws
    burst = spec.get("tail_burst", 0)
    burst_ids = ids[-burst:] if burst else []
    normal_ids = ids[:len(ids) - burst]
    scale = (window_len / max(1, len(normal_ids))) if rate is None else 1.0 / rate
    arrivals = []
    t = ws
    for tid in normal_ids:
        t += max(1, int(rng.exponential(scale * 0.93)))
        if t >= we:
            t = we - 1
        arrivals.append({"task_id": tid, "arrive_ts": int(t)})
    burst_start = we - max(1, int(window_len * 0.03))
    span = max(1, we - 1 - burst_start)
    for j, tid in enumerate(burst_ids):
        ts = burst_start + int(rng.integers(0, span))
        arrivals.append({"task_id": tid, "arrive_ts": int(ts)})
    arrivals.sort(key=lambda a: a["arrive_ts"])
    return arrivals


def _corrupt_events(events: list[dict], rng: np.random.Generator,
                    drop_enqueue_frac: float = 0.10,
                    drop_complete_frac: float = 0.03,
                    duplicate_count: int = 6) -> list[dict]:
    """Deliberately damage the log in controlled, documented ways."""
    out = []
    stage_groups: dict[tuple, list[dict]] = {}
    for e in events:
        stage_groups.setdefault((e["task_id"], e["stage_index"]), []).append(e)

    for (tid, stage), evs in stage_groups.items():
        has_leave = any(e["event_type"] == EV_LEAVE for e in evs)
        has_complete = any(e["event_type"] == EV_COMPLETE for e in evs)
        drop_enq = rng.random() < drop_enqueue_frac
        drop_cmp = has_leave and has_complete and rng.random() < drop_complete_frac
        for e in evs:
            if drop_enq and e["event_type"] == EV_ENQUEUE:
                continue
            if drop_cmp and e["event_type"] == EV_COMPLETE:
                continue   # leave remains; completion becomes unknown (0, U]
            out.append(dict(e))
    # identical duplicates
    if out and duplicate_count:
        for _ in range(duplicate_count):
            src = out[int(rng.integers(0, len(out)))]
            out.append(dict(src))
    # stable, readable order; identical duplicates remain adjacent
    out.sort(key=lambda e: (e["ts"], e["task_id"], e["stage_index"], e["event_type"]))
    return out


def _events_from_run(run: dict) -> list[dict]:
    rows = simulation_event_rows(run)
    for r in rows:
        r["category"] = r.get("category", "default")
    return rows


def _run_shift(cfg_obj, cfg_dict, shift_id, arrivals, params, seed, end_policy="cutoff"):
    from .config import parse_config
    fn = _true_service_fn(params)
    sh = next(s for s in cfg_dict["shifts"] if s["shift_id"] == shift_id)
    sim = Simulation(cfg_obj, fn, seed, arrivals, end_policy=end_policy,
                     start_time=sh["window_start"], end_time=sh["window_end"])
    return sim.run()


def generate(out_dir: str, seed: int = 20240001) -> dict:
    import os
    cfg = build_config()
    os.makedirs(out_dir, exist_ok=True)
    dump_json(os.path.join(out_dir, "config.json"), cfg)

    from .config import parse_config
    cfg_obj = parse_config(cfg)

    rng = np.random.default_rng(seed)
    arr_a = build_arrivals(cfg, "train_a", seed + 1)
    arr_b = build_arrivals(cfg, "train_b", seed + 2)
    hold_arr = build_arrivals(cfg, "holdout", seed + 3)

    # ground-truth runs per shift.  train_a and train_b share the same true
    # parameters except S2/standard, which drifts between them; holdout has the
    # stronger S2 drift described by DRIFT_PARAMS.
    truth_params = TRUE_PARAMS
    train_b_params = dict(TRUE_PARAMS)
    train_b_params["S2"] = {**TRUE_PARAMS["S2"], **TRAIN_DRIFT_PARAMS["S2"]}
    drift_params = dict(TRUE_PARAMS)
    drift_params["S2"] = {**TRUE_PARAMS["S2"], **DRIFT_PARAMS["S2"]}

    run_a = _run_shift(cfg_obj, cfg, "train_a", arr_a, truth_params, seed + 10)
    run_b = _run_shift(cfg_obj, cfg, "train_b", arr_b, train_b_params, seed + 11)
    hold_run = _run_shift(cfg_obj, cfg, "holdout", hold_arr, drift_params, seed + 12)

    # Build events with category/priority attached from config
    task_meta = {t["task_id"]: t for t in cfg["tasks"]}
    events_a = _events_from_run(run_a)
    events_b = _events_from_run(run_b)
    hold_events = _events_from_run(hold_run)
    for e in events_a + events_b + hold_events:
        e["category"] = task_meta[e["task_id"]]["category"]

    # corrupt training logs (missing enqueue / missing service_complete / dups)
    log_a = _corrupt_events(events_a, rng)
    log_b = _corrupt_events(events_b, np.random.default_rng(seed + 21))
    # held-out log: lighter corruption, no identity conflicts
    hold_log = _corrupt_events(hold_events, np.random.default_rng(seed + 22),
                               drop_enqueue_frac=0.05, drop_complete_frac=0.01,
                               duplicate_count=2)
    all_events = log_a + log_b + hold_log
    all_events.sort(key=lambda e: (e["ts"], e["task_id"], e["stage_index"],
                                   e["event_type"]))
    dump_json(os.path.join(out_dir, "events.json"), all_events)

    # reproducibility snapshot of raw simulated truth (for verifier)
    def dump_truth_run(run, params_used, shift_id):
        rows = []
        for task in run["tasks"]:
            for st in task["stages"]:
                rows.append({
                    "task_id": task["task_id"], "stage_index": st["stage_index"],
                    "station_id": st["station_id"], "category": st["category"],
                    "true_service_ms": (st["complete_ts"] - st["start_ts"]
                                        if st["complete_ts"] is not None
                                        and st["start_ts"] is not None else None),
                    "arrive_ts": st["arrive_ts"], "enqueue_ts": st["enqueue_ts"],
                    "start_ts": st["start_ts"], "complete_ts": st["complete_ts"],
                    "leave_ts": st["leave_ts"], "exit_ts": st["exit_ts"],
                    "base_uniform": st["base_uniform"],
                    "censored": st["censored"], "state": st["state"],
                })
        return {"shift_id": shift_id, "params_used": params_used,
                "stats": run["stats"], "stages": rows}

    truth = {
        "note": "GROUND TRUTH. Only the verifier module may read this file.",
        "seed": seed,
        "true_params": _jsonable(truth_params),
        "train_b_params": _jsonable(train_b_params),
        "drift_params": _jsonable(drift_params),
        "runs": {
            "train_a": dump_truth_run(run_a, _jsonable(truth_params), "train_a"),
            "train_b": dump_truth_run(run_b, _jsonable(train_b_params), "train_b"),
            "holdout": dump_truth_run(hold_run, _jsonable(drift_params), "holdout"),
        },
    }
    dump_json(os.path.join(out_dir, "truth.json"), truth)

    # scenario definitions for capacity comparison
    scenarios = [
        # Baseline mirrors the holdout shift's configured capacity exactly.
        {"name": "baseline", "shift_id": "holdout",
         "seats": {"S1": 2, "S2": 2, "S3": 1},
         "wait_capacity": {"S1": 3, "S2": 2, "S3": 2}},
        # Add one S3 seat: directly relieves the bottleneck.
        {"name": "add_worker_S3", "shift_id": "holdout",
         "seats": {"S1": 2, "S2": 2, "S3": 2},
         "wait_capacity": {"S1": 3, "S2": 2, "S3": 2}},
        # Add one S2 seat: should shift blocking away from S2 while S3 stays tight.
        {"name": "add_worker_S2", "shift_id": "holdout",
         "seats": {"S1": 2, "S2": 3, "S3": 1},
         "wait_capacity": {"S1": 3, "S2": 2, "S3": 2}},
        # Expand buffers at S2/S3: less upstream blocking, same service capacity.
        {"name": "expand_buffer_S2_S3", "shift_id": "holdout",
         "seats": {"S1": 2, "S2": 2, "S3": 1},
         "wait_capacity": {"S1": 3, "S2": 3, "S3": 3}},
        # Reduce S3 to zero seats is invalid; instead remove one S2 seat.
        {"name": "reduced_S2", "shift_id": "holdout",
         "seats": {"S1": 2, "S2": 1, "S3": 1},
         "wait_capacity": {"S1": 3, "S2": 2, "S3": 2}},
    ]
    dump_json(os.path.join(out_dir, "scenarios.json"), scenarios)
    dump_json(os.path.join(out_dir, "windows.json"), {
        "train_windows": ["train_a", "train_b"],
        "holdout_windows": ["holdout"],
        "observation_end": max(s["window_end"] for s in cfg["shifts"]),
    })
    return {
        "config": os.path.join(out_dir, "config.json"),
        "events": os.path.join(out_dir, "events.json"),
        "truth": os.path.join(out_dir, "truth.json"),
        "scenarios": os.path.join(out_dir, "scenarios.json"),
        "windows": os.path.join(out_dir, "windows.json"),
        "n_events": len(all_events),
    }


def _jsonable(params: dict) -> dict:
    out = {}
    for st, cats in params.items():
        out[st] = {}
        for cat, spec in cats.items():
            out[st][cat] = {k: (float(v) if isinstance(v, (int, float, np.floating))
                                else v) for k, v in spec.items()}
    return out


def generate_demo_logs(out_dir: str) -> dict:
    """Tiny, capacity-consistent, hand-checkable log plus broken variants.

    Layout: S1 (seat=1, wait=2) -> S2 (seat=1, wait=1). Window [0, 100).
    Priorities: T1=0, T2=1, T3=2, T5=0. Lower number is served first.

    Hand calculation (verified by `verifier.check_demo`):
      T1: arrive 0, enqueue 0, start 2, complete 12, leave 12
          S2 enqueue 12, start 14, complete 34, leave 34, exit 34
          -> S1 svc=10 block=0 ; S2 queue_wait=2 svc=20
      T2: arrive 5, enqueue 5, start 12 (waiting 7), complete 22, leave 22
          S2 enqueue 22, start 34 (waiting 12), complete 40, leave 40, exit 40
          -> S1 svc=10 ; S2 svc=6
      T3: arrive 0, enqueue 0, start 22 (waiting 22; outranked by T1/T2),
          complete 27, leave 34 -> BLOCKED 27..34 (S2 wait full), block=7
          S2 enqueue 34, start 40, complete 44, leave 44, exit 44
          -> S1 svc=5 ; S2 queue_wait=6 svc=4
      T5: arrive 50, enqueue 50, start 55, never completes in window
          -> service RIGHT-CENSORED, lower bound 45; must not be called a
             completion and must not be filled with zero.
    """
    import os
    os.makedirs(out_dir, exist_ok=True)

    demo_cfg = {
        "stations": [
            {"station_id": "S1", "seats_capacity": 1},
            {"station_id": "S2", "seats_capacity": 1},
        ],
        "shifts": [
            {"shift_id": "shift0", "window_start": 0, "window_end": 100,
             "seats": {"S1": 1, "S2": 1},
             "wait_capacity": {"S1": 2, "S2": 1}},
        ],
        "tasks": [
            {"task_id": "T1", "priority": 0, "category": "std", "stages": ["S1", "S2"]},
            {"task_id": "T2", "priority": 1, "category": "std", "stages": ["S1", "S2"]},
            {"task_id": "T3", "priority": 2, "category": "std", "stages": ["S1", "S2"]},
            {"task_id": "T5", "priority": 0, "category": "std", "stages": ["S1", "S2"]},
        ],
    }
    events = [
        ev("T1", 0, "S1", 0, "arrive"), ev("T1", 0, "S1", 0, "enqueue"),
        ev("T1", 0, "S1", 2, "start_service"), ev("T1", 0, "S1", 12, "service_complete"),
        ev("T1", 0, "S1", 12, "leave_station"),
        ev("T1", 1, "S2", 12, "enqueue"), ev("T1", 1, "S2", 14, "start_service"),
        ev("T1", 1, "S2", 34, "service_complete"), ev("T1", 1, "S2", 34, "leave_station"),
        ev("T1", 1, "S2", 34, "exit"),

        ev("T2", 0, "S1", 5, "arrive"), ev("T2", 0, "S1", 5, "enqueue"),
        ev("T2", 0, "S1", 12, "start_service"), ev("T2", 0, "S1", 22, "service_complete"),
        ev("T2", 0, "S1", 22, "leave_station"),
        ev("T2", 1, "S2", 22, "enqueue"), ev("T2", 1, "S2", 34, "start_service"),
        ev("T2", 1, "S2", 40, "service_complete"), ev("T2", 1, "S2", 40, "leave_station"),
        ev("T2", 1, "S2", 40, "exit"),

        ev("T3", 0, "S1", 0, "arrive"), ev("T3", 0, "S1", 0, "enqueue"),
        ev("T3", 0, "S1", 22, "start_service"), ev("T3", 0, "S1", 27, "service_complete"),
        ev("T3", 0, "S1", 34, "leave_station"),       # blocked 27..34
        ev("T3", 1, "S2", 34, "enqueue"), ev("T3", 1, "S2", 40, "start_service"),
        ev("T3", 1, "S2", 44, "service_complete"), ev("T3", 1, "S2", 44, "leave_station"),
        ev("T3", 1, "S2", 44, "exit"),

        ev("T5", 0, "S1", 50, "arrive"), ev("T5", 0, "S1", 50, "enqueue"),
        ev("T5", 0, "S1", 55, "start_service"),       # censored in service
    ]
    dump_json(os.path.join(out_dir, "demo_config.json"), demo_cfg)
    dump_json(os.path.join(out_dir, "demo_events.json"), events)

    # Broken log: identity conflict, duplicate, reversed order, missing complete
    broken = [dict(e) for e in events]
    broken += [
        ev("T1", 0, "S1", 18, "service_complete"),   # conflicting completion time
        ev("T3", 0, "S1", 0, "arrive"),               # identical duplicate
        ev("T3", 1, "S2", 30, "leave_station"),       # leave before start (reversal)
    ]
    # remove T2 S2 completion -> leave present, completion unknown (0, U]
    broken = [e for e in broken if not (
        e["task_id"] == "T2" and e["stage_index"] == 1
        and e["event_type"] == "service_complete")]
    dump_json(os.path.join(out_dir, "demo_broken_events.json"), broken)
    return {"demo_config": os.path.join(out_dir, "demo_config.json"),
            "demo_events": os.path.join(out_dir, "demo_events.json"),
            "demo_broken_events": os.path.join(out_dir, "demo_broken_events.json")}


def ev(task_id, stage, station, ts, etype, category="std"):
    return {"task_id": task_id, "stage_index": stage, "station_id": station,
            "category": category, "ts": ts, "event_type": etype}


def ev(task_id, stage, station, ts, etype, category="std"):
    return {"task_id": task_id, "stage_index": stage, "station_id": station,
            "category": category, "ts": ts, "event_type": etype}
