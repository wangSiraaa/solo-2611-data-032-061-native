"""Validation for the request shape shared by the coordinator and participants.

A submit request looks like::

    {
      "txid": "tx-0001",
      "participants": {
        "p1": [{"key": "a", "expected": "ABSENT" | <int>, "new": <int>}, ...],
        "p2": [ ... ]
      }
    }

Rules enforced here (and re-checked independently by each participant):
  * txid is a non-empty string, at most MAX_TXID_LEN characters;
  * exactly the two endpoints p1 and p2 are present, no unknown endpoints;
  * each side holds at most ten update items;
  * keys are non-empty strings, at most 64 characters, unique per side;
  * expected is either the sentinel string ABSENT or an integer;
  * new is an integer (SQLite INTEGER is a signed 64-bit value);
  * ABSENT may only be used for a genuine insert; using it for a key that
    already exists is a condition failure detected in prepare, not a syntax
    error (an illegal *label* is rejected here).
"""
from __future__ import annotations

from .util import DemoError, MAX_KEYS_PER_SIDE, MAX_TXID_LEN

ENDPOINTS = ("p1", "p2")
ABSENT = "ABSENT"
INT64_MIN = -(2 ** 63)
INT64_MAX = 2 ** 63 - 1
MAX_KEY_LEN = 64


def validate_txid(txid) -> str:
    if not isinstance(txid, str) or not txid.strip():
        raise DemoError("txid must be a non-empty string")
    if len(txid) > MAX_TXID_LEN:
        raise DemoError(f"txid must be at most {MAX_TXID_LEN} characters")
    if any(ch in txid for ch in "\n\r\t"):
        raise DemoError("txid must not contain control whitespace")
    return txid


def validate_side(endpoint: str, items) -> list[dict]:
    if not isinstance(items, list):
        raise DemoError(f"updates for {endpoint} must be a JSON array")
    if len(items) > MAX_KEYS_PER_SIDE:
        raise DemoError(
            f"at most {MAX_KEYS_PER_SIDE} update items per side, "
            f"got {len(items)} for {endpoint}"
        )
    seen: set[str] = set()
    normalized: list[dict] = []
    for index, item in enumerate(items):
        where = f"{endpoint} update #{index}"
        if not isinstance(item, dict):
            raise DemoError(f"{where} must be an object")
        unknown = set(item) - {"key", "expected", "new"}
        if unknown:
            raise DemoError(f"{where} has unknown fields: {sorted(unknown)}")
        key = item.get("key")
        if not isinstance(key, str) or not key:
            raise DemoError(f"{where}: key must be a non-empty string")
        if len(key) > MAX_KEY_LEN:
            raise DemoError(f"{where}: key must be at most {MAX_KEY_LEN} characters")
        if key in seen:
            raise DemoError(f"{where}: duplicate key {key!r} on side {endpoint}")
        seen.add(key)
        if "expected" not in item or "new" not in item:
            raise DemoError(f"{where}: both 'expected' and 'new' are required")
        expected = item["expected"]
        if expected != ABSENT:
            if isinstance(expected, bool) or not isinstance(expected, int):
                raise DemoError(
                    f"{where}: expected must be ABSENT or an integer"
                )
            _check_int64(where, "expected", expected)
        new = item["new"]
        if isinstance(new, bool) or not isinstance(new, int):
            raise DemoError(f"{where}: new must be an integer")
        _check_int64(where, "new", new)
        normalized.append({"key": key, "expected": expected, "new": new})
    return normalized


def _check_int64(where: str, field: str, value: int) -> None:
    if not (INT64_MIN <= value <= INT64_MAX):
        raise DemoError(f"{where}: {field} is outside signed 64-bit range")


def validate_request(req) -> dict:
    """Return a canonicalised request or raise DemoError."""
    if not isinstance(req, dict):
        raise DemoError("request must be a JSON object")
    unknown = set(req) - {"txid", "participants"}
    if unknown:
        raise DemoError(f"request has unknown top-level fields: {sorted(unknown)}")
    txid = validate_txid(req.get("txid"))
    participants = req.get("participants")
    if not isinstance(participants, dict):
        raise DemoError("participants must be a JSON object")
    missing = [p for p in ENDPOINTS if p not in participants]
    if missing:
        raise DemoError(f"request must contain both endpoints p1 and p2; missing {missing}")
    extra = sorted(set(participants) - set(ENDPOINTS))
    if extra:
        raise DemoError(f"unknown endpoints in request: {extra}")
    canonical = {
        "txid": txid,
        "participants": {
            endpoint: validate_side(endpoint, participants[endpoint])
            for endpoint in ENDPOINTS
        },
    }
    return canonical


def canonical_json(value) -> str:
    import json

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

def canonicalize_lenient(req) -> dict:
    """Structural normalisation used by the coordinator before fan-out.

    Only the txid, the two endpoint names and the JSON-serialisability of the
    sides are checked here.  Deeper rules (ten-key limit, duplicate keys,
    ABSENT/int typing) are enforced by each participant in prepare, which
    answers NO for an illegal request.  This is what makes an illegal request
    end in an ABORT rather than a pre-protocol parse error.
    """
    if not isinstance(req, dict):
        raise DemoError("request must be a JSON object")
    unknown = set(req) - {"txid", "participants"}
    if unknown:
        raise DemoError(f"request has unknown top-level fields: {sorted(unknown)}")
    txid = validate_txid(req.get("txid"))
    participants = req.get("participants")
    if not isinstance(participants, dict):
        raise DemoError("participants must be a JSON object")
    missing = [p for p in ENDPOINTS if p not in participants]
    if missing:
        raise DemoError(f"request must contain both endpoints p1 and p2; missing {missing}")
    extra = sorted(set(participants) - set(ENDPOINTS))
    if extra:
        raise DemoError(f"unknown endpoints in request: {extra}")
    # The sides are passed through verbatim; each participant validates its
    # own side.  They must at least be JSON arrays so the transport is sound.
    for endpoint in ENDPOINTS:
        if not isinstance(participants[endpoint], list):
            raise DemoError(f"updates for {endpoint} must be a JSON array")
    return {"txid": txid,
            "participants": {e: participants[e] for e in ENDPOINTS}}

