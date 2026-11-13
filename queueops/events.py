"""Operational event log: parsing, deduplication, validation and reconstruction.

Design decisions (also surfaced in reports)
-------------------------------------------
* Same clock, integer milliseconds.
* A *lifecycle* for one task stage is ordered
  arrive (stage 0 only) -> enqueue -> start_service -> service_complete
  -> leave_station -> exit (last stage only).
  A completed task that cannot leave because the downstream waiting area is full
  sits in state ``blocked`` and keeps the upstream seat: blocking time is
  ``leave_station - service_complete``.
* Identical duplicate records are folded.  Records with the same
  (task, stage, event_type) but a different time/station/category are
  *identity conflicts* and are located, not silently merged.
* A missing ``service_complete`` is NEVER replaced by the leave timestamp.  If a
  terminal event exists we report service as an interval ``(0, U]``; if the
  stage is still open at the end of observation we report a right-censored
  lower bound.  Missing values are never written as zero.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator

from .common import (
    EV_ARRIVE, EV_COMPLETE, EV_ENQUEUE, EV_EXIT, EV_LEAVE, EV_START, EVENT_TYPES,
    STATUS_EXACT, STATUS_RIGHT, STATUS_UNKNOWN, STATUS_UPPER,
    ST_BLOCKED, ST_DONE, ST_EXT_WAIT, ST_QUEUED, ST_SERVICE, ST_UNKNOWN,
    ConfigError, LogError, as_int, as_nonneg_int, load_json, require,
)
from .config import Config

PHASE_ORDER = {
    EV_ARRIVE: 0,
    EV_ENQUEUE: 1,
    EV_START: 2,
    EV_COMPLETE: 3,
    EV_LEAVE: 4,
    EV_EXIT: 5,
}

# Status buckets used in diagnostics / timeline export.
#   exact       duration observed directly
#   right       right-censored (T > c)
#   upper       interval (0, U] (terminal event but completion time unknown)
#   unknown     cannot bound
STATUS_EXACT = "exact"
STATUS_RIGHT = "right_censored"
STATUS_UPPER = "interval_upper"
STATUS_UNKNOWN = "unknown"


@dataclass
class Event:
    task_id: str
    stage_index: int
    station_id: str
    category: str
    ts: int
    event_type: str
    seq: int = 0
    raw_index: int = 0
    inferred: bool = False

    def key(self) -> tuple:
        return (self.task_id, self.stage_index, self.event_type)

    def identity(self) -> tuple:
        return (self.task_id, self.stage_index, self.station_id,
                self.category, self.ts, self.event_type)

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id, "stage_index": self.stage_index,
            "station_id": self.station_id, "category": self.category,
            "ts": self.ts, "event_type": self.event_type, "seq": self.seq,
            "inferred": self.inferred,
        }


@dataclass
class Diagnostics:
    errors: list[dict] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)
    duplicates_folded: int = 0
    conflicts: list[dict] = field(default_factory=list)
    order_violations: list[dict] = field(default_factory=list)
    impossible: list[dict] = field(default_factory=list)
    unknown_tasks: list[str] = field(default_factory=list)
    bad_events: list[dict] = field(default_factory=list)

    def err(self, code: str, message: str, where: dict | None = None) -> None:
        self.errors.append({"code": code, "message": message, **(where or {})})

    def warn(self, code: str, message: str, where: dict | None = None) -> None:
        self.warnings.append({"code": code, "message": message, **(where or {})})

    def to_dict(self) -> dict:
        return {
            "errors": self.errors,
            "warnings": self.warnings,
            "duplicates_folded": self.duplicates_folded,
            "conflicts": self.conflicts,
            "order_violations": self.order_violations,
            "impossible": self.impossible,
            "unknown_tasks": self.unknown_tasks,
            "bad_events": self.bad_events,
        }


# --------------------------------------------------------------------------- io
def load_raw_events(path: str) -> list[dict]:
    """Accept JSON list / {"events": [...]} / JSONL."""
    if path.lower().endswith(".jsonl") or path.lower().endswith(".ndjson"):
        out: list[dict] = []
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out
    raw = load_json(path)
    if isinstance(raw, dict):
        if "events" not in raw:
            raise LogError("JSON log object must contain an 'events' array")
        raw = raw["events"]
    require(isinstance(raw, list), LogError, "event log must be a list of events")
    return raw


def _parse_event(raw: Any, idx: int) -> Event:
    require(isinstance(raw, dict), LogError, f"event[{idx}] must be an object")
    tid = raw.get("task_id")
    require(isinstance(tid, str) and tid, LogError, f"event[{idx}].task_id required")
    sid = raw.get("station_id")
    require(isinstance(sid, str) and sid, LogError, f"event[{idx}].station_id required")
    cat = raw.get("category", "default")
    require(isinstance(cat, str) and cat, LogError, f"event[{idx}].category must be a string")
    et = raw.get("event_type")
    require(et in EVENT_TYPES, LogError,
            f"event[{idx}].event_type {et!r} not in {sorted(EVENT_TYPES)}")
    stage = as_nonneg_int(raw.get("stage_index"), LogError, f"event[{idx}].stage_index")
    ts = as_nonneg_int(raw.get("ts"), LogError, f"event[{idx}].ts")
    seq = as_nonneg_int(raw.get("seq", idx), LogError, f"event[{idx}].seq")
    return Event(tid, stage, sid, cat, ts, et, seq, idx)


def parse_events(raw_events: Iterable[Any], cfg: Config,
                 diagnostics: Diagnostics) -> list[Event]:
    """Validate against config, fold duplicates, locate conflicts."""
    events: list[Event] = []
    for i, raw in enumerate(raw_events):
        try:
            ev = _parse_event(raw, i)
        except (LogError, ConfigError) as exc:
            diagnostics.bad_events.append({"index": i, "reason": str(exc)})
            diagnostics.err("bad_event", str(exc), {"index": i})
            continue
        task = cfg.tasks.get(ev.task_id)
        if task is None:
            diagnostics.err("unknown_task", f"task {ev.task_id!r} not in config",
                            {"index": i, "task_id": ev.task_id})
            if ev.task_id not in diagnostics.unknown_tasks:
                diagnostics.unknown_tasks.append(ev.task_id)
            continue
        if ev.stage_index >= len(task.stages):
            diagnostics.err("bad_stage",
                            f"task {ev.task_id} has no stage {ev.stage_index}",
                            {"index": i, "task_id": ev.task_id,
                             "stage_index": ev.stage_index})
            diagnostics.impossible.append({
                "code": "stage_out_of_range", "index": i,
                "task_id": ev.task_id, "stage_index": ev.stage_index})
            continue
        expected = task.stages[ev.stage_index]
        if ev.station_id != expected:
            diagnostics.err("route_mismatch",
                            f"task {ev.task_id} stage {ev.stage_index} belongs to "
                            f"{expected!r}, event says {ev.station_id!r}",
                            {"index": i, "task_id": ev.task_id,
                             "stage_index": ev.stage_index, "station_id": ev.station_id})
            diagnostics.impossible.append({
                "code": "route_mismatch", "index": i,
                "task_id": ev.task_id, "stage_index": ev.stage_index,
                "expected_station": expected, "station_id": ev.station_id})
            continue
        if ev.category != task.category:
            diagnostics.err("category_mismatch",
                            f"task {ev.task_id} category is {task.category!r}, "
                            f"event says {ev.category!r}",
                            {"index": i, "task_id": ev.task_id,
                             "category": ev.category})
            diagnostics.impossible.append({
                "code": "category_mismatch", "index": i,
                "task_id": ev.task_id, "category": ev.category,
                "expected_category": task.category})
            continue
        if ev.event_type == EV_ARRIVE and ev.stage_index != 0:
            diagnostics.impossible.append({
                "code": "arrive_not_stage0", "index": i,
                "task_id": ev.task_id, "stage_index": ev.stage_index})
            diagnostics.err("arrive_not_stage0",
                            f"external arrive on task {ev.task_id} stage "
                            f"{ev.stage_index}; only stage 0 may arrive",
                            {"index": i})
            continue
        if ev.event_type == EV_EXIT and ev.stage_index != len(task.stages) - 1:
            diagnostics.impossible.append({
                "code": "exit_not_final", "index": i,
                "task_id": ev.task_id, "stage_index": ev.stage_index})
            diagnostics.err("exit_not_final",
                            f"final exit on task {ev.task_id} stage "
                            f"{ev.stage_index} but route has {len(task.stages)} stages",
                            {"index": i})
            continue
        events.append(ev)

    # fold duplicates / locate conflicts by (task, stage, event_type)
    by_key: dict[tuple, list[Event]] = {}
    for ev in events:
        by_key.setdefault(ev.key(), []).append(ev)
    folded: list[Event] = []
    for key, group in by_key.items():
        identities = {ev.identity() for ev in group}
        if len(identities) == 1:
            kept = sorted(group, key=lambda e: (e.seq, e.raw_index))[0]
            folded.append(kept)
            diagnostics.duplicates_folded += len(group) - 1
        else:
            # same logical event reported with conflicting telemetry
            base = sorted(group, key=lambda e: (e.ts, e.seq, e.raw_index))[0]
            folded.append(base)
            rec = {
                "task_id": key[0], "stage_index": key[1], "event_type": key[2],
                "variants": [e.to_dict() for e in group],
            }
            diagnostics.conflicts.append(rec)
            diagnostics.err("identity_conflict",
                            f"conflicting {key[2]!r} events for task {key[0]} "
                            f"stage {key[1]}", {"task_id": key[0], "stage_index": key[1]})
    return folded


# ---------------------------------------------------------------- reconstruction
@dataclass
class StageObservation:
    task_id: str
    stage_index: int
    station_id: str
    category: str
    priority: int
    arrive_ts: int | None = None
    enqueue_ts: int | None = None
    enqueue_source: str = "unknown"
    start_ts: int | None = None
    complete_ts: int | None = None
    leave_ts: int | None = None
    exit_ts: int | None = None
    has_next: bool = False
    external_wait_ms: int | None = None
    queue_wait_ms: int | None = None
    service_ms: int | None = None
    blocking_ms: int | None = None
    total_wait_ms: int | None = None
    service_status: str = STATUS_UNKNOWN
    service_lower: int | None = None   # right-censored lower bound
    service_upper: int | None = None   # interval (0, U]
    queue_status: str = STATUS_UNKNOWN
    queue_lower: int | None = None
    queue_upper: int | None = None
    blocking_status: str = STATUS_UNKNOWN
    blocking_lower: int | None = None
    blocking_upper: int | None = None
    shift_id: str | None = None        # shift whose window contains start (or entry)
    flags: list[str] = field(default_factory=list)
    valid: bool = True
    events: list[Event] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id, "stage_index": self.stage_index,
            "station_id": self.station_id, "category": self.category,
            "priority": self.priority,
            "arrive_ts": self.arrive_ts, "enqueue_ts": self.enqueue_ts,
            "enqueue_source": self.enqueue_source,
            "start_ts": self.start_ts, "complete_ts": self.complete_ts,
            "leave_ts": self.leave_ts, "exit_ts": self.exit_ts,
            "external_wait_ms": self.external_wait_ms,
            "queue_wait_ms": self.queue_wait_ms,
            "service_ms": self.service_ms,
            "blocking_ms": self.blocking_ms,
            "total_wait_ms": self.total_wait_ms,
            "service_status": self.service_status,
            "service_lower": self.service_lower, "service_upper": self.service_upper,
            "queue_status": self.queue_status,
            "queue_lower": self.queue_lower, "queue_upper": self.queue_upper,
            "blocking_status": self.blocking_status,
            "blocking_lower": self.blocking_lower, "blocking_upper": self.blocking_upper,
            "shift_id": self.shift_id, "flags": list(self.flags),
            "valid": self.valid,
        }


@dataclass
class TaskObservation:
    task_id: str
    priority: int
    category: str
    stages: list[StageObservation] = field(default_factory=list)
    status_at_boundaries: dict[str, str] = field(default_factory=dict)
    final_status: str = ST_UNKNOWN
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id, "priority": self.priority,
            "category": self.category,
            "stages": [s.to_dict() for s in self.stages],
            "status_at_boundaries": dict(self.status_at_boundaries),
            "final_status": self.final_status,
            "notes": list(self.notes),
        }


@dataclass
class Reconstruction:
    tasks: dict[str, TaskObservation]
    diagnostics: Diagnostics
    observation_end: int
    windows: list[tuple[str, int, int]]

    def stage_observations(self) -> Iterator[StageObservation]:
        for task in self.tasks.values():
            for st in task.stages:
                yield st

    def to_dict(self) -> dict:
        return {
            "observation_end": self.observation_end,
            "windows": [{"shift_id": s, "window_start": a, "window_end": b}
                        for (s, a, b) in self.windows],
            "tasks": [t.to_dict() for t in self.tasks.values()],
            "diagnostics": self.diagnostics.to_dict(),
        }


def _shift_for(ts: int | None, cfg: Config) -> str | None:
    if ts is None:
        return None
    for sh in cfg.shifts.values():
        if sh.window_start <= ts < sh.window_end:
            return sh.shift_id
    return None


def _window_end_for(ts: int | None, cfg: Config, observation_end: int) -> int:
    return observation_end


def reconstruct(cfg: Config, events: list[Event],
                diagnostics: Diagnostics | None = None,
                observation_end: int | None = None) -> Reconstruction:
    """Turn validated events into per-task/stage observations."""
    diagnostics = diagnostics or Diagnostics()
    if observation_end is None:
        observation_end = max(sh.window_end for sh in cfg.shifts.values())
    windows = [(sh.shift_id, sh.window_start, sh.window_end)
               for sh in sorted(cfg.shifts.values(), key=lambda s: s.window_start)]

    events_by_task: dict[str, list[Event]] = {}
    for ev in events:
        events_by_task.setdefault(ev.task_id, []).append(ev)

    tasks: dict[str, TaskObservation] = {}
    for task_id, task in cfg.tasks.items():
        evs = events_by_task.get(task_id, [])
        tob = TaskObservation(task_id, task.priority, task.category)
        prev_leave: int | None = None
        for stage_idx, station in enumerate(task.stages):
            sev = [e for e in evs if e.stage_index == stage_idx]
            sev.sort(key=lambda e: (e.ts, PHASE_ORDER[e.event_type], e.seq, e.raw_index))
            obs = _reconstruct_stage(cfg, task_id, task.category, task.priority,
                                     stage_idx, station, sev,
                                     has_next=stage_idx < len(task.stages) - 1,
                                     observation_end=observation_end,
                                     prev_leave=prev_leave,
                                     diagnostics=diagnostics)
            # queue-wait inference for stages entered from an upstream leave
            if (obs.enqueue_source == "unknown" and obs.start_ts is not None
                    and prev_leave is not None and obs.enqueue_ts is None):
                obs.enqueue_ts = prev_leave
                obs.enqueue_source = "inferred_upstream_leave"
                if obs.queue_wait_ms is None and obs.start_ts >= prev_leave:
                    obs.queue_wait_ms = obs.start_ts - prev_leave
                    obs.queue_status = STATUS_EXACT
                    obs.flags.append("enqueue_inferred")
            tob.stages.append(obs)
            prev_leave = obs.leave_ts if obs.leave_ts is not None else (
                obs.exit_ts if obs.exit_ts is not None else None)
        tasks[task_id] = tob
        # Post-pass: when a stage completed service but has no observed leave,
        # the next stage's enqueue (observed or inferred) bounds the blocking
        # interval: blocking in (0, next_enqueue - complete].
        for i, obs in enumerate(tob.stages[:-1]):
            if obs.complete_ts is None or obs.leave_ts is not None:
                continue
            nxt = tob.stages[i + 1]
            if nxt.enqueue_ts is not None and nxt.enqueue_ts >= obs.complete_ts:
                obs.blocking_upper = nxt.enqueue_ts - obs.complete_ts
                obs.blocking_status = STATUS_UPPER
                obs.flags.append("blocking_bounded_by_next_enqueue")

    # boundary states and final status
    for task_id, tob in tasks.items():
        for sh_id, ws, we in windows:
            tob.status_at_boundaries[sh_id] = _state_at(tob, we, observation_end)
        tob.final_status = _state_at(tob, observation_end, observation_end)

    rec = Reconstruction(tasks, diagnostics, observation_end, windows)
    # cross checks that depend on all stages
    _check_task_sequences(cfg, rec)
    return rec


def _reconstruct_stage(cfg: Config, task_id: str, category: str, priority: int,
                       stage_idx: int, station: str, evs: list[Event],
                       has_next: bool, observation_end: int,
                       prev_leave: int | None,
                       diagnostics: Diagnostics) -> StageObservation:
    obs = StageObservation(task_id, stage_idx, station, category, priority, has_next=has_next)

    def pick(et: str) -> Event | None:
        found = [e for e in evs if e.event_type == et]
        return found[0] if found else None

    e_arr = pick(EV_ARRIVE)
    e_enq = pick(EV_ENQUEUE)
    e_start = pick(EV_START)
    e_comp = pick(EV_COMPLETE)
    e_leave = pick(EV_LEAVE)
    e_exit = pick(EV_EXIT)
    obs.events = evs

    obs.arrive_ts = e_arr.ts if e_arr else None
    obs.enqueue_ts = e_enq.ts if e_enq else None
    obs.start_ts = e_start.ts if e_start else None
    obs.complete_ts = e_comp.ts if e_comp else None
    obs.leave_ts = e_leave.ts if e_leave else None
    obs.exit_ts = e_exit.ts if e_exit else None
    if e_enq:
        obs.enqueue_source = "observed"
    elif stage_idx > 0 and prev_leave is not None:
        obs.enqueue_source = "inferred_upstream_leave"
    elif stage_idx == 0 and e_arr:
        obs.enqueue_source = "unknown_split"

    # --- order / impossible occupancy checks -------------------------------
    order = [("arrive", obs.arrive_ts), ("enqueue", obs.enqueue_ts),
             ("start", obs.start_ts), ("complete", obs.complete_ts),
             ("leave", obs.leave_ts), ("exit", obs.exit_ts)]
    last_name, last_ts = None, -1
    for name, ts in order:
        if ts is None:
            continue
        if ts < last_ts:
            rec = {"task_id": task_id, "stage_index": stage_idx,
                   "violation": f"{name} at {ts} before {last_name} at {last_ts}"}
            diagnostics.order_violations.append(rec)
            diagnostics.err("order_violation", rec["violation"], rec)
            obs.flags.append("out_of_order")
            obs.valid = False
        last_name, last_ts = name, ts
    if stage_idx == 0 and e_arr is None and (e_enq or e_start or e_comp):
        obs.flags.append("missing_arrive")
        diagnostics.warn("missing_arrive",
                         f"task {task_id} stage 0 lacks external arrive; entry time inferred",
                         {"task_id": task_id, "stage_index": stage_idx})
    if e_start and e_comp and e_comp.ts < e_start.ts:
        diagnostics.impossible.append({
            "code": "complete_before_start", "task_id": task_id,
            "stage_index": stage_idx, "start_ts": e_start.ts, "complete_ts": e_comp.ts})
        obs.flags.append("complete_before_start")
        obs.valid = False
    if e_leave and e_start and e_leave.ts < e_start.ts:
        diagnostics.impossible.append({
            "code": "leave_before_start", "task_id": task_id,
            "stage_index": stage_idx, "start_ts": e_start.ts, "leave_ts": e_leave.ts})
        obs.flags.append("leave_before_start")
        obs.valid = False
    if e_comp is None and e_leave is not None:
        obs.flags.append("missing_service_complete")
        diagnostics.warn("missing_service_complete",
                         f"task {task_id} stage {stage_idx}: leave_station without "
                         f"service_complete; completion time is not fabricated from leave",
                         {"task_id": task_id, "stage_index": stage_idx})
    if e_start is None and e_comp is not None:
        obs.flags.append("missing_start")
    if e_enq is None and stage_idx > 0 and prev_leave is None:
        obs.flags.append("missing_enqueue")

    obs.shift_id = _shift_for(obs.start_ts if obs.start_ts is not None else
                              (obs.enqueue_ts if obs.enqueue_ts is not None else
                               obs.arrive_ts), cfg)

    # --- service duration --------------------------------------------------
    if obs.start_ts is not None and obs.complete_ts is not None:
        if obs.complete_ts >= obs.start_ts:
            obs.service_ms = obs.complete_ts - obs.start_ts
            obs.service_status = STATUS_EXACT
        else:
            obs.service_status = STATUS_UNKNOWN
    elif obs.start_ts is not None and obs.complete_ts is None:
        terminal = obs.leave_ts if obs.leave_ts is not None else obs.exit_ts
        if terminal is not None and terminal >= obs.start_ts:
            obs.service_upper = terminal - obs.start_ts
            obs.service_status = STATUS_UPPER
        else:
            # open at end of observation -> right censored
            if observation_end >= obs.start_ts:
                obs.service_lower = observation_end - obs.start_ts
                obs.service_status = STATUS_RIGHT
            else:
                obs.service_status = STATUS_UNKNOWN
    elif obs.start_ts is None and obs.complete_ts is not None:
        entry = obs.enqueue_ts if obs.enqueue_ts is not None else (
            prev_leave if prev_leave is not None else obs.arrive_ts)
        if entry is not None and obs.complete_ts >= entry:
            obs.service_upper = obs.complete_ts - entry
            obs.service_status = STATUS_UPPER
        else:
            obs.service_status = STATUS_UNKNOWN

    # --- queue / external waiting -----------------------------------------
    if obs.start_ts is not None:
        if obs.enqueue_ts is not None:
            if obs.start_ts >= obs.enqueue_ts:
                obs.queue_wait_ms = obs.start_ts - obs.enqueue_ts
                obs.queue_status = STATUS_EXACT
        elif stage_idx == 0 and obs.arrive_ts is not None:
            if obs.start_ts >= obs.arrive_ts:
                obs.total_wait_ms = obs.start_ts - obs.arrive_ts
                obs.queue_status = STATUS_UNKNOWN
                obs.flags.append("queue_external_split_unknown")
    elif obs.enqueue_ts is not None:
        if observation_end >= obs.enqueue_ts:
            obs.queue_lower = observation_end - obs.enqueue_ts
            obs.queue_status = STATUS_RIGHT
        else:
            obs.queue_status = STATUS_UNKNOWN

    if stage_idx == 0 and obs.arrive_ts is not None and obs.enqueue_ts is not None:
        if obs.enqueue_ts >= obs.arrive_ts:
            obs.external_wait_ms = obs.enqueue_ts - obs.arrive_ts
    if stage_idx == 0 and obs.arrive_ts is not None and obs.start_ts is not None:
        if obs.total_wait_ms is None:
            obs.total_wait_ms = obs.start_ts - obs.arrive_ts

    # --- blocking (only stages with a downstream station) ------------------
    if has_next:
        if obs.complete_ts is not None and obs.leave_ts is not None:
            if obs.leave_ts >= obs.complete_ts:
                obs.blocking_ms = obs.leave_ts - obs.complete_ts
                obs.blocking_status = STATUS_EXACT
            else:
                obs.blocking_status = STATUS_UNKNOWN
        elif obs.complete_ts is not None and obs.leave_ts is None:
            # blocked with no observed leave: bound later by next-stage enqueue
            # (post-pass) or right-censored at the observation end.
            if observation_end >= obs.complete_ts:
                obs.blocking_lower = observation_end - obs.complete_ts
                obs.blocking_status = STATUS_RIGHT
            else:
                obs.blocking_status = STATUS_UNKNOWN
        elif obs.complete_ts is None:
            # service completion itself is unknown -> blocking not identifiable
            obs.blocking_status = STATUS_UNKNOWN
            if obs.leave_ts is not None:
                obs.flags.append("blocking_unidentifiable_missing_complete")
    else:
        # final station: no downstream waiting area; no blocking by definition
        obs.blocking_status = "not_applicable"
        if obs.complete_ts is not None and obs.exit_ts is None and obs.leave_ts is None:
            if observation_end >= obs.complete_ts:
                obs.flags.append("missing_final_exit")
    return obs


def _check_task_sequences(cfg: Config, rec: Reconstruction) -> None:
    """Stage order must match route and cannot go backwards; rework repeats allowed."""
    for tid, task in rec.tasks.items():
        route = cfg.tasks[tid].stages
        seen: set[int] = set()
        last = -1
        for st in task.stages:
            if not st.events:
                continue
            if st.stage_index < last:
                rec.diagnostics.order_violations.append({
                    "task_id": tid, "stage_index": st.stage_index,
                    "violation": "stage index decreased"})
                rec.diagnostics.err("stage_order", "stage index decreased",
                                    {"task_id": tid})
            last = st.stage_index
            seen.add(st.stage_index)


def _state_at(task: TaskObservation, t: int, observation_end: int) -> str:
    """Best-effort state of a task at time ``t`` from reconstructed stages."""
    last_known = None
    for st in task.stages:
        # timestamps that define state transitions
        if st.exit_ts is not None and st.exit_ts <= t:
            last_known = ST_DONE
            continue
        if st.start_ts is not None and st.start_ts <= t:
            if st.complete_ts is not None and st.complete_ts <= t:
                if st.has_next:
                    if st.leave_ts is not None and st.leave_ts <= t:
                        last_known = ST_QUEUED      # moved downstream (approx)
                    else:
                        last_known = ST_BLOCKED
                else:
                    last_known = ST_DONE if st.exit_ts is not None and st.exit_ts <= t else ST_SERVICE
            else:
                last_known = ST_SERVICE
            continue
        if st.enqueue_ts is not None and st.enqueue_ts <= t:
            last_known = ST_QUEUED
            continue
        if st.arrive_ts is not None and st.arrive_ts <= t:
            last_known = ST_EXT_WAIT
            continue
        # before this stage's first event
        if st.arrive_ts is not None and t < st.arrive_ts:
            return ST_UNKNOWN if st.stage_index > 0 else "not_yet_arrived"
    if last_known is not None:
        return last_known
    if all(st.arrive_ts is None and not st.events for st in task.stages):
        return "not_yet_arrived"
    return ST_UNKNOWN


# --------------------------------------------------------------- export helpers
def write_timeline_json(rec: Reconstruction, path: str) -> None:
    from .common import dump_json
    dump_json(path, rec.to_dict())


def write_per_task_events(rec: Reconstruction, path_csv: str) -> None:
    """Independently recomputable event table: one row per observed event."""
    import csv
    rows = []
    for task in rec.tasks.values():
        for st in task.stages:
            for ev in st.events:
                rows.append(ev.to_dict())
    write_event_rows_csv(rows, path_csv)


def write_event_rows_csv(rows: list[dict], path_csv: str) -> None:
    import csv
    import os
    os.makedirs(os.path.dirname(os.path.abspath(path_csv)), exist_ok=True)
    fields = ["task_id", "stage_index", "station_id", "category",
              "ts", "event_type", "seq", "inferred"]
    with open(path_csv, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
