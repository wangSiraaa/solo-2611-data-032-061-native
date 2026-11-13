"""Scenario file model for the simulated HTTP service.

A scenario is JSON::

    {
      "name": "...",
      "resources": {
        "/path": {
          "content": "...",
          "etag": "\"...\"",
          "cache_control": "max-age=10",
          "vary": ["Accept-Language"],
          "content_type": "text/plain; charset=utf-8",
          "variants": [
            {"match": {"Accept-Language": "en"}, "content": "...", "etag": "\"...\""}
          ],
          "steps": [
            {"cache_control": "max-age=20"},
            {"content": "...", "etag": "\"...\"", "conditional": "force_200"},
            {"truncate": 5},
            {"error": 500},
            {"conditional": "etag_mismatch"}
          ]
        }
      }
    }

``steps`` apply one each to successive requests for that resource (after a
cache hit, no request reaches the server, so no step is consumed).  Persistent
step fields (content/etag/cache_control/vary/content_type/headers/variants)
accumulate into the resource's current representation; transient fields
(conditional/force_200/truncate/error/error_body) affect only that response.

See README for the full protocol boundaries.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from pathlib import Path

from .headers import (
    ProtocolError,
    is_strong_etag,
    parse_cache_control,
    parse_vary,
)


MISSING = object()

DEFAULT_CONTENT_TYPE = "text/plain; charset=utf-8"

CONDITIONAL_MODES = frozenset(
    {
        "force_200",
        "no_entry",
        "etag_mismatch",
        "vary_mismatch",
        "etag_absent",
        "no_vary",
        "no_cache_control",
        "raw_bad_length",
        "raw_extra_body",
    }
)

_RESOURCE_KEYS = {
    "description",
    "content",
    "content_b64",
    "etag",
    "cache_control",
    "vary",
    "content_type",
    "headers",
    "variants",
    "steps",
}
_VARIANT_KEYS = {
    "description",
    "match",
    "content",
    "content_b64",
    "etag",
    "cache_control",
    "content_type",
    "headers",
}
_STEP_KEYS = _VARIANT_KEYS | {
    "vary",
    "variants",
    "error",
    "error_body",
    "truncate",
    "conditional",
    "force_200",
}


class ScenarioError(Exception):
    """The scenario file is malformed or outside the supported protocol."""


@dataclass
class Scenario:
    name: str
    resources: dict[str, dict] = field(default_factory=dict)
    generated: dict = field(default_factory=dict)
    source_path: str | None = None


def _strip(value):
    return value.strip(" \t")


def _decode_content(raw: dict, where: str) -> bytes:
    has_text = "content" in raw
    has_b64 = "content_b64" in raw
    if has_text and has_b64:
        raise ScenarioError(f"{where}: use content or content_b64, not both")
    if has_b64:
        if not isinstance(raw["content_b64"], str):
            raise ScenarioError(f"{where}.content_b64 must be a string")
        try:
            return base64.b64decode(raw["content_b64"], validate=True)
        except Exception as exc:
            raise ScenarioError(f"{where}.content_b64 is not valid base64") from exc
    if has_text:
        if not isinstance(raw["content"], str):
            raise ScenarioError(f"{where}.content must be a string")
        return raw["content"].encode("utf-8")
    return MISSING


def _normalize_vary(value, where: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        lines = [value]
    elif isinstance(value, list) and all(isinstance(x, str) for x in value):
        lines = list(value)
    else:
        raise ScenarioError(f"{where}.vary must be a string or list of strings")
    try:
        return parse_vary(lines)
    except ProtocolError as exc:
        raise ScenarioError(f"{where}.vary: {exc}") from exc


def _normalize_etag(value, where: str) -> str:
    if not isinstance(value, str) or not is_strong_etag(value):
        raise ScenarioError(f"{where}.etag must be a strong quoted ETag")
    return value


def _normalize_cache_control(value, where: str):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ScenarioError(f"{where}.cache_control must be a string or null")
    try:
        parse_cache_control([value])
    except ProtocolError as exc:
        raise ScenarioError(f"{where}.cache_control: {exc}") from exc
    return value


def _normalize_headers(value, where: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ScenarioError(f"{where}.headers must be an object")
    out: dict[str, str] = {}
    for name, header_value in value.items():
        if not isinstance(name, str) or not isinstance(header_value, str):
            raise ScenarioError(f"{where}.headers entries must be strings")
        clean = _strip(name)
        if not clean or ":" in clean or any(ord(ch) < 32 for ch in clean + header_value):
            raise ScenarioError(f"{where}.headers contains an invalid header")
        out[clean] = _strip(header_value)
    return out


def _normalize_common(raw: dict, where: str, allowed: set[str]) -> dict:
    unknown = set(raw) - allowed
    if unknown:
        raise ScenarioError(f"{where}: unknown keys: {', '.join(sorted(unknown))}")
    out: dict = {}
    content = _decode_content(raw, where)
    if content is not MISSING:
        out["content"] = content
    if "etag" in raw:
        out["etag"] = _normalize_etag(raw["etag"], where)
    if "cache_control" in raw:
        out["cache_control"] = _normalize_cache_control(
            raw["cache_control"], where
        )
    if "content_type" in raw:
        if not isinstance(raw["content_type"], str):
            raise ScenarioError(f"{where}.content_type must be a string")
        out["content_type"] = raw["content_type"]
    if "headers" in raw:
        out["headers"] = _normalize_headers(raw["headers"], where)
    return out


def _normalize_variant(raw: dict, where: str) -> dict:
    if not isinstance(raw, dict):
        raise ScenarioError(f"{where} must be an object")
    out = _normalize_common(raw, where, _VARIANT_KEYS)
    match = raw.get("match", {})
    if not isinstance(match, dict):
        raise ScenarioError(f"{where}.match must be an object")
    normalized_match: dict[str, str] = {}
    for name, value in match.items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise ScenarioError(f"{where}.match entries must be strings")
        normalized_match[_strip(name).lower()] = _strip(value)
    out["match"] = normalized_match
    return out


def _normalize_step(raw: dict, where: str) -> dict:
    if not isinstance(raw, dict):
        raise ScenarioError(f"{where} must be an object")
    out = _normalize_common(raw, where, _STEP_KEYS)
    if "vary" in raw:
        out["vary_rule"] = _normalize_vary(raw["vary"], where)
    if "variants" in raw:
        variants = raw["variants"]
        if not isinstance(variants, list):
            raise ScenarioError(f"{where}.variants must be a list")
        out["variants"] = [
            _normalize_variant(item, f"{where}.variants[{index}]")
            for index, item in enumerate(variants)
        ]
    if "conditional" in raw:
        mode = raw["conditional"]
        if mode not in CONDITIONAL_MODES:
            raise ScenarioError(
                f"{where}.conditional must be one of {sorted(CONDITIONAL_MODES)}"
            )
        out["conditional"] = mode
    if raw.get("force_200", False):
        if not isinstance(raw["force_200"], bool):
            raise ScenarioError(f"{where}.force_200 must be a boolean")
        out["force_200"] = True
    if "truncate" in raw:
        value = raw["truncate"]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ScenarioError(f"{where}.truncate must be a non-negative integer")
        out["truncate"] = value
    if "error" in raw:
        value = raw["error"]
        if not isinstance(value, int) or isinstance(value, bool) or not 400 <= value <= 599:
            raise ScenarioError(f"{where}.error must be an HTTP 4xx/5xx integer")
        out["error"] = value
    if "error_body" in raw:
        if not isinstance(raw["error_body"], str):
            raise ScenarioError(f"{where}.error_body must be a string")
        out["error_body"] = raw["error_body"].encode("utf-8")
    if "error_body" in out and "error" not in out:
        raise ScenarioError(f"{where}.error_body requires error")
    return out


def _normalize_resource(raw: dict, where: str) -> dict:
    if not isinstance(raw, dict):
        raise ScenarioError(f"{where} must be an object")
    out = _normalize_common(raw, where, _RESOURCE_KEYS)
    vary_rule = _normalize_vary(raw.get("vary"), where)
    base = {
        "content": out.get("content", b""),
        "etag": out.get("etag", None),
        "cache_control": out.get("cache_control", None),
        "vary_rule": vary_rule,
        "content_type": out.get("content_type", DEFAULT_CONTENT_TYPE),
        "extra_headers": out.get("headers", {}),
    }
    variants = raw.get("variants", [])
    if not isinstance(variants, list):
        raise ScenarioError(f"{where}.variants must be a list")
    normalized_variants = [
        _normalize_variant(item, f"{where}.variants[{index}]")
        for index, item in enumerate(variants)
    ]
    steps = raw.get("steps", [])
    if not isinstance(steps, list):
        raise ScenarioError(f"{where}.steps must be a list")
    normalized_steps = [
        _normalize_step(item, f"{where}.steps[{index}]")
        for index, item in enumerate(steps)
    ]
    return {
        "base": base,
        "variants": normalized_variants,
        "steps": normalized_steps,
        "description": raw.get("description", ""),
    }


def load_scenario(path: str | Path) -> Scenario:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise ScenarioError(f"cannot read scenario: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ScenarioError(f"scenario is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ScenarioError("scenario root must be an object")
    resources = raw.get("resources")
    if not isinstance(resources, dict):
        raise ScenarioError("scenario.resources must be an object")
    scenario = Scenario(
        name=str(raw.get("name", "unnamed")),
        generated=raw.get("generated", {}) if isinstance(raw.get("generated", {}), dict) else {},
        source_path=str(path),
    )
    for target, resource in resources.items():
        if not isinstance(target, str) or not target.startswith("/"):
            raise ScenarioError(f"resource path must start with '/': {target!r}")
        if target in ("/_requests", "/_health"):
            raise ScenarioError(f"resource path is reserved: {target}")
        scenario.resources[target] = _normalize_resource(resource, f"resources[{target!r}]")
    return scenario
