"""Discrete-event simulation with finite buffers, priorities and blocking.

Semantics (explicit, also exported in run reports)
--------------------------------------------------
* Clock: integer milliseconds, one clock.
* Same-instant event order: service_complete first, then capacity change,
  then external arrival.  After each event a deterministic propagation
  fixed-point runs per station in station_order:
      (1) start queued tasks while a seat is free,
      (2) admit blocked downstream-oriented tasks while a wait slot is free
          (ordered by blocking_start, task_id),
      (3) admit external arrivals into the entry station's wait area
          (ordered by priority, arrival_ts, task_id).
  Repeated until no station changes state.
* Service seat is held from start_service until leave_station; a task whose
  service completed but whose downstream wait area is full stays ``blocked``
  and keeps the upstream seat.  Final-stage tasks release the seat at exit.
* Wait capacity counts only QUEUED tasks (not in-service, not blocked).
* First-station arrivals that cannot be admitted stay in an independent
  unbounded external waiting area; they are never dropped.
* When seats shrink at a shift boundary, running services are NOT preempted.
  Excess seats are released only as those tasks leave.
* End policy:
    cutoff  : stop at window_end; unfinished work is censored (not dropped
              and never counted as finished).
    drain   : after window_end admit no new arrivals, freeze the last shift's
              capacity and keep servicing existing work until idle or until
              no event can progress; then report any blocking chain.
* Service samples are bound to (seed, task_id, stage_index, station):
  one base uniform u is drawn from a stable hash and transformed by the
  service model's inverse CDF.  Changing capacity never changes u.
"""
from __future__ import annotations

import hashlib
import heapq
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np

from .common import (
    EV_ARRIVE, EV_COMPLETE, EV_ENQUEUE, EV_EXIT, EV_LEAVE, EV_START,
    ST_BLOCKED, ST_DONE, ST_EXT_WAIT, ST_QUEUED, ST_SERVICE,
)
from .config import Config

# state names for simulation
S_EXT = "external_waiting"
S_QUEUED = "queued"
S_SERVICE = "in_service"
S_BLOCKED = "blocked"
S_DONE = "finished"

EP_COMPLETE = "complete"
EP_CAPACITY = "capacity"
EP_ARRIVE = "arrive"
_RANK = {EP_COMPLETE: 0, EP_CAPACITY: 1, EP_ARRIVE: 2}


def stable_uniform(seed: int, task_id: str, stage_index: int, station: str,
                    category: str, stream: str = "service") -> float:
    """Deterministic base uniform in (0,1) bound to identity only."""
    payload = f"{stream}|{task_id}|{stage_index}|{station}|{category}".encode("utf-8")
    digest = hashlib.blake2b(payload, digest_size=8).digest()
    h = int.from_bytes(digest, "little")
    rng = np.random.default_rng([int(seed), h])
    return float(rng.random())


ServiceFn = Callable[[str, int, str, str, float], int]
"""service_fn(task_id, stage_index, station, category, u) -> integer ms >= 1"""


@dataclass
class StageRun:
    task_id: str
    stage_index: int
    station_id: str
    category: str
    priority: int
    has_next: bool
    arrive_ts: int | None = None
    enqueue_ts: int | None = None
    start_ts: int | None = None
    complete_ts: int | None = None
    leave_ts: int | None = None
    exit_ts: int | None = None
    service_s: int | None = None
    base_uniform: float | None = None
    state: str = "not_started"
    censored: bool = False
    flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id, "stage_index": self.stage_index,
            "station_id": self.station_id, "category": self.category,
            "priority": self.priority,
            "arrive_ts": self.arrive_ts, "enqueue_ts": self.enqueue_ts,
            "start_ts": self.start_ts, "complete_ts": self.complete_ts,
            "leave_ts": self.leave_ts, "exit_ts": self.exit_ts,
            "service_s": self.service_s, "base_uniform": self.base_uniform,
            "state": self.state, "censored": self.censored,
            "flags": list(self.flags),
            "queue_wait_ms": (self.start_ts - self.enqueue_ts
                              if self.start_ts is not None and self.enqueue_ts is not None
                              else None),
            "blocking_ms": (self.leave_ts - self.complete_ts
                            if self.leave_ts is not None and self.complete_ts is not None
                            else None),
            "service_ms": (self.complete_ts - self.start_ts
                           if self.complete_ts is not None and self.start_ts is not None
                           else None),
            "external_wait_ms": (self.enqueue_ts - self.arrive_ts
                                 if self.arrive_ts is not None and self.enqueue_ts is not None
                                 else None),
        }


