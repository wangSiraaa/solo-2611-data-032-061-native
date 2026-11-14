"""Exact decimal-seconds to sample conversion.

Annotations carry times as decimal strings.  We never accumulate a float
running time; instead every timestamp is converted independently with
``Decimal`` and rounded once, at the end, to the nearest sample boundary.
"""

from decimal import Decimal, InvalidOperation

# Exact decimal representation of a sample period.  1/16000 terminates in
# decimal, so products of annotation times and the rate stay exact.
SAMPLES_PER_SECOND = Decimal(16000)


class TimeValueError(ValueError):
    """Raised when a timestamp is not a plain, finite decimal number."""


def parse_seconds(text, *, field="time"):
    """Parse a decimal-seconds string, rejecting floats, exponents, signs.

    Only an unsigned fixed-point decimal such as ``"0"``, ``"1.2"`` or
    ``"0.10003"`` is accepted.  This keeps the mapping rule reproducible:
    what was written is exactly what gets rounded, with no binary-float
    intermediate.
    """
    if not isinstance(text, str):
        raise TimeValueError(f"{field} must be a decimal string, got {type(text).__name__}")
    stripped = text.strip()
    if not stripped:
        raise TimeValueError(f"{field} is empty")
    if len(stripped) > 40:
        # 16000 samples/second only needs modest precision; cap the input so a
        # pathological digit count cannot force huge intermediate integers.
        raise TimeValueError(f"{field} has too many characters: {text!r}")
    if stripped.startswith(("+", "-")):
        raise TimeValueError(f"{field} must be non-negative: {text!r}")
    if "e" in stripped.lower():
        raise TimeValueError(f"{field} must not use exponent notation: {text!r}")
    # Reject anything Decimal accepts but we do not want (e.g. Infinity, NaN,
    # underscores); a plain fractional decimal has at most one dot.
    if stripped.count(".") > 1 or not stripped.replace(".", "", 1).isdigit():
        raise TimeValueError(f"{field} is not a decimal number: {text!r}")
    try:
        value = Decimal(stripped)
    except InvalidOperation as exc:  # pragma: no cover - guarded above
        raise TimeValueError(f"{field} is not a decimal number: {text!r}") from exc
    if not value.is_finite():
        raise TimeValueError(f"{field} must be finite: {text!r}")
    return value


def seconds_to_samples(seconds, *, rate=SAMPLES_PER_SECOND):
    """Map decimal seconds to the nearest sample boundary, exactly.

    Rounding rule: compute ``seconds * rate`` as an exact rational and round
    half up (ties move forward).  The computation uses integer arithmetic on
    the decimal's own digits, so neither binary floats nor Decimal context
    precision can round an intermediate result.  ``rate`` may be an int or a
    Decimal; it is normalised to an integer numerator/denominator pair.
    """
    sign, digits, exponent = seconds.as_tuple()
    if sign:  # rejected earlier, but keep the arithmetic total
        raise ValueError("seconds must be non-negative")
    coefficient = 0
    for digit in digits:
        coefficient = coefficient * 10 + digit

    rate_num, rate_den = _as_ratio(rate)

    if exponent >= 0:
        numerator = coefficient * (10 ** exponent) * rate_num
        denominator = rate_den
    else:
        numerator = coefficient * rate_num
        denominator = rate_den * (10 ** (-exponent))

    quotient, remainder = divmod(numerator, denominator)
    if 2 * remainder >= denominator:
        quotient += 1
    return quotient


def _as_ratio(value):
    """Return ``(numerator, denominator)`` for an int or Decimal."""
    if isinstance(value, int):
        return value, 1
    sign, digits, exponent = value.as_tuple()
    coefficient = 0
    for digit in digits:
        coefficient = coefficient * 10 + digit
    if exponent >= 0:
        return coefficient * (10 ** exponent), 1
    return coefficient, 10 ** (-exponent)


def samples_to_decimal(samples, *, rate=SAMPLES_PER_SECOND):
    """Convert an integer sample index back to an exact Decimal second value."""
    numerator, denominator = _as_ratio(rate)
    return Decimal(samples) / Decimal(numerator) * Decimal(denominator)


def format_seconds(seconds):
    """Render a Decimal seconds value without exponent or trailing zeros."""
    normalized = seconds.normalize()
    # ``normalize`` can produce exponent form such as ``1E+1``.
    text = format(normalized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def format_samples(samples, *, rate=SAMPLES_PER_SECOND):
    """Exact, human-readable seconds for an integer sample count."""
    return format_seconds(samples_to_decimal(samples, rate=rate))
