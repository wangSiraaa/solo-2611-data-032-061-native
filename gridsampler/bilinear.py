"""Four-corner bilinear sampling on a regular, possibly descending axis."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# Weight tolerance used to decide whether a zero-weight neighbour counts.
ZERO_WEIGHT_EPS = 1e-12

# Supported positive-weight-missing policies.
POLICIES = ("renormalize", "strict")


@dataclass
class Corner:
    i: int
    j: int
    lat: float
    lon: float
    weight: float  # weight actually applied to the value (0 when no value)
    original_weight: float  # bilinear weight before renormalisation
    valid: bool
    value_c: float | None  # unpacked Celsius value at this corner (None if missing)


@dataclass
class Sample:
    status: str  # ok | missing_neighbour | low_coverage | out_of_range | incomplete_neighbourhood
    reason: str | None
    value: float | None
    corners: list[Corner]
    coverage: float  # valid positive weight / total positive weight (0..1)
    lat_index: int | None
    lon_index: int | None


def _ascending(axis: np.ndarray) -> tuple[np.ndarray, int, int]:
    """Return ``(asc_view, base, step)`` mapping ascending index -> original."""
    if axis[0] <= axis[-1]:
        return axis, 0, 1
    return axis[::-1], axis.size - 1, -1


def _bracket(axis: np.ndarray, target: float):
    """Return ``(i0, i1, frac)``.

    ``i0``/``i1`` are original indices into ``axis``; ``frac`` is the position
    between them (0 -> i0, 1 -> i1). For an exact gridline the second index
    carries zero weight. Returns ``None`` when outside the closed axis span.
    """
    asc, base, step = _ascending(axis)
    if target < asc[0] or target > asc[-1]:
        return None

    # Exact gridline: anchor to one node; the other neighbour's weight is 0.
    loc = np.nonzero(asc == target)[0]
    if loc.size:
        pos = int(loc[0])
        i = base + step * pos
        if pos == asc.size - 1:
            return i, i, 0.0
        return i, base + step * (pos + 1), 0.0

    pos = int(np.searchsorted(asc, target) - 1)
    lo, hi = pos, pos + 1
    frac = (target - asc[lo]) / (asc[hi] - asc[lo])
    return base + step * lo, base + step * hi, float(frac)


def _unique_corners(
    raw: list[tuple[int, int, float]],
) -> list[tuple[int, int, float]]:
    """Merge duplicate (i, j) entries, preserving first-seen order."""
    order: list[tuple[int, int]] = []
    sums: dict[tuple[int, int], float] = {}
    for i, j, w in raw:
        key = (i, j)
        if key not in sums:
            sums[key] = 0.0
            order.append(key)
        sums[key] += w
    return [(i, j, sums[(i, j)]) for (i, j) in order]


def sample_station(
    lat_axis: np.ndarray,
    lon_axis: np.ndarray,
    value_c: np.ndarray,
    valid: np.ndarray,
    lat: float,
    lon: float,
    *,
    policy: str = "renormalize",
    min_coverage: float = 0.5,
) -> Sample:
    """Bilinearly sample one station for one timestamp.

    Zero-weight neighbours (gridline or boundary points) are ignored when
    deciding whether a value can be produced. Positive-weight neighbours that
    are missing are handled by ``policy``:

    * ``renormalize`` -- drop invalid positive-weight corners, renormalise the
      remaining weights, and require ``coverage >= min_coverage``.
    * ``strict`` -- any missing positive-weight corner rejects the sample.
    """
    if policy not in POLICIES:
        raise ValueError(f"unknown policy {policy!r}; expected one of {POLICIES}")

    lb = _bracket(lat_axis, lat)
    xb = _bracket(lon_axis, lon)
    if lb is None or xb is None:
        side: list[str] = []
        if lb is None:
            side.append(
                f"lat {lat} outside [{float(lat_axis.min())}, {float(lat_axis.max())}]"
            )
        if xb is None:
            side.append(
                f"lon {lon} outside [{float(lon_axis.min())}, {float(lon_axis.max())}]"
            )
        return Sample(
            status="out_of_range",
            reason="; ".join(side),
            value=None,
            corners=[],
            coverage=0.0,
            lat_index=None,
            lon_index=None,
        )

    i0, i1, fy = lb
    j0, j1, fx = xb
    anchor_lat, anchor_lon = int(i0), int(j0)

    raw = [
        (int(i), int(j), float(wy * wx))
        for i, wy in ((i0, 1.0 - fy), (i1, fy))
        for j, wx in ((j0, 1.0 - fx), (j1, fx))
    ]
    collapsed = _unique_corners(raw)

    positive = [(i, j, w) for (i, j, w) in collapsed if w > ZERO_WEIGHT_EPS]
    total_positive = sum(w for _, _, w in positive)

    def build_corners(apply_value: bool, divisor: float) -> list[Corner]:
        result: list[Corner] = []
        for i, j, w in collapsed:
            is_valid = bool(valid[i, j])
            if apply_value and w > ZERO_WEIGHT_EPS and is_valid and divisor > 0:
                used = w / divisor
            else:
                used = 0.0
            result.append(
                Corner(
                    i=i,
                    j=j,
                    lat=float(lat_axis[i]),
                    lon=float(lon_axis[j]),
                    weight=float(used),
                    original_weight=float(w),
                    valid=is_valid,
                    value_c=float(value_c[i, j]) if is_valid else None,
                )
            )
        return result

    if total_positive <= ZERO_WEIGHT_EPS:
        return Sample(
            status="incomplete_neighbourhood",
            reason="no positive-weight neighbours",
            value=None,
            corners=build_corners(False, 0.0),
            coverage=0.0,
            lat_index=anchor_lat,
            lon_index=anchor_lon,
        )

    valid_positive = [(i, j, w) for (i, j, w) in positive if bool(valid[i, j])]
    valid_weight = sum(w for _, _, w in valid_positive)
    coverage = valid_weight / total_positive

    if policy == "strict" and len(valid_positive) != len(positive):
        missing = [f"({i},{j})" for i, j, _ in positive if not bool(valid[i, j])]
        return Sample(
            status="missing_neighbour",
            reason=(
                "strict policy: positive-weight neighbour(s) "
                f"{', '.join(missing)} missing"
            ),
            value=None,
            corners=build_corners(False, 0.0),
            coverage=coverage,
            lat_index=anchor_lat,
            lon_index=anchor_lon,
        )

    if not valid_positive:
        return Sample(
            status="missing_neighbour",
            reason="all positive-weight neighbours are missing",
            value=None,
            corners=build_corners(False, 0.0),
            coverage=0.0,
            lat_index=anchor_lat,
            lon_index=anchor_lon,
        )

    if policy == "renormalize" and coverage < min_coverage:
        return Sample(
            status="low_coverage",
            reason=(
                f"valid positive-weight coverage {coverage:.6f} below threshold "
                f"{min_coverage:.6f}"
            ),
            value=None,
            corners=build_corners(False, 0.0),
            coverage=coverage,
            lat_index=anchor_lat,
            lon_index=anchor_lon,
        )

    sampled = sum(w * float(value_c[i, j]) for i, j, w in valid_positive) / valid_weight
    return Sample(
        status="ok",
        reason=None,
        value=float(sampled),
        corners=build_corners(True, valid_weight),
        coverage=float(coverage),
        lat_index=anchor_lat,
        lon_index=anchor_lon,
    )
