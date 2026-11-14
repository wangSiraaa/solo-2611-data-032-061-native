"""Shared contract for the frozen featurization artifact.

This module only holds *contract* knowledge: policy names, JSON layout of the
artifact, request/response field names and the raw-record validation rules.
It deliberately contains no feature transformation logic. The batch path
(``batch.py``) and the online path (``serve.py``) each implement their own
execution against the same frozen artifact, so neither path calls the other.
"""
from __future__ import annotations

import math
from typing import Any

ARTIFACT_KIND = "frozen-featurizer-artifact"
ARTIFACT_VERSION = 1

# Categorical integer codes. Real category indices start at 2, so the two
# reserved codes can never collide with a real vocabulary entry (including a
# real category that happens to be the literal string "__UNKNOWN__" or "null").
CODE_NULL = 0
CODE_UNKNOWN = 1
VOCAB_INDEX_OFFSET = 2

# Extra-field policy, applied identically by both transformation paths.
EXTRA_FIELDS_POLICY = "reject"

# Output column groups (fixed order, fully specified inside the artifact).
INDICATOR_SUFFIX = "_missing"
NUMERIC_SCALED_SUFFIX = "_scaled"
CATEGORY_CODE_COLUMN = "channel_code"
CATEGORY_KNOWN_COLUMN = "channel_known"
CATEGORY_MISSING_COLUMN = "channel_missing"


class RecordError(ValueError):
    """Validation error carrying a JSON-pointer-like field path."""

    def __init__(self, path: str, message: str):
        super().__init__(f"{path}: {message}" if path else message)
        self.path = path
        self.message = message


def validate_raw_record(
    record: Any,
    *,
    record_id_field: str,
    numeric_fields: list[str],
    categorical_field: str,
) -> None:
    """Validate one raw input record against the frozen input schema.

    Runs before any feature math in both execution paths. Booleans are not
    accepted as numbers, non-finite floats are located precisely, and every
    error carries the offending field path. Nothing here mutates the record.
    """
    if not isinstance(record, dict):
        raise RecordError("", f"record must be a JSON object, got {type(record).__name__}")

    expected = {record_id_field, *numeric_fields, categorical_field}
    extra = sorted(set(record) - expected)
    if extra:
        raise RecordError(extra[0], "unexpected extra field (policy=reject)")

    rid = record.get(record_id_field)
    if rid is None:
        raise RecordError(record_id_field, "missing or null record_id")
    if not isinstance(rid, str) or isinstance(rid, bool):
        raise RecordError(
            record_id_field, f"record_id must be a string, got {type(rid).__name__}"
        )

    for field in numeric_fields:
        if field not in record:
            raise RecordError(field, "missing field")
        value = record[field]
        if value is None:
            continue
        # bool is a subclass of int in Python; reject it explicitly instead
        # of silently coercing True/False into 1/0.
        if isinstance(value, bool):
            raise RecordError(field, "boolean is not a valid numeric value")
        if not isinstance(value, (int, float)):
            raise RecordError(
                field, f"expected number or null, got {type(value).__name__}"
            )
        if not math.isfinite(float(value)):
            raise RecordError(field, "non-finite numeric value (nan/infinity)")

    cat_field = categorical_field
    if cat_field not in record:
        raise RecordError(cat_field, "missing field")
    cat_value = record[cat_field]
    if cat_value is None:
        return
    if not isinstance(cat_value, str):
        raise RecordError(
            cat_field, f"category must be a string or null, got {type(cat_value).__name__}"
        )
