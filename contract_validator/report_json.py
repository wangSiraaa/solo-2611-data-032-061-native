"""Canonical JSON rendering and JSONL ingress with exact line accounting.

The reader never guesses at file boundaries: lines are split on ``\\n`` only
(a trailing ``\\n`` does not create a phantom final line), so the number of
physical input lines is well defined and invariant under how the last line is
terminated.

Canonical JSON is rendered with ``ensure_ascii=True`` (ASCII-only, deterministic
across locales), sorted object keys, and the standard library's compact
separators.  Object key order therefore has no effect on the canonical output.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

# 64 KiB of raw bytes per input line, excluding the terminating newline.
MAX_LINE_BYTES = 64 * 1024


class IngressError(Exception):
    """A rejected input line, carrying the stable error code used in reports."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class DuplicateKeyError(Exception):
    pass


def _object_no_duplicates(pairs):
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise DuplicateKeyError(key)
        seen[key] = value
    return seen


def _reject_constant(token: str):
    # Python's json accepts NaN/Infinity/-Infinity by default (a superset of
    # RFC 8259).  Synthetic data must stay portable JSON, so reject outright.
    raise IngressError("invalid-json", f"non-finite JSON number {token!r} is not allowed")


def _ensure_finite(value) -> None:
    """Raise IngressError if any number in ``value`` is non-finite.

    ``parse_constant`` only sees the bare ``NaN``/``Infinity``/``-Infinity``
    tokens; a huge literal such as ``1e400`` parses directly to ``inf`` without
    passing through it.  Walking the parsed tree (iteratively, so no depth
    dependency) catches every such value before canonical rendering, keeping
    accepted output strictly portable JSON.
    """
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, float):
            if item != item or item == float("inf") or item == float("-inf"):
                raise IngressError(
                    "invalid-json", "non-finite number is not allowed"
                )
        elif isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)


def parse_json_line(text: str):
    """Parse one JSON text line, enforcing duplicate-key and JSON validity.

    Raises IngressError with a stable code.  Nesting depth is bounded by a
    pre-scan (see :func:`measure_nesting`) performed by the caller; json's own
    recursion guard is never permitted to escape.
    """
    try:
        value = json.loads(
            text,
            object_pairs_hook=_object_no_duplicates,
            parse_constant=_reject_constant,
        )
    except DuplicateKeyError as exc:  # pragma: no cover - message path selector
        raise IngressError("duplicate-key", f"duplicate object key {exc.args[0]!r}")
    except IngressError:
        raise
    except json.JSONDecodeError as exc:
        raise IngressError("invalid-json", f"invalid JSON at column {exc.colno}")
    except RecursionError:
        # Only reachable if the depth pre-scan and json disagree (it should
        # not); isolate rather than crash the process.
        raise IngressError("too-deep", "input nesting exceeds the 64-level limit")
    _ensure_finite(value)
    return value


def measure_nesting(text: str) -> int:
    """Return the maximum nesting depth of ``{}``/``[]`` in a JSON text line.

    The scan is string-aware so brackets inside string literals do not count.
    Depth is 1 for a single level; a scalar has depth 0.  This runs before
    ``json.loads`` so an over-deep line is rejected as data instead of being
    allowed to trip the interpreter's recursion guard.
    """
    depth = 0
    max_depth = 0
    in_string = False
    escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{" or ch == "[":
            depth += 1
            if depth > max_depth:
                max_depth = depth
        elif ch == "}" or ch == "]":
            depth -= 1
            if depth < 0:
                # Unbalanced closer; json.loads will reject it, but never let
                # the pre-scan produce nonsense.
                depth = 0
    return max_depth


def split_lines(raw: bytes) -> list[bytes]:
    """Split raw file bytes into physical lines (no phantom trailing line)."""
    if not raw:
        return []
    body = raw[:-1] if raw.endswith(b"\n") else raw
    return body.split(b"\n")


def iter_physical_lines(stream, chunk_size: int = 64 * 1024):
    """Yield ``(line_bytes, too_long, excess)`` for a binary stream, line by line.

    Splitting is on ``\\n`` only.  A trailing newline does not create a phantom
    final line, and a final line without a newline is still yielded.  Input is
    consumed in fixed-size chunks, so memory stays bounded even if a single
    line is enormous; such a line is flagged ``too_long`` (its bytes are not
    retained or returned) and ``excess`` reports how many bytes were observed.
    """
    pending = bytearray()
    too_long = False
    excess = 0

    while True:
        chunk = stream.read(chunk_size)
        if not chunk:
            break
        start = 0
        while True:
            newline = chunk.find(b"\n", start)
            if newline == -1:
                remainder = chunk[start:]
                if too_long:
                    excess += len(remainder)
                elif len(pending) + len(remainder) > MAX_LINE_BYTES:
                    too_long = True
                    excess = len(pending) + len(remainder)
                    pending = bytearray()
                else:
                    pending.extend(remainder)
                break
            segment = chunk[start:newline]
            if too_long:
                excess += len(segment)
                yield b"", True, excess
            elif len(pending) + len(segment) > MAX_LINE_BYTES:
                yield b"", True, len(pending) + len(segment)
            elif pending:
                pending.extend(segment)
                yield bytes(pending), False, 0
            else:
                yield segment, False, 0
            pending = bytearray()
            too_long = False
            excess = 0
            start = newline + 1
    if too_long:
        yield b"", True, excess
    elif pending:
        yield bytes(pending), False, 0


def decode_line(line: bytes) -> tuple[str, bool]:
    """Decode one physical line to text.

    Returns ``(text, ok)``.  UTF-8 (optionally BOM-terminated) is the only
    accepted encoding; on failure the caller rejects the line as invalid JSON.
    A UTF-8 BOM on the first line is ignored.
    """
    try:
        return line.decode("utf-8"), True
    except UnicodeDecodeError:
        return "", False


def canonical_json(value) -> str:
    """Return the canonical JSON rendering of a Python JSON value."""
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


@dataclass
class LineResult:
    lineno: int
    accepted: bool
    code: str | None = None
    canonical: str | None = None
    data_pointer: str = ""
    schema_pointer: str = ""
    message: str = ""
    successes: list = field(default_factory=list)
    representative_errors: list = field(default_factory=list)


__all__ = [
    "MAX_LINE_BYTES",
    "DuplicateKeyError",
    "IngressError",
    "LineResult",
    "canonical_json",
    "decode_line",
    "iter_physical_lines",
    "measure_nesting",
    "parse_json_line",
    "split_lines",
]
