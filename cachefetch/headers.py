"""HTTP header, Vary and Cache-Control helpers for the limited cache protocol.

Only the headers named by the task are understood as Vary dimensions:
``Accept-Language`` and ``Accept`` (case-insensitive), or ``*`` (never
store).  Header values are stripped of leading/trailing SP/HTAB and then
compared exactly.  Missing and empty values both mean the empty string.
"""

from __future__ import annotations

import json
from typing import Iterable, Mapping, Sequence


ALLOWED_VARY_HEADERS = frozenset({"accept-language", "accept"})


class ProtocolError(Exception):
    """A response or scenario violates the limited cache protocol."""


def strip_ows(value: str) -> str:
    """Strip optional whitespace (SP / HTAB) from a header value."""
    return value.strip(" \t")


def normalize_headers(pairs: Iterable[tuple[str, str]]) -> dict[str, str]:
    """Return a lowercase-name header map with stripped values.

    Repeated request header fields are combined with ``", "`` as HTTP does
    for list-valued fields.  The task's scenarios use one value per header,
    but this keeps the comparison deterministic.
    """
    out: dict[str, str] = {}
    for raw_name, raw_value in pairs:
        name = strip_ows(raw_name).lower()
        if not name:
            continue
        value = strip_ows(raw_value)
        if name in out:
            out[name] = out[name] + ", " + value
        else:
            out[name] = value
    return out


def header_value(headers: Mapping[str, str], name: str) -> str:
    """Value of a normalized header, with missing == empty."""
    return headers.get(name.lower(), "")


def parse_vary(header_lines: Sequence[str] | None) -> list[str]:
    """Parse one or more Vary header lines into a canonical rule list.

    The result is either ``[]`` (no variation), a sorted list of lowercase
    allowed header names, or ``["*"]``.  Any other header name, or ``*``
    mixed with other names, is a protocol error.
    """
    if not header_lines:
        return []
    tokens: list[str] = []
    for line in header_lines:
        for part in line.split(","):
            token = strip_ows(part).lower()
            if token:
                tokens.append(token)
    if not tokens:
        return []
    if "*" in tokens:
        if len(tokens) != 1:
            raise ProtocolError("Vary '*' must not be combined with other fields")
        return ["*"]
    unknown = [t for t in tokens if t not in ALLOWED_VARY_HEADERS]
    if unknown:
        raise ProtocolError(
            "Vary may only name Accept-Language or Accept: " + ", ".join(unknown)
        )
    return sorted(set(tokens))


def canonical_vary(rule: Sequence[str]) -> str:
    """Stable JSON representation of a Vary rule."""
    return json.dumps(list(rule), ensure_ascii=False, separators=(",", ":"))


def decode_vary(raw: str) -> list[str]:
    value = json.loads(raw)
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise ValueError("invalid stored Vary rule")
    return value


def vary_key(rule: Sequence[str], headers: Mapping[str, str]) -> str:
    """Canonical cache-key portion for the request under a Vary rule.

    For a non-star rule this is a JSON list of ``[lowercase name, value]``
    pairs in sorted order.  Missing headers appear with ``""``.
    """
    names = list(rule)
    if "*" in names:
        raise ValueError("a Vary '*' rule has no cache key")
    return json.dumps(
        [[name, header_value(headers, name)] for name in sorted(names)],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def explain_vary_key(raw: str) -> list[list[str]]:
    value = json.loads(raw)
    if not isinstance(value, list):
        raise ValueError("invalid stored vary key")
    return value


def parse_cache_control(
    header_lines: Sequence[str] | None,
) -> tuple[bool, int | None]:
    """Parse Cache-Control.

    Returns ``(no_store, max_age)``.  ``max_age`` is ``None`` when absent,
    otherwise a non-negative integer.  Unknown directives are ignored by
    design (see README).  A malformed known directive is a protocol error.
    """
    no_store = False
    max_age: int | None = None
    if not header_lines:
        return no_store, max_age
    for line in header_lines:
        for raw_part in line.split(","):
            part = strip_ows(raw_part)
            if not part:
                continue
            if "=" in part:
                raw_name, raw_parameter = part.split("=", 1)
                has_parameter = True
            else:
                raw_name, raw_parameter = part, ""
                has_parameter = False
            name = strip_ows(raw_name).lower()
            parameter = strip_ows(raw_parameter)

            if name == "max-age":
                if not has_parameter or not parameter:
                    raise ProtocolError("max-age requires a non-negative integer")
                if len(parameter) >= 2 and parameter[0] == '"' and parameter[-1] == '"':
                    parameter = parameter[1:-1]
                if not parameter.isdigit():
                    raise ProtocolError("max-age requires a non-negative integer")
                max_age = int(parameter)
            elif name == "no-store":
                if has_parameter and parameter:
                    raise ProtocolError("no-store does not take a value")
                no_store = True
            # Unknown directives (including no-cache, public, private,
            # must-revalidate, s-maxage, stale-while-revalidate, ...) are
            # deliberately ignored.
    return no_store, max_age


def is_strong_etag(value: str | None) -> bool:
    """Strong ETags are quoted and do not have the W/ weakness prefix.

    The opaque tag may contain visible ASCII (0x21-0x7E) except DQUOTE, plus
    obs-text (0x80-0xFF); this also rules out header-injecting CR/LF bytes.
    """
    if not isinstance(value, str) or len(value) < 2:
        return False
    if not (value.startswith('"') and value.endswith('"')) or value.startswith("W/"):
        return False
    for char in value[1:-1]:
        code = ord(char)
        if char == " ":
            continue
        if code < 0x21 or code == 0x7F or char == '"':
            return False
    return True
