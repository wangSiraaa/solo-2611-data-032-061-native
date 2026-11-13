"""Deterministic generator for the bundled scenario JSON files.

``python -m cachefetch generate`` rewrites ``scenarios/*.json`` from the
definitions here.  Everything is deterministic and project-generated.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

SCENARIO_DIR = Path(__file__).resolve().parent.parent / "scenarios"

LARGE_BODY = (
    "BEGIN-ASSET-0001\n"
    + "".join(
        f"line {index:04d} of the deterministic generated asset\n"
        for index in range(1, 201)
    )
    + "END-ASSET-0001\n"
)

LANG_EN = "Hello! This is the English representation.\n"
LANG_ZH = "你好！这是简体中文表示。\n"
LANG_BASE = "Greeting for any other Accept-Language value.\n"
LANG_CASE_BASE = "Case-insensitive scenario header name test.\n"
LANG_CASE_ZH = "大小写不敏感场景头名测试。\n"

GREETING_BASE = "Greeting for any other Accept-Language value.\n"
GREETING_ZH = "问候（仅用于 Vary 规则切换测试）。\n"
GREETING_NEW = "Greeting after the Vary rule switched to Accept.\n"

DEFAULT_BASE = "Default representation. Vary names Accept.\n"
DEFAULT_JSON = '{"representation":"default"}\n'

CLOCK_BODY = "Clock body used for the freshness boundary tests.\n"
CLOCK_ETAG = '"clock-v1"'

DIRECTIVES_BODY = "Unknown directives are ignored; max-age is honoured.\n"
DIRECTIVES_ETAG = '"directives-v1"'

ZERO_BODY = "max-age=0 is never fresh, not even at the receive instant.\n"
ZERO_ETAG = '"zero-v1"'

NOMAX_BODY = "No max-age: stored only for revalidation, never served fresh.\n"
NOMAX_ETAG = '"nomax-v1"'

UPDATE_BODY = "Update test: the original representation.\n"
UPDATE_BODY_V2 = "Update test: representation replaced by a later 200.\n"
UPDATE_ETAG = '"update-v1"'

KEEP_BODY = "Keep test: a 304 without Cache-Control keeps the old max-age.\n"
KEEP_ETAG = '"keep-v1"'

REPLACE_V1 = "Replacement test, representation v1.\n"
REPLACE_V2 = "Replacement test, representation v2 with a new ETag.\n"

PLAIN_BODY = "no-store: this response must never become a cache entry.\n"
PLAIN_BODY_AFTER = "no-store: this body comes from the network, not the cache.\n"
PLAIN_ETAG = '"plain-v1"'

STAR_BODY = "Vary '*' responses have no cache key and are never stored.\n"
STAR_ETAG = '"star-v1"'

BADETAG_BODY = "304 with a mismatching ETag must be rejected.\n"
BADETAG_ETAG = '"badetag-v1"'

BADVARY_BODY = "304 with a mismatching Vary must be rejected.\n"
BADVARY_ETAG = '"badvary-v1"'

NOENTRY_BODY = "Unsolicited 304: there is no stored entry for this URL.\n"
NOENTRY_ETAG = '"noentry-v1"'

ABSENT_BODY = "304 without an ETag cannot prove the stored validator.\n"
ABSENT_ETAG = '"absent-v1"'

NOVARY_BODY = "304 without a Vary header keeps the stored rule.\n"
NOVARY_ETAG = '"novary-v1"'

TRUNC_BODY_V1 = "Truncation test, version 1.\n"
TRUNC_BODY_V2 = (
    "Truncation test, version 2: this body is much longer than five bytes.\n"
)
TRUNC_ETAG = '"trunc-v1"'
TRUNC_ETAG_V2 = '"trunc-v2"'

BADLEN_BODY_V1 = "Bad Content-Length test, version 1.\n"
BADLEN_BODY_V2 = "Bad Content-Length test, version 2, full intended body.\n"
BADLEN_ETAG = '"badlen-v1"'
BADLEN_ETAG_V2 = '"badlen-v2"'

EXTRA_BODY_V1 = "Extra body bytes test, version 1.\n"
EXTRA_BODY_V2 = "Extra body bytes test, version 2, intended body.\n"
EXTRA_ETAG = '"extra-v1"'
EXTRA_ETAG_V2 = '"extra-v2"'

ERROR_BODY_V1 = "Server error test, version 1.\n"
ERROR_ETAG = '"error-v1"'

MUTATE_V1 = "Mutation test initial body, version 1.\n"
MUTATE_V2 = "Mutation test new body, version 2, committed after a 200.\n"


def _language() -> dict:
    return {
        "name": "language",
        "description": "Language variants, header-name case, value whitespace.",
        "resources": {
            "/text": {
                "description": "Two Accept-Language variants plus a default.",
                "vary": ["Accept-Language"],
                "content": LANG_BASE,
                "etag": '"lang-base"',
                "cache_control": "max-age=12, public",
                "variants": [
                    {
                        "match": {"accept-language": "en"},
                        "content": LANG_EN,
                        "etag": '"lang-en"',
                    },
                    {
                        "match": {"accept-language": "zh-CN"},
                        "content": LANG_ZH,
                        "etag": '"lang-zh"',
                    },
                ],
                "steps": [],
            },
            "/case": {
                "description": "Scenario Vary header name is upper case.",
                "vary": ["ACCEPT-LANGUAGE"],
                "content": LANG_CASE_BASE,
                "etag": '"case-base"',
                "cache_control": "max-age=12",
                "variants": [
                    {
                        "match": {"Accept-Language": "zh-CN"},
                        "content": LANG_CASE_ZH,
                        "etag": '"case-zh"',
                    }
                ],
                "steps": [],
            },
        },
    }


def _default() -> dict:
    return {
        "name": "default",
        "description": "Vary names Accept; missing and empty values coincide.",
        "resources": {
            "/default": {
                "vary": ["Accept"],
                "content": DEFAULT_BASE,
                "etag": '"default-base"',
                "cache_control": "max-age=10",
                "variants": [
                    {
                        "match": {"accept": "application/json"},
                        "content": DEFAULT_JSON,
                        "etag": '"default-json"',
                    }
                ],
                "steps": [],
            }
        },
    }


def _freshness() -> dict:
    return {
        "name": "freshness",
        "description": "Freshness boundary and unknown Cache-Control directives.",
        "resources": {
            "/clock": {
                "content": CLOCK_BODY,
                "etag": CLOCK_ETAG,
                "cache_control": "max-age=5",
                "steps": [],
            },
            "/directives": {
                "content": DIRECTIVES_BODY,
                "etag": DIRECTIVES_ETAG,
                "cache_control": "max-age=3, s-maxage=999, must-revalidate, public",
                "steps": [
                    {},
                    {"cache_control": "max-age=7, stale-while-revalidate=30"},
                ],
            },
            "/zero": {
                "content": ZERO_BODY,
                "etag": ZERO_ETAG,
                "cache_control": "max-age=0",
                "steps": [
                    {},
                    {"cache_control": "max-age=0"},
                ],
            },
            "/nomax": {
                "content": NOMAX_BODY,
                "etag": NOMAX_ETAG,
                "cache_control": None,
                "steps": [
                    {},
                    {"cache_control": None},
                ],
            },
        },
    }


def _update() -> dict:
    return {
        "name": "update",
        "description": "304 refreshes the clock and max-age; missing max-age keeps it.",
        "resources": {
            "/page": {
                "content": UPDATE_BODY,
                "etag": UPDATE_ETAG,
                "cache_control": "max-age=4",
                "steps": [
                    {},
                    {"cache_control": "max-age=20"},
                    {},
                ],
            },
            "/keep": {
                "content": KEEP_BODY,
                "etag": KEEP_ETAG,
                "cache_control": "max-age=12",
                "steps": [
                    {},
                    {"conditional": "no_cache_control"},
                    {},
                ],
            },
        },
    }


def _replace() -> dict:
    return {
        "name": "replace",
        "description": "A revalidation that returns 200 replaces the entry.",
        "resources": {
            "/page": {
                "content": REPLACE_V1,
                "etag": '"replace-v1"',
                "cache_control": "max-age=3",
                "steps": [
                    {},
                    {
                        "content": REPLACE_V2,
                        "etag": '"replace-v2"',
                        "cache_control": "max-age=30",
                        "force_200": True,
                    },
                    {},
                ],
            }
        },
    }


def _vary() -> dict:
    return {
        "name": "vary",
        "description": "A Vary rule change invalidates all old-rule variants.",
        "resources": {
            "/greeting": {
                "vary": ["Accept-Language"],
                "content": GREETING_BASE,
                "etag": '"greet-base-v1"',
                "cache_control": "max-age=6, public",
                # Only zh-CN matches a variant; every other value (including
                # the empty value) gets the base representation, so EN and
                # "missing header" legitimately share a byte-identical body.
                "variants": [
                    {
                        "match": {"accept-language": "zh-CN"},
                        "content": GREETING_ZH,
                        "etag": '"greet-zh-v1"',
                    }
                ],
                "steps": [
                    {},
                    {},
                    {
                        "vary": ["Accept"],
                        "content": GREETING_NEW,
                        "etag": '"greet-accept-v1"',
                        "force_200": True,
                    },
                    {},
                ],
            }
        },
    }


def _nostore() -> dict:
    return {
        "name": "nostore",
        "description": "no-store and Vary '*' never persist and evict the old entry.",
        "resources": {
            "/plain": {
                "content": PLAIN_BODY,
                "etag": PLAIN_ETAG,
                "cache_control": "max-age=2",
                "steps": [
                    {},
                    {
                        "force_200": True,
                        "cache_control": "max-age=50, no-store",
                        "content": PLAIN_BODY_AFTER,
                        "etag": '"plain-v2"',
                    },
                    {
                        "cache_control": "max-age=50",
                        "content": PLAIN_BODY_AFTER,
                        "etag": '"plain-v2"',
                    },
                ],
            },
            "/star": {
                "content": STAR_BODY,
                "etag": STAR_ETAG,
                "cache_control": "max-age=2",
                "steps": [
                    {},
                    {"force_200": True, "vary": "*"},
                    {"vary": []},
                ],
            },
        },
    }


def _bad304() -> dict:
    return {
        "name": "bad304",
        "description": "Protocol violations in 304 responses and unsolicited 304.",
        "resources": {
            "/badetag": {
                "content": BADETAG_BODY,
                "etag": BADETAG_ETAG,
                "cache_control": "max-age=2",
                "steps": [
                    {},
                    {"conditional": "etag_mismatch"},
                    {},
                ],
            },
            "/badvary": {
                "vary": ["Accept-Language"],
                "content": BADVARY_BODY,
                "etag": BADVARY_ETAG,
                "cache_control": "max-age=2",
                "steps": [
                    {},
                    {"conditional": "vary_mismatch"},
                    {},
                ],
            },
            "/noentry": {
                "content": NOENTRY_BODY,
                "etag": NOENTRY_ETAG,
                "cache_control": "max-age=2",
                "steps": [
                    {"conditional": "no_entry"},
                    {},
                ],
            },
            "/absent": {
                "content": ABSENT_BODY,
                "etag": ABSENT_ETAG,
                "cache_control": "max-age=2",
                "steps": [
                    {},
                    {"conditional": "etag_absent"},
                    {},
                ],
            },
            "/novary": {
                "vary": ["Accept-Language"],
                "content": NOVARY_BODY,
                "etag": NOVARY_ETAG,
                "cache_control": "max-age=2",
                "steps": [
                    {},
                    {"conditional": "no_vary"},
                    {},
                ],
            },
        },
    }


def _truncated() -> dict:
    return {
        "name": "truncated",
        "description": "Short bodies, wrong Content-Length and server errors.",
        "resources": {
            "/trunc": {
                "content": TRUNC_BODY_V1,
                "etag": TRUNC_ETAG,
                "cache_control": "max-age=2",
                "steps": [
                    {},
                    {
                        "content": TRUNC_BODY_V2,
                        "etag": TRUNC_ETAG_V2,
                        "truncate": 5,
                    },
                    {},
                ],
            },
            "/badlen": {
                "content": BADLEN_BODY_V1,
                "etag": BADLEN_ETAG,
                "cache_control": "max-age=2",
                "steps": [
                    {},
                    {
                        "content": BADLEN_BODY_V2,
                        "etag": BADLEN_ETAG_V2,
                        "conditional": "raw_bad_length",
                    },
                    {},
                ],
            },
            "/extra": {
                "content": EXTRA_BODY_V1,
                "etag": EXTRA_ETAG,
                "cache_control": "max-age=2",
                "steps": [
                    {},
                    {
                        "content": EXTRA_BODY_V2,
                        "etag": EXTRA_ETAG_V2,
                        "conditional": "raw_extra_body",
                    },
                    {},
                ],
            },
            "/error": {
                "content": ERROR_BODY_V1,
                "etag": ERROR_ETAG,
                "cache_control": "max-age=2",
                "steps": [
                    {},
                    {"error": 500},
                    {},
                ],
            },
        },
    }


def _interrupt() -> dict:
    mutate = {
        "content": MUTATE_V1,
        "etag": '"mutate-v1"',
        "cache_control": "max-age=2",
        "steps": [
            {},
            {"content": MUTATE_V2, "etag": '"mutate-v2"', "cache_control": "max-age=45"},
            {},
        ],
    }
    return {
        "name": "interrupt",
        "description": "Kill the client during or after the SQLite transaction.",
        "resources": {
            "/mutate": mutate,
            "/mutate2": dict(mutate),
        },
    }


def _large() -> dict:
    return {
        "name": "large",
        "description": "Deterministic generated asset with a long max-age.",
        "resources": {
            "/asset": {
                "content_b64": base64.b64encode(LARGE_BODY.encode("utf-8")).decode("ascii"),
                "etag": '"large-v1"',
                "cache_control": "max-age=125",
                "content_type": "text/plain; charset=utf-8",
                "headers": {"X-Asset-Id": "asset-0001"},
                "steps": [],
            }
        },
    }


def build_scenarios() -> dict[str, dict]:
    return {
        name: factory()
        for name, factory in (
            ("language", _language),
            ("default", _default),
            ("freshness", _freshness),
            ("update", _update),
            ("replace", _replace),
            ("vary", _vary),
            ("nostore", _nostore),
            ("bad304", _bad304),
            ("truncated", _truncated),
            ("interrupt", _interrupt),
            ("large", _large),
        )
    }


def write_scenarios(output_dir: str | Path | None = None) -> list[Path]:
    output_dir = Path(output_dir) if output_dir else SCENARIO_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, data in build_scenarios().items():
        path = output_dir / f"{name}.json"
        path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        written.append(path)
    return written
