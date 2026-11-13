"""Shared data model: constants, exceptions, JSON I/O helpers.

All timestamps are integer milliseconds on one common clock.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Iterable


# --- event types -----------------------------------------------------------
EV_ARRIVE = "arrive"            # external arrival into station-1 external waiting
EV_ENQUEUE = "enqueue"          # enter the waiting area of a station
EV_START = "start_service"      # occupy a service seat and begin work
EV_COMPLETE = "service_complete"  # work finished (seat still held until leave)
EV_LEAVE = "leave_station"      # release upstream seat; enter downstream wait
EV_EXIT = "exit"                # final departure from the system

EVENT_TYPES = {EV_ARRIVE, EV_ENQUEUE, EV_START, EV_COMPLETE, EV_LEAVE, EV_EXIT}

# state names used in reconstruction
ST_EXT_WAIT = "external_waiting"
ST_QUEUED = "queued"
ST_SERVICE = "in_service"
ST_BLOCKED = "blocked"          # service complete but upstream seat retained
ST_DONE = "finished"
ST_UNKNOWN = "unknown"

# observation status buckets
STATUS_EXACT = "exact"
STATUS_RIGHT = "right_censored"
STATUS_UPPER = "interval_upper"
STATUS_UNKNOWN = "unknown"


class ConfigError(ValueError):
    """Configuration (task definitions / stations / shifts) is invalid."""


class LogError(ValueError):
    """Operational log violates event semantics."""


class CalibrationError(ValueError):
    """Calibration cannot produce a defensible model."""


def load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def dump_json(path: str, obj: Any) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2, sort_keys=False)
        fh.write("\n")


def require(cond: bool, exc: type[Exception], msg: str) -> None:
    if not cond:
        raise exc(msg)


def as_int(value: Any, exc: type[Exception], name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise exc(f"{name} must be an integer, got {value!r}")
    return value


def as_nonneg_int(value: Any, exc: type[Exception], name: str) -> int:
    v = as_int(value, exc, name)
    if v < 0:
        raise exc(f"{name} must be >= 0, got {v}")
    return v


def as_pos_int(value: Any, exc: type[Exception], name: str) -> int:
    v = as_int(value, exc, name)
    if v <= 0:
        raise exc(f"{name} must be > 0, got {v}")
    return v
