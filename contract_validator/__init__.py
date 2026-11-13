#!/usr/bin/env python3
"""Local JSON Schema-subset contract validator.

Implements, using only the Python 3.11 standard library, a deliberately small
JSON Schema subset with a clear static contract checker and an error-locating
instance validator.  See README.md for the full boundary specification.

Public modules:
    contract_check  -- schema compilation / static rejection (check-schema)
    instance_check  -- line-level JSONL validation (validate)
    report_json     -- canonical JSON rendering and rejected-line reports
    generate        -- synthetic contract + JSONL corpus generation
    verify          -- end-to-end self-check / conformance suite
"""

from __future__ import annotations

__all__ = [
    "contract_check",
    "instance_check",
    "report_json",
]
