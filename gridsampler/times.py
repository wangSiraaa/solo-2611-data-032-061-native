"""Time handling for 'hours since <reference>' with a standard calendar."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from .errors import SamplerError

_TIME_RE = re.compile(
    r"^\s*(?P<unit>[A-Za-z]+)\s+since\s+"
    r"(?P<date>\d{4}-\d{1,2}-\d{1,2})"
    r"(?:[ T](?P<time>\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?))?"
    r"(?:\s*(?P<tz>Z|[+-]\d{2}:?\d{2}))?\s*$"
)


def parse_hours_since(units: str, path: str, variable: str) -> datetime:
    """Parse a ``hours since ...`` reference into an aware UTC datetime."""
    match = _TIME_RE.match(units)
    if not match:
        raise SamplerError(
            f"cannot parse time units {units!r}",
            path=path,
            variable=variable,
            kind="invalid_time",
        )
    if match.group("unit").lower() not in {"hours", "hour"}:
        raise SamplerError(
            f"time units {units!r} are not in hours",
            path=path,
            variable=variable,
            kind="unsupported_time_units",
        )
    date_part = match.group("date")
    time_part = match.group("time") or "00:00:00"
    try:
        ref = datetime.fromisoformat(f"{date_part}T{time_part}")
    except ValueError as exc:
        raise SamplerError(
            f"cannot parse time reference in {units!r}: {exc}",
            path=path,
            variable=variable,
            kind="invalid_time",
        ) from exc
    tz = match.group("tz")
    if tz:
        if tz == "Z":
            ref = ref.replace(tzinfo=timezone.utc)
        else:
            sign = 1 if tz[0] == "+" else -1
            digits = tz[1:].replace(":", "")
            offset = timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
            ref = ref.replace(tzinfo=timezone(sign * offset))
    else:
        ref = ref.replace(tzinfo=timezone.utc)
    return ref.astimezone(timezone.utc)


def hours_to_datetimes(
    values, units: str, path: str, variable: str
) -> list[datetime]:
    """Convert numeric hour offsets to UTC datetimes (standard calendar)."""
    ref = parse_hours_since(units, path, variable)
    out: list[datetime] = []
    for raw in values:
        try:
            out.append(ref + timedelta(hours=float(raw)))
        except OverflowError as exc:
            raise SamplerError(
                f"time offset {raw!r} is out of range: {exc}",
                path=path,
                variable=variable,
                kind="invalid_time",
            ) from exc
    return out


def iso(dt: datetime) -> str:
    """Deterministic ISO-8601 rendering in UTC (``Z`` suffix)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    text = dt.isoformat(timespec="seconds")
    return text.replace("+00:00", "Z")
