"""Configuration parsing and validation.

A *config* describes the observable system:
  * stations (id, seats, priority/merge rules for categories)
  * shifts (observation windows and per-station seat capacity, wait capacity)
  * tasks (stable task_id, non-negative integer priority, finite stage sequence)

Nothing here contains service-time ground truth.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .common import ConfigError, as_int, as_nonneg_int, as_pos_int, load_json, require


@dataclass(frozen=True)
class Station:
    station_id: str
    seats_capacity: int          # base seat count (shifts may override per-station)
    serves_categories: tuple[str, ...] | None = None  # None = all categories


@dataclass(frozen=True)
class Shift:
    shift_id: str
    window_start: int
    window_end: int
    seats: dict[str, int] = field(default_factory=dict)     # station -> seats this shift
    wait_capacity: dict[str, int] = field(default_factory=dict)  # station -> wait slots


@dataclass(frozen=True)
class Task:
    task_id: str
    priority: int
    stages: tuple[str, ...]     # ordered stations; rework via repeats allowed
    category: str = "default"


@dataclass(frozen=True)
class Config:
    stations: dict[str, Station]
    shifts: dict[str, Shift]
    tasks: dict[str, Task]
    station_order: tuple[str, ...]

    def seats_for(self, shift_id: str, station_id: str) -> int:
        sh = self.shifts[shift_id]
        return sh.seats.get(station_id, self.stations[station_id].seats_capacity)

    def wait_for(self, shift_id: str, station_id: str) -> int:
        sh = self.shifts[shift_id]
        return sh.wait_capacity[station_id]

    def stages_of(self, task_id: str) -> tuple[str, ...]:
        return self.tasks[task_id].stages

    def entry_station(self) -> str:
        return self.station_order[0]

    def validate_route(self, task_id: str, station_id: str, stage_index: int) -> None:
        stages = self.stages_of(task_id)
        if stage_index < 0 or stage_index >= len(stages):
            raise ConfigError(
                f"task {task_id} has no stage index {stage_index} (len={len(stages)})")
        if stages[stage_index] != station_id:
            raise ConfigError(
                f"task {task_id} stage {stage_index} is {stages[stage_index]!r}, "
                f"not {station_id!r}")


def _parse_station(raw: Any, idx: int) -> Station:
    require(isinstance(raw, dict), ConfigError, f"station[{idx}] must be an object")
    sid = raw.get("station_id")
    require(isinstance(sid, str) and sid, ConfigError, f"station[{idx}].station_id required")
    seats = as_pos_int(raw.get("seats_capacity", raw.get("seats", 1)),
                       ConfigError, f"{sid}.seats_capacity")
    cats = raw.get("serves_categories")
    if cats is not None:
        require(isinstance(cats, list) and all(isinstance(c, str) for c in cats),
                ConfigError, f"{sid}.serves_categories must be a list of strings")
        cats = tuple(cats)
    return Station(sid, seats, cats)


def _parse_shift(raw: Any, idx: int, stations: dict[str, Station]) -> Shift:
    require(isinstance(raw, dict), ConfigError, f"shift[{idx}] must be an object")
    sid = raw.get("shift_id")
    require(isinstance(sid, str) and sid, ConfigError, f"shift[{idx}].shift_id required")
    ws = as_nonneg_int(raw.get("window_start"), ConfigError, f"{sid}.window_start")
    we = as_nonneg_int(raw.get("window_end"), ConfigError, f"{sid}.window_end")
    require(we > ws, ConfigError, f"{sid}: window_end must exceed window_start")
    seats: dict[str, int] = {}
    for k, v in (raw.get("seats") or {}).items():
        require(k in stations, ConfigError, f"{sid}.seats references unknown station {k!r}")
        seats[k] = as_nonneg_int(v, ConfigError, f"{sid}.seats[{k}]")
    waits: dict[str, int] = {}
    for k, v in (raw.get("wait_capacity") or {}).items():
        require(k in stations, ConfigError, f"{sid}.wait_capacity references unknown station {k!r}")
        waits[k] = as_pos_int(v, ConfigError, f"{sid}.wait_capacity[{k}]")
    for st in stations:
        require(st in waits, ConfigError, f"{sid}: missing wait_capacity for station {st!r}")
    return Shift(sid, ws, we, seats, waits)


def _parse_task(raw: Any, idx: int, stations: dict[str, Station]) -> Task:
    require(isinstance(raw, dict), ConfigError, f"task[{idx}] must be an object")
    tid = raw.get("task_id")
    require(isinstance(tid, str) and tid, ConfigError, f"task[{idx}].task_id required")
    if "priority" not in raw:
        raise ConfigError(f"task {tid}: priority is required and missing")
    prio = as_nonneg_int(raw["priority"], ConfigError, f"{tid}.priority")
    stages_raw = raw.get("stages")
    require(isinstance(stages_raw, list) and stages_raw,
            ConfigError, f"task {tid}: stages must be a non-empty list")
    stages: list[str] = []
    for j, s in enumerate(stages_raw):
        require(isinstance(s, str), ConfigError, f"{tid}.stages[{j}] must be a string")
        require(s in stations, ConfigError, f"{tid}.stages[{j}] references unknown station {s!r}")
        stages.append(s)
    cat = raw.get("category", "default")
    require(isinstance(cat, str) and cat, ConfigError, f"{tid}.category must be a string")
    return Task(tid, prio, tuple(stages), cat)


def parse_config(raw: Any) -> Config:
    require(isinstance(raw, dict), ConfigError, "config root must be an object")
    stations_raw = raw.get("stations")
    require(isinstance(stations_raw, list) and stations_raw, ConfigError, "stations required")
    stations: dict[str, Station] = {}
    station_order: list[str] = []
    for i, s in enumerate(stations_raw):
        st = _parse_station(s, i)
        require(st.station_id not in stations, ConfigError, f"duplicate station {st.station_id!r}")
        stations[st.station_id] = st
        station_order.append(st.station_id)

    shifts_raw = raw.get("shifts")
    require(isinstance(shifts_raw, list) and shifts_raw, ConfigError, "shifts required")
    shifts: dict[str, Shift] = {}
    for i, s in enumerate(shifts_raw):
        sh = _parse_shift(s, i, stations)
        require(sh.shift_id not in shifts, ConfigError, f"duplicate shift {sh.shift_id!r}")
        shifts[sh.shift_id] = sh
    # windows must be disjoint and ordered (we rely on this for reconstruction)
    ordered = sorted(shifts.values(), key=lambda s: s.window_start)
    prev_end = -1
    for sh in ordered:
        require(sh.window_start >= prev_end, ConfigError,
                f"shift {sh.shift_id} overlaps previous observation window")
        prev_end = sh.window_end

    tasks_raw = raw.get("tasks")
    require(isinstance(tasks_raw, list) and tasks_raw, ConfigError, "tasks required")
    tasks: dict[str, Task] = {}
    for i, t in enumerate(tasks_raw):
        tk = _parse_task(t, i, stations)
        require(tk.task_id not in tasks, ConfigError, f"duplicate task {tk.task_id!r}")
        require(tk.stages[0] == station_order[0], ConfigError,
                f"task {tk.task_id} must start at entry station {station_order[0]!r}")
        tasks[tk.task_id] = tk

    cfg = Config(stations, shifts, tasks, tuple(station_order))
    # category/station compatibility
    for tk in tasks.values():
        for st_id in tk.stages:
            allowed = stations[st_id].serves_categories
            if allowed is not None:
                require(tk.category in allowed, ConfigError,
                        f"task {tk.task_id} category {tk.category!r} not served by {st_id}")
    return cfg


def load_config(path: str) -> Config:
    return parse_config(load_json(path))


def config_summary(cfg: Config) -> dict:
    return {
        "stations": [
            {"station_id": s.station_id, "seats_capacity": s.seats_capacity,
             "serves_categories": list(s.serves_categories) if s.serves_categories else None}
            for s in cfg.stations.values()
        ],
        "shifts": [
            {"shift_id": sh.shift_id, "window_start": sh.window_start,
             "window_end": sh.window_end, "seats": dict(sh.seats),
             "wait_capacity": dict(sh.wait_capacity)}
            for sh in cfg.shifts.values()
        ],
        "tasks": [
            {"task_id": t.task_id, "priority": t.priority, "category": t.category,
             "stages": list(t.stages)}
            for t in cfg.tasks.values()
        ],
        "station_order": list(cfg.station_order),
        "n_tasks": len(cfg.tasks),
    }
