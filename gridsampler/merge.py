"""Compatibility checks and deterministic merging across the two inputs."""

from __future__ import annotations

import numpy as np

from .errors import MergeError

_COORD_ATOL = 1e-9


def _same_coordinates(a: np.ndarray, b: np.ndarray) -> bool:
    """Compare coordinate *sets*, ignoring storage order/direction."""
    if a.size != b.size:
        return False
    return bool(np.allclose(np.sort(a), np.sort(b), rtol=0.0, atol=_COORD_ATOL))


def check_spatial_compatibility(meta_a, meta_b) -> None:
    """Reject files whose lat/lon grids genuinely differ.

    Dimension order and ascending/descending direction may differ (they are
    handled by name and by sampling), but the coordinate values must match.
    """
    problems: list[str] = []
    if not _same_coordinates(meta_a.lat, meta_b.lat):
        problems.append(
            f"lat coordinates differ: {meta_a.lat.tolist()} vs {meta_b.lat.tolist()}"
        )
    if not _same_coordinates(meta_a.lon, meta_b.lon):
        problems.append(
            f"lon coordinates differ: {meta_a.lon.tolist()} vs {meta_b.lon.tolist()}"
        )
    if meta_a.n_lat != meta_b.n_lat:
        problems.append(f"lat sizes differ: {meta_a.n_lat} vs {meta_b.n_lat}")
    if meta_a.n_lon != meta_b.n_lon:
        problems.append(f"lon sizes differ: {meta_a.n_lon} vs {meta_b.n_lon}")
    if problems:
        raise MergeError(
            "incompatible spatial metadata between "
            f"{meta_a.path} and {meta_b.path}: " + "; ".join(problems),
            path=meta_b.path,
            variable=meta_b.variable,
        )


def duplicate_time_map(entries: list[tuple[str, object, str, int]]) -> dict:
    """Map absolute datetime -> [(path, time_index), ...] for duplicate detection.

    ``entries`` are ``(path, datetime, time_units, time_index)`` tuples.
    """
    seen: dict = {}
    for path, dt, _units, index in entries:
        seen.setdefault(dt, []).append((path, index))
    return {dt: locs for dt, locs in seen.items() if len(locs) > 1}


def merge_points(rows: list[dict]) -> list[dict]:
    """Merge station rows from both files in a file-order-independent way.

    Rows are sorted by ``(time_utc, station_id)``. A duplicate
    ``(time_utc, station_id)`` pair is an error rather than silently taking the
    last value.
    """
    keyed: dict[tuple, dict] = {}
    for row in rows:
        key = (row["time_utc"], row["station_id"])
        if key in keyed:
            other = keyed[key]
            raise MergeError(
                "duplicate station/time record for "
                f"station {row['station_id']!r} at {row['time_utc']}: "
                f"{other['file']} and {row['file']}",
                path=row["file"],
            )
        keyed[key] = row
    return [keyed[k] for k in sorted(keyed)]
