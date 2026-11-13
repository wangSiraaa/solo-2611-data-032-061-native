"""Numeric helpers: canonical geometry keys and exact integer apportionment.

Area arithmetic uses IEEE-754 doubles as produced by GEOS.  Wh apportionment
converts those doubles to exact rationals (``Fraction``) so the quota
calculation has no additional rounding; only the final integer allocation is
rounded, via the largest-remainder rule.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Sequence

from shapely import normalize, to_wkt
from shapely.geometry.base import BaseGeometry


def decimals_for_grid(grid: float) -> int:
    """Number of decimal places needed to represent a grid step.

    ``0.01`` -> 2.  A tiny safety margin is added so canonical keys are not
    dominated by coordinate noise far below the declared precision.
    """
    if grid <= 0:
        return 6
    decimals = max(0, math.ceil(-math.log10(grid)))
    return decimals


def canonical_key(geom: BaseGeometry, grid: float) -> str:
    """Deterministic, order-independent geometry identity.

    ``shapely.normalize`` puts rings and coordinates into GEOS canonical order.
    WKT rounding to the declared precision makes the key stable against
    last-bit differences while still distinguishing real geometry.
    """
    decimals = decimals_for_grid(grid) + 6
    return to_wkt(normalize(geom), rounding_precision=decimals, trim=True)


@dataclass(frozen=True)
class ShareDetail:
    """Per-fragment apportionment evidence."""

    floor_wh: int
    remainder_wh: int
    exact_share: Fraction
    remainder_fraction: Fraction
    rank: int | None

    @property
    def total_wh(self) -> int:
        return self.floor_wh + self.remainder_wh

    def as_dict(self) -> dict[str, Any]:
        return {
            "floor_wh": self.floor_wh,
            "remainder_wh": self.remainder_wh,
            "total_wh": self.total_wh,
            "exact_share": _fraction_to_float(self.exact_share),
            "exact_share_expr": f"{self.exact_share.numerator}/{self.exact_share.denominator}",
            "fractional_remainder": _fraction_to_float(self.remainder_fraction),
            "fractional_remainder_expr": (
                f"{self.remainder_fraction.numerator}/{self.remainder_fraction.denominator}"
            ),
            "remainder_rank": self.rank,
        }


def _fraction_to_float(value: Fraction) -> float:
    return float(value)


def allocate_wh(
    total_wh: int,
    weights: Sequence[float],
    tie_keys: Sequence[Any],
) -> tuple[list[int], list[ShareDetail]]:
    """Split ``total_wh`` across weighted items with strict conservation.

    Each item's exact quota is ``total_wh * weight_i / sum(weights)`` as a
    rational.  The integer floors are handed out first; the remaining units go
    one each to the largest fractional remainders.  Equal remainders are broken
    by ``tie_keys`` (ascending), which the caller supplies as
    ``(owner_id, canonical_geometry)``.
    """
    if total_wh < 0:
        raise ValueError("total_wh must be non-negative")
    if len(weights) != len(tie_keys):
        raise ValueError("weights and tie_keys must have the same length")
    n = len(weights)
    if n == 0:
        if total_wh != 0:
            raise ValueError("cannot apportion energy without fragments")
        return [], []

    exact_weights = [Fraction.from_float(float(w)) for w in weights]
    if any(w < 0 for w in exact_weights):
        raise ValueError("weights must be non-negative")
    weight_sum = sum(exact_weights, Fraction(0))

    if total_wh == 0 or weight_sum == 0:
        details = [
            ShareDetail(0, 0, Fraction(0), Fraction(0), None)
            for _ in range(n)
        ]
        return [0] * n, details

    exact_shares = [Fraction(total_wh) * w / weight_sum for w in exact_weights]
    floors = [share.numerator // share.denominator for share in exact_shares]
    fractions = [share - floor for share, floor in zip(exact_shares, floors)]
    remainder = total_wh - sum(floors)
    if not (0 <= remainder < n):
        # Exact arithmetic makes this unreachable; fail loudly if it is not.
        raise AssertionError(
            f"invalid remainder {remainder} for {n} fragments (total {total_wh})"
        )

    order = sorted(
        range(n),
        key=lambda i: (-fractions[i], _tie_key(tie_keys[i])),
    )
    awarded = set(order[:remainder])
    allocations = [
        floors[i] + (1 if i in awarded else 0)
        for i in range(n)
    ]
    rank_of = {index: rank + 1 for rank, index in enumerate(order)}
    details = [
        ShareDetail(
            floor_wh=floors[i],
            remainder_wh=1 if i in awarded else 0,
            exact_share=exact_shares[i],
            remainder_fraction=fractions[i],
            rank=rank_of[i],
        )
        for i in range(n)
    ]
    if sum(allocations) != total_wh:
        raise AssertionError("apportionment failed to conserve energy")
    return allocations, details


def split_energy(
    total_wh: int,
    items: Sequence[tuple[str, str, float]],
) -> tuple[list[int], list[ShareDetail]]:
    """Convenience wrapper: ``items`` are ``(owner_id, geometry_key, area)``."""
    weights = [area for _owner, _key, area in items]
    tie_keys = [(owner, key) for owner, key, _area in items]
    return allocate_wh(total_wh, weights, tie_keys)


def _tie_key(key: Any) -> tuple:
    """Normalise tie keys so mixed strings/tuples compare deterministically."""
    if isinstance(key, tuple):
        return tuple(str(part) for part in key)
    return (str(key),)
