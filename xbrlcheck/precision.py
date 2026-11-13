"""XBRL ``decimals`` precision intervals.

For ``decimals=n`` a reported numeric value ``v`` represents the closed
interval ``[v - 0.5*10**-n, v + 0.5*10**-n]``.  For ``decimals="INF"`` the
interval is the single point ``[v, v]``.  Negative ``n`` widens the interval
(e.g. ``decimals="-2"`` means the value is stated to the nearest hundred).

All arithmetic is done with :class:`decimal.Decimal` under a local, generous
context; no binary floats are involved.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, localcontext

INF = "INF"
_INT_RE = re.compile(r"^[+-]?\d+$")
_DECIMAL_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$")
_DECIMAL_PRECISION = 60


class PrecisionError(ValueError):
    pass


def parse_decimals(raw: str | None) -> str | None:
    """Validate and canonically normalise a ``decimals`` attribute.

    Returns ``"INF"`` or a canonical integer string (``"+00"`` -> ``"0"``).
    ``None`` stays ``None``; callers decide whether that is allowed.
    """
    if raw is None:
        return None
    s = raw.strip()
    if s == INF:
        return INF
    if not _INT_RE.match(s):
        raise PrecisionError("decimals must be an integer or INF, got %r" % raw)
    return str(int(s))


def parse_numeric(raw: str) -> Decimal:
    """Parse an XBRL numeric lexical value without accepting NaN/Infinity."""
    s = raw.strip()
    if not _DECIMAL_RE.match(s):
        raise PrecisionError("not an xs:decimal value: %r" % raw)
    try:
        value = Decimal(s)
    except InvalidOperation as exc:  # defensive; regex already excludes these
        raise PrecisionError("not a decimal value: %r" % raw) from exc
    if not value.is_finite():
        raise PrecisionError("non-finite value: %r" % raw)
    return value


def precision_interval(value: Decimal, decimals: str) -> tuple[Decimal, Decimal]:
    """Closed interval of true values represented by ``value``/``decimals``."""
    if decimals == INF:
        return (value, value)
    if decimals is None:
        raise PrecisionError("decimals is required for a numeric fact")
    n = int(decimals)
    with localcontext() as ctx:
        ctx.prec = _DECIMAL_PRECISION
        half = Decimal("0.5").scaleb(-n)
        return (value - half, value + half)


def intersect_intervals(
    intervals: list[tuple[Decimal, Decimal]] | tuple[tuple[Decimal, Decimal], ...],
) -> tuple[Decimal, Decimal] | None:
    """Intersection of closed intervals, or ``None`` when empty."""
    if not intervals:
        return None
    low = max(i[0] for i in intervals)
    high = min(i[1] for i in intervals)
    with localcontext() as ctx:
        ctx.prec = _DECIMAL_PRECISION
        if low <= high:
            return (low, high)
    return None


def scale_interval(weight: Decimal, interval: tuple[Decimal, Decimal]) -> tuple[Decimal, Decimal]:
    with localcontext() as ctx:
        ctx.prec = _DECIMAL_PRECISION
        a = weight * interval[0]
        b = weight * interval[1]
    return (a, b) if a <= b else (b, a)


def sum_intervals(intervals: list[tuple[Decimal, Decimal]]) -> tuple[Decimal, Decimal] | None:
    if not intervals:
        return None
    with localcontext() as ctx:
        ctx.prec = _DECIMAL_PRECISION
        low = sum((i[0] for i in intervals), Decimal(0))
        high = sum((i[1] for i in intervals), Decimal(0))
    return (low, high)


def dec_str(value: Decimal | None) -> str | None:
    """Stable, exponent-free decimal rendering (original lexical text is kept
    separately on the fact)."""
    if value is None:
        return None
    if value == 0:
        return "0"
    with localcontext() as ctx:
        ctx.prec = _DECIMAL_PRECISION
        return format(value.normalize(), "f")


def interval_str(interval: tuple[Decimal, Decimal] | None) -> str | None:
    if interval is None:
        return None
    return "[%s, %s]" % (dec_str(interval[0]), dec_str(interval[1]))
