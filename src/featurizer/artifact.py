"""JSON artifact serialization and frozen-structure validation.

The artifact is plain JSON data: no pickle, no executable code. It contains
every statistic, the stable vocabulary, the input type contract, the exact
output column layout and all policies, plus a sha256 content digest that
identifies the fit run.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

from .contract import (
    ARTIFACT_KIND,
    ARTIFACT_VERSION,
    CATEGORY_CODE_COLUMN,
    CATEGORY_KNOWN_COLUMN,
    CATEGORY_MISSING_COLUMN,
    CODE_NULL,
    CODE_UNKNOWN,
    EXTRA_FIELDS_POLICY,
    INDICATOR_SUFFIX,
    NUMERIC_SCALED_SUFFIX,
    VOCAB_INDEX_OFFSET,
)


class ArtifactError(ValueError):
    """Raised when an artifact file is structurally or numerically invalid."""


def canonical_payload(payload: dict[str, Any]) -> bytes:
    """Canonical JSON bytes used for the digest (digest field excluded)."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def compute_digest(payload: dict[str, Any]) -> str:
    # The digest identifies the fitted content (statistics, vocabulary,
    # layouts and policies). It excludes the digest itself and the training
    # file *path*, which is audit metadata: moving or re-ordering the same
    # training data must not change the artifact's column identity.
    body = {k: v for k, v in payload.items() if k != "digest"}
    if isinstance(body.get("training"), dict) and "source" in body["training"]:
        body["training"] = {k: v for k, v in body["training"].items() if k != "source"}
    return hashlib.sha256(canonical_payload(body)).hexdigest()


def save_artifact(payload: dict[str, Any], path: str | Path) -> None:
    payload = dict(payload)
    payload["digest"] = compute_digest(payload)
    Path(path).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise ArtifactError(message)