@dataclass
class TaskRun:
    task_id: str
    priority: int
    category: str
    stages: list[StageRun]
    state: str = "not_started"
    current_stage: int = 0
    done_ts: int | None = None
    censored: bool = False

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id, "priority": self.priority,
            "category": self.category, "state": self.state,
            "current_stage": self.current_stage, "done_ts": self.done_ts,
            "stages": [s.to_dict() for s in self.stages],
        }


class Simulation:
    def __init__(self, cfg: Config, service_fn: ServiceFn, seed: int,
                 arrivals: list[dict], *,
                 end_policy: str = "cutoff",
                 shift_override: dict[str, dict] | None = None,
                 start_time: int | None = None,
                 end_time: int | None = None,
                 max_events: int = 2_000_000):
        if end_policy not in ("cutoff", "drain"):
            raise ValueError("end_policy must be 'cutoff' or 'drain'")
        self.cfg = cfg
        self.service_fn = service_fn
        self.seed = int(seed)
        self.end_policy = end_policy
        self.max_events = max_events
        self.shift_override = shift_override or {}
        self.start = start_time if start_time is not None else min(
            sh.window_start for sh in cfg.shifts.values())
        self.end = end_time if end_time is not None else max(
            sh.window_end for sh in cfg.shifts.values())

        self.tasks: dict[str, TaskRun] = {}
        self.entry = cfg.entry_station()
        self.stations = list(cfg.station_order)

        # runtime
        self.wait_heap: dict[str, list] = {s: [] for s in self.stations}
        self.ext_heap: dict[str, list] = {s: [] for s in self.stations}
        self.blocked_heap: dict[str, list] = {s: [] for s in self.stations}  # target -> blocked
        self.seats_in_use: dict[str, int] = {s: 0 for s in self.stations}
        self.event_heap: list = []
        self._serial = 0
        self._arrivals_by_time: list[tuple[int, int, str, int]] = []
        self.capacity_changes: list[dict] = []
        self.progress_events = 0
        self._build_tasks(arrivals)

    # -------------------------------------------------------------- helpers
    def _new_task(self, task_id: str) -> TaskRun:
        task = self.cfg.tasks[task_id]
        stages = [
            StageRun(task_id, i, st, task.category, task.priority,
                     has_next=i < len(task.stages) - 1)
            for i, st in enumerate(task.stages)
        ]
        return TaskRun(task_id, task.priority, task.category, stages)

    def _build_tasks(self, arrivals: list[dict]) -> None:
        seen: set[str] = set()
        arr: list[tuple[int, int, str, int]] = []
        for i, a in enumerate(arrivals):
            tid = a["task_id"]
            if tid not in self.cfg.tasks:
                raise ValueError(f"arrival for unknown task {tid!r}")
            if tid in seen:
                raise ValueError(f"duplicate arrival for task {tid!r}")
            seen.add(tid)
            ts = int(a["arrive_ts"])
            if self.end_policy == "cutoff" and ts > self.end:
                continue
            arr.append((ts, self.cfg.tasks[tid].priority, tid, i))
            self.tasks[tid] = self._new_task(tid)
        # deterministic arrival ordering
        arr.sort(key=lambda x: (x[0], x[1], x[2]))
        for ts, prio, tid, i in arr:
            self._push(ts, EP_ARRIVE, {"task_id": tid})
        # capacity-change events at each shift boundary inside horizon
        for sh in sorted(self.cfg.shifts.values(), key=lambda s: s.window_start):
            if sh.window_start > self.start:
                if self.end_policy == "cutoff" and sh.window_start > self.end:
                    continue
                self._push(sh.window_start, EP_CAPACITY, {"shift": sh.shift_id})

    def _push(self, t: int, kind: str, payload: dict) -> None:
        self._serial += 1
        heapq.heappush(self.event_heap, (int(t), _RANK[kind], self._serial, kind, payload))

    def _shift_at(self, t: int, station: str) -> tuple[int, int]:
        """Return (seats, wait_capacity) at time t for station."""
        shifts = sorted(self.cfg.shifts.values(), key=lambda s: s.window_start)
        chosen = shifts[0]
        for sh in shifts:
            if sh.window_start <= t:
                chosen = sh
            else:
                break
        ov = self.shift_override.get(chosen.shift_id, {})
        seats = ov.get("seats", {}).get(station, self.cfg.seats_for(chosen.shift_id, station))
        wait = ov.get("wait_capacity", {}).get(
            station, self.cfg.wait_for(chosen.shift_id, station))
        return int(seats), int(wait)

    # ---------------------------------------------------------- propagation
    def _propagate(self, t: int) -> None:
        for _ in range(10000):
            changed = False
            for s in self.stations:
                seats, waitcap = self._shift_at(t, s)
                # 1) start queued while seat free
                while self.seats_in_use[s] < seats and self.wait_heap[s]:
                    _, _, tid, stage = heapq.heappop(self.wait_heap[s])
                    sr = self.tasks[tid].stages[stage]
                    if sr.state != S_QUEUED:
                        continue  # stale guard (should not occur)
                    u = self._service_u(tid, stage, s, sr.category)
                    srv = self.service_fn(tid, stage, s, sr.category, u)
                    srv = max(1, int(round(srv)))
                    sr.enqueue_ts = sr.enqueue_ts if sr.enqueue_ts is not None else t
                    sr.start_ts = t
                    sr.service_s = srv
                    sr.base_uniform = u
                    sr.state = S_SERVICE
                    self.tasks[tid].state = S_SERVICE
                    self.tasks[tid].current_stage = stage
                    self.seats_in_use[s] += 1
                    self._push(t + srv, EP_COMPLETE, {"task_id": tid, "stage": stage})
                    changed = True
                # 2) promote blocked tasks targeting s while wait space
                while (len(self.wait_heap[s]) < waitcap
                       and self.blocked_heap[s]):
                    bstart, bt, btid, bstage = heapq.heappop(self.blocked_heap[s])
                    sr = self.tasks[btid].stages[bstage]
                    if sr.state != S_BLOCKED:
                        continue
                    up = sr.station_id
                    sr.leave_ts = t
                    sr.state = S_DONE
                    self.seats_in_use[up] -= 1
                    nxt = self.tasks[btid].stages[bstage + 1]
                    nxt.enqueue_ts = t
                    nxt.state = S_QUEUED
                    self.tasks[btid].current_stage = bstage + 1
                    self.tasks[btid].state = S_QUEUED
                    heapq.heappush(self.wait_heap[s],
                                   (self.tasks[btid].priority, t, btid, bstage + 1))
                    # NOTE: service draw happens on start, not enqueue; u placeholder
                    changed = True
                # 3) admit external arrivals into entry station
                if s == self.entry:
                    while (len(self.wait_heap[s]) < waitcap
                           and self.ext_heap[s]):
                        prio, ats, etid = heapq.heappop(self.ext_heap[s])
                        sr = self.tasks[etid].stages[0]
                        if sr.state != S_EXT:
                            continue
                        sr.enqueue_ts = t
                        sr.state = S_QUEUED
                        self.tasks[etid].state = S_QUEUED
                        heapq.heappush(self.wait_heap[s], (prio, t, etid, 0))
                        changed = True
            if not changed:
                return

    def _service_u(self, task_id: str, stage: int, station: str, category: str) -> float:
        return stable_uniform(self.seed, task_id, stage, station, category)

    # --------------------------------------------------------------- handling
    def _on_arrive(self, t: int, payload: dict) -> None:
        tid = payload["task_id"]
        task = self.tasks[tid]
        sr = task.stages[0]
        sr.arrive_ts = t
        sr.state = S_EXT
        task.state = S_EXT
        # external waiting orders by priority, arrival, task_id
        heapq.heappush(self.ext_heap[self.entry],
                       (task.priority, t, tid))
        self._propagate(t)

    def _on_complete(self, t: int, payload: dict) -> None:
        tid = payload["task_id"]
        stage = payload["stage"]
        task = self.tasks[tid]
        sr = task.stages[stage]
        if sr.state != S_SERVICE:
            return
        sr.complete_ts = t
        if sr.has_next:
            nxt_station = task.stages[stage + 1].station_id
            _, waitcap = self._shift_at(t, nxt_station)
            if len(self.wait_heap[nxt_station]) < waitcap:
                # immediate transfer: leave upstream, enter downstream wait
                sr.leave_ts = t
                self.seats_in_use[sr.station_id] -= 1
                sr.state = S_DONE
                nxt = task.stages[stage + 1]
                nxt.enqueue_ts = t
                nxt.state = S_QUEUED
                task.current_stage = stage + 1
                task.state = S_QUEUED
                heapq.heappush(self.wait_heap[nxt_station],
                               (task.priority, t, tid, stage + 1))
            else:
                # blocked: keep upstream seat
                sr.state = S_BLOCKED
                task.state = S_BLOCKED
                heapq.heappush(self.blocked_heap[nxt_station],
                               (t, tid, tid, stage))
        else:
            sr.exit_ts = t
            sr.state = S_DONE
            task.state = S_DONE
            task.done_ts = t
            self.seats_in_use[sr.station_id] -= 1
        self.progress_events += 1
        self._propagate(t)

    def _on_capacity(self, t: int, payload: dict) -> None:
        self.capacity_changes.append({
            "ts": t, "shift_id": payload["shift"],
            "seats": {s: self._shift_at(t, s)[0] for s in self.stations},
            "wait_capacity": {s: self._shift_at(t, s)[1] for s in self.stations},
        })
        self._propagate(t)

    # ------------------------------------------------------------------- run
    def run(self) -> dict:
        cutoff_end = self.end
        while self.event_heap:
            t, rank, serial, kind, payload = self.event_heap[0]
            if self.end_policy == "cutoff" and t > cutoff_end:
                break
            heapq.heappop(self.event_heap)
            if kind == EP_COMPLETE:
                self._on_complete(t, payload)
            elif kind == EP_CAPACITY:
                self._on_capacity(t, payload)
            elif kind == EP_ARRIVE:
                self._on_arrive(t, payload)
            else:
                raise AssertionError(kind)
            if self.progress_events > self.max_events:
                break

        # if drain and idle but tasks remain -> attempt no-op detect blocking chain
        unfinished = [tid for tid, task in self.tasks.items() if task.state != S_DONE]
        # mark censoring at the report horizon
        censor_at = self.end if self.end_policy == "cutoff" else self._last_time()
        for tid in unfinished:
            task = self.tasks[tid]
            for i, sr in enumerate(task.stages):
                if sr.state == S_DONE:
                    continue
                if sr.state == S_SERVICE and sr.start_ts is not None:
                    sr.censored = True
                    sr.flags.append("service_censored")
                elif sr.state == S_BLOCKED:
                    sr.censored = True
                    sr.flags.append("blocking_censored")
                elif sr.state == S_QUEUED:
                    sr.censored = True
                    sr.flags.append("queue_censored")
                elif sr.state == S_EXT:
                    sr.censored = True
                    sr.flags.append("external_wait_censored")
                break  # later stages not reached yet
            task.censored = True

        deadlock = None
        if unfinished and not self.event_heap:
            deadlock = self._blocking_chain()

        return {
            "meta": {
                "seed": self.seed,
                "end_policy": self.end_policy,
                "start": self.start,
                "end": self.end,
                "entry_station": self.entry,
            },
            "tasks": [t.to_dict() for t in self.tasks.values()],
            "stats": self._stats(censor_at, unfinished),
            "stations": self._occupancy(censor_at),
            "capacity_changes": self.capacity_changes,
            "deadlock": deadlock,
        }

    def _last_time(self) -> int:
        times = [self.start]
        for task in self.tasks.values():
            for sr in task.stages:
                for ts in (sr.start_ts, sr.complete_ts, sr.leave_ts, sr.exit_ts):
                    if ts is not None:
                        times.append(ts)
        return max(times)

    # ---------------------------------------------------------------- stats
    def _stats(self, horizon: int, unfinished: list[str]) -> dict:
        total = len(self.tasks)
        done = total - len(unfinished)
        per_station: dict[str, dict] = {}
        for s in self.stations:
            per_station[s] = {
                "queue_wait_ms": [], "blocking_ms": [], "service_ms": [],
                "external_wait_ms": [], "queue_censored_lb": [],
                "blocking_censored_lb": [], "service_censored_lb": [],
                "n_stages": 0,
            }
        overall_wait = []
        for task in self.tasks.values():
            for sr in task.stages:
                st = per_station[sr.station_id]
                st["n_stages"] += 1
                if sr.start_ts is not None and sr.enqueue_ts is not None:
                    st["queue_wait_ms"].append(sr.start_ts - sr.enqueue_ts)
                    overall_wait.append(sr.start_ts - sr.enqueue_ts)
                elif sr.state == S_QUEUED and sr.enqueue_ts is not None:
                    st["queue_censored_lb"].append(max(0, horizon - sr.enqueue_ts))
                if sr.complete_ts is not None and sr.start_ts is not None:
                    st["service_ms"].append(sr.complete_ts - sr.start_ts)
                elif sr.state == S_SERVICE and sr.start_ts is not None:
                    st["service_censored_lb"].append(max(0, horizon - sr.start_ts))
                if sr.has_next:
                    if sr.leave_ts is not None and sr.complete_ts is not None:
                        st["blocking_ms"].append(sr.leave_ts - sr.complete_ts)
                    elif sr.state == S_BLOCKED and sr.complete_ts is not None:
                        st["blocking_censored_lb"].append(max(0, horizon - sr.complete_ts))
                if sr.stage_index == 0 and sr.arrive_ts is not None and sr.enqueue_ts is not None:
                    st["external_wait_ms"].append(sr.enqueue_ts - sr.arrive_ts)

        def q(vals: list[int]) -> dict:
            if not vals:
                return {"n": 0, "mean": None, "p50": None, "p90": None, "max": None}
            a = np.asarray(vals, dtype=float)
            return {"n": int(a.size), "mean": float(a.mean()),
                    "p50": float(np.percentile(a, 50)),
                    "p90": float(np.percentile(a, 90)), "max": float(a.max())}

        stations = {s: {
            "queue_wait": q(v["queue_wait_ms"]),
            "blocking": q(v["blocking_ms"]),
            "service": q(v["service_ms"]),
            "external_wait": q(v["external_wait_ms"]),
            "censored_queue_lb": q(v["queue_censored_lb"]),
            "censored_blocking_lb": q(v["blocking_censored_lb"]),
            "censored_service_lb": q(v["service_censored_lb"]),
            "n_stages": v["n_stages"],
        } for s, v in per_station.items()}

        per_sc: dict[tuple[str, str], list[int]] = {}
        for task in self.tasks.values():
            for sr in task.stages:
                if sr.complete_ts is not None and sr.start_ts is not None:
                    per_sc.setdefault((sr.station_id, sr.category), []).append(
                        sr.complete_ts - sr.start_ts)
        service_by_category = {}
        for (sid, cat), vals in per_sc.items():
            arr = np.asarray(vals, dtype=float)
            service_by_category[f"{sid}|{cat}"] = {
                "n": int(arr.size), "mean": float(arr.mean())}

        unfinished_states = {s: 0 for s in
                             (S_EXT, S_QUEUED, S_SERVICE, S_BLOCKED)}
        for tid in unfinished:
            state = self.tasks[tid].state
            unfinished_states[state] = unfinished_states.get(state, 0) + 1

        return {
            "tasks_total": total,
            "tasks_done": done,
            "tasks_censored": len(unfinished),
            "throughput": done,
            "horizon": horizon,
            "overall_queue_wait": q(overall_wait),
            "unfinished_states": unfinished_states,
            "stations": stations,
            "service_by_category": service_by_category,
            "seat_utilization": self._seat_utilization(horizon),
        }

    def _seat_utilization(self, horizon: int) -> dict:
        window = max(1, horizon - self.start)
        out = {}
        for s in self.stations:
            service_t = 0
            blocked_t = 0
            for task in self.tasks.values():
                for sr in task.stages:
                    if sr.station_id != s:
                        continue
                    if sr.start_ts is None:
                        continue
                    end_service = sr.complete_ts if sr.complete_ts is not None else horizon
                    end_service = min(end_service, horizon)
                    if end_service > sr.start_ts:
                        service_t += end_service - sr.start_ts
                    if sr.complete_ts is not None and sr.has_next:
                        end_block = sr.leave_ts if sr.leave_ts is not None else horizon
                        end_block = min(end_block, horizon)
                        if end_block > sr.complete_ts:
                            blocked_t += end_block - sr.complete_ts
            seat_ms = 0
            prev = self.start
            for ev in sorted(self.capacity_changes, key=lambda e: e["ts"]):
                seg_end = min(ev["ts"], horizon)
                if seg_end > prev:
                    seats = self._shift_at(prev, s)[0]
                    seat_ms += (seg_end - prev) * seats
                prev = max(prev, ev["ts"])
            if horizon > prev:
                seat_ms += (horizon - prev) * self._shift_at(prev, s)[0]
            out[s] = {
                "service_occupancy_ratio": (service_t / seat_ms) if seat_ms else None,
                "blocked_occupancy_ratio": (blocked_t / seat_ms) if seat_ms else None,
                "service_seat_ms": service_t, "blocked_seat_ms": blocked_t,
                "available_seat_ms": seat_ms,
            }
        return out

    def _occupancy(self, horizon: int) -> dict:
        """Stepwise occupancy intervals for charts (per station)."""
        out = {}
        for s in self.stations:
            intervals = []
            for task in self.tasks.values():
                for sr in task.stages:
                    if sr.station_id != s or sr.start_ts is None:
                        continue
                    service_end = sr.complete_ts if sr.complete_ts is not None else horizon
                    intervals.append([sr.start_ts, min(service_end, horizon), "service",
                                      task.task_id, sr.stage_index])
                    if sr.complete_ts is not None and sr.has_next:
                        block_end = sr.leave_ts if sr.leave_ts is not None else horizon
                        intervals.append([sr.complete_ts, min(block_end, horizon), "blocked",
                                          task.task_id, sr.stage_index])
            out[s] = intervals
        return out

    def _blocking_chain(self) -> dict:
        blocked = []
        for s in self.stations:
            for bstart, bt, btid, bstage in self.blocked_heap[s]:
                sr = self.tasks[btid].stages[bstage]
                if sr.state != S_BLOCKED:
                    continue
                blocked.append({
                    "task_id": btid, "stage_index": bstage, "station_id": sr.station_id,
                    "next_station": self.tasks[btid].stages[bstage + 1].station_id,
                    "blocking_start": bstart,
                })
        stations = {}
        for s in self.stations:
            seats, waitcap = self._shift_at(self._last_time(), s)
            stations[s] = {
                "seats": seats, "seats_in_use": self.seats_in_use[s],
                "wait_capacity": waitcap, "wait_len": len(self.wait_heap[s]),
                "external_wait": len(self.ext_heap[s]),
                "blocked_incoming": len(self.blocked_heap[s]),
            }
        return {
            "reason": "no scheduled event remains; unfinished tasks cannot progress",
            "blocked_tasks": blocked, "stations": stations,
            "note": ("capacity is not restored; this is a reported blocking chain, "
                     "not an infinite wait in the simulator"),
        }


# ----------------------------------------------------------------- statistics
def simulation_event_rows(run: dict) -> list[dict]:
    """Flatten simulated stages into recomputable event rows."""
    rows = []
    for task in run["tasks"]:
        for st in task["stages"]:
            tid, si, sid = st["task_id"], st["stage_index"], st["station_id"]
            cat = st.get("category", "default")
            if st["arrive_ts"] is not None:
                rows.append(dict(task_id=tid, stage_index=si, station_id=sid,
                                 category=cat, event_type=EV_ARRIVE, ts=st["arrive_ts"],
                                 seq=0, inferred=False))
            if st["enqueue_ts"] is not None:
                rows.append(dict(task_id=tid, stage_index=si, station_id=sid,
                                 category=cat, event_type=EV_ENQUEUE, ts=st["enqueue_ts"],
                                 seq=1, inferred=False))
            if st["start_ts"] is not None:
                rows.append(dict(task_id=tid, stage_index=si, station_id=sid,
                                 category=cat, event_type=EV_START, ts=st["start_ts"],
                                 seq=2, inferred=False))
            if st["complete_ts"] is not None:
                rows.append(dict(task_id=tid, stage_index=si, station_id=sid,
                                 category=cat, event_type=EV_COMPLETE, ts=st["complete_ts"],
                                 seq=3, inferred=False))
            if st["leave_ts"] is not None:
                rows.append(dict(task_id=tid, stage_index=si, station_id=sid,
                                 category=cat, event_type=EV_LEAVE, ts=st["leave_ts"],
                                 seq=4, inferred=False))
            if st["exit_ts"] is not None:
                rows.append(dict(task_id=tid, stage_index=si, station_id=sid,
                                 category=cat, event_type=EV_EXIT, ts=st["exit_ts"],
                                 seq=5, inferred=False))
    rows.sort(key=lambda r: (r["ts"], r["task_id"], r["stage_index"], r["seq"]))
    return rows