def _check_finite_number(value: Any, path: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ArtifactError(f"{path}: expected finite number, got {type(value).__name__}")
    if not math.isfinite(float(value)):
        raise ArtifactError(f"{path}: non-finite parameter")


def validate_artifact(obj: Any) -> dict[str, Any]:
    """Validate the full artifact structure. Returns the same dict if valid."""
    _require(isinstance(obj, dict), "artifact root must be a JSON object")

    for key in (
        "kind",
        "artifact_version",
        "digest",
        "spec",
        "input_schema",
        "policies",
        "numeric_stats",
        "categorical",
        "output_layout",
        "training",
    ):
        _require(key in obj, f"missing top-level field: {key}")

    _require(obj["kind"] == ARTIFACT_KIND, "kind mismatch")
    _require(obj["artifact_version"] == ARTIFACT_VERSION, "artifact_version mismatch")
    _require(isinstance(obj["digest"], str) and len(obj["digest"]) == 64, "bad digest")
    if compute_digest(obj) != obj["digest"]:
        raise ArtifactError("digest mismatch: artifact content was altered after fitting")

    spec = obj["spec"]
    _require(isinstance(spec, dict), "spec must be an object")
    _require(isinstance(spec.get("record_id"), str) and spec["record_id"], "bad spec.record_id")
    _require(
        isinstance(spec.get("numeric"), list)
        and len(spec["numeric"]) == 2
        and all(isinstance(f, str) for f in spec["numeric"])
        and len(set(spec["numeric"])) == 2,
        "spec.numeric must list exactly two distinct field names",
    )
    _require(
        isinstance(spec.get("categorical"), str) and spec["categorical"],
        "bad spec.categorical",
    )
    numeric_fields = spec["numeric"]
    cat_field = spec["categorical"]
    _require(
        cat_field not in numeric_fields and spec["record_id"] not in numeric_fields,
        "spec field names must be distinct",
    )

    input_schema = obj["input_schema"]
    _require(isinstance(input_schema, dict), "input_schema must be an object")
    _require(
        input_schema.get("record_id") == {"type": "string"},
        "input_schema.record_id mismatch",
    )
    for field in numeric_fields:
        _require(
            input_schema.get(field)
            == {"type": ["number", "null"], "boolean_accepted": False},
            f"input_schema.{field} mismatch",
        )
    _require(
        input_schema.get(cat_field)
        == {"type": ["string", "null"], "note": "exact-string vocabulary"},
        "input_schema categorical mismatch",
    )

    policies = obj["policies"]
    _require(
        policies.get("missing_numeric") == "fill_train_mean_with_indicator",
        "bad policies.missing_numeric",
    )
    _require(
        policies.get("constant_column")
        == "scale_to_zero_and_keep_filled_value",
        "bad policies.constant_column",
    )
    _require(
        policies.get("all_missing_column")
        == "fill_zero_scale_zero_indicator_all_ones",
        "bad policies.all_missing_column",
    )
    _require(
        policies.get("unseen_category") == "reserved_code_unknown",
        "bad policies.unseen_category",
    )
    _require(
        policies.get("null_category") == "reserved_code_null",
        "bad policies.null_category",
    )
    _require(
        policies.get("extra_fields") == EXTRA_FIELDS_POLICY,
        "bad policies.extra_fields",
    )

    stats = obj["numeric_stats"]
    _require(isinstance(stats, dict) and set(stats) == set(numeric_fields), "numeric_stats keys mismatch")
    for field in numeric_fields:
        st = stats[field]
        prefix = f"numeric_stats.{field}"
        _require(isinstance(st, dict), f"{prefix} must be an object")
        for key in ("mean", "std", "observed_count", "missing_count", "all_missing", "constant"):
            _require(key in st, f"{prefix}.{key} missing")
        _check_finite_number(st["mean"], f"{prefix}.mean")
        _check_finite_number(st["std"], f"{prefix}.std")
        _require(st["std"] >= 0.0, f"{prefix}.std must be non-negative")
        _require(isinstance(st["observed_count"], int) and st["observed_count"] >= 0, f"{prefix}.observed_count bad")
        _require(isinstance(st["missing_count"], int) and st["missing_count"] >= 0, f"{prefix}.missing_count bad")
        _require(isinstance(st["all_missing"], bool), f"{prefix}.all_missing bad")
        _require(isinstance(st["constant"], bool), f"{prefix}.constant bad")
        if st["all_missing"]:
            _require(st["observed_count"] == 0, f"{prefix}: all_missing implies zero observations")
            _require(st["mean"] == 0.0 and st["std"] == 0.0, f"{prefix}: all-missing stats must be zero")
        if st["constant"] and not st["all_missing"]:
            _require(st["std"] == 0.0, f"{prefix}: constant column must have zero std")

    cat = obj["categorical"]
    _require(isinstance(cat, dict), "categorical must be an object")
    _require(cat.get("field") == cat_field, "categorical.field mismatch")
    vocab = cat.get("vocab")
    _require(
        isinstance(vocab, list) and all(isinstance(v, str) for v in vocab),
        "categorical.vocab must be a list of strings",
    )
    _require(len(vocab) == len(set(vocab)), "categorical.vocab contains duplicates")
    _require(vocab == sorted(vocab), "categorical.vocab is not in stable sorted order")
    _require(
        cat.get("code_null") == CODE_NULL
        and cat.get("code_unknown") == CODE_UNKNOWN
        and cat.get("vocab_index_offset") == VOCAB_INDEX_OFFSET,
        "categorical reserved codes mismatch",
    )
    expected_codes = {v: i + VOCAB_INDEX_OFFSET for i, v in enumerate(vocab)}
    _require(cat.get("codes") == expected_codes, "categorical.codes mismatch")

    layout = obj["output_layout"]
    _require(isinstance(layout, dict), "output_layout must be an object")
    expected_columns: list[str] = []
    expected_dtypes: dict[str, str] = {}
    for field in numeric_fields:
        expected_columns.append(f"{field}{NUMERIC_SCALED_SUFFIX}")
        expected_dtypes[f"{field}{NUMERIC_SCALED_SUFFIX}"] = "float64"
        expected_columns.append(f"{field}{INDICATOR_SUFFIX}")
        expected_dtypes[f"{field}{INDICATOR_SUFFIX}"] = "int8"
    expected_columns += [CATEGORY_MISSING_COLUMN, CATEGORY_KNOWN_COLUMN, CATEGORY_CODE_COLUMN]
    expected_dtypes.update(
        {
            CATEGORY_MISSING_COLUMN: "int8",
            CATEGORY_KNOWN_COLUMN: "int8",
            CATEGORY_CODE_COLUMN: "int64",
        }
    )
    _require(layout.get("columns") == expected_columns, "output_layout.columns mismatch")
    _require(layout.get("dtypes") == expected_dtypes, "output_layout.dtypes mismatch")
    _require(
        isinstance(layout.get("column_order_signature"), str)
        and len(layout["column_order_signature"]) == 16,
        "bad column_order_signature",
    )

    training = obj["training"]
    _require(isinstance(training, dict), "training must be an object")
    _require(isinstance(training.get("row_count"), int) and training["row_count"] >= 0, "bad training.row_count")
    _require(isinstance(training.get("std_ddof"), int) and training["std_ddof"] == 0, "bad training.std_ddof")
    _require(isinstance(training.get("source"), str) and training["source"], "bad training.source")

    return obj


def load_artifact(path: str | Path) -> dict[str, Any]:
    """Read, JSON-parse and fully validate a frozen artifact."""
    text = Path(path).read_text(encoding="utf-8")
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ArtifactError(f"artifact is not valid JSON: {exc}") from exc
    return validate_artifact(obj)
