"""fit: estimate statistics from training data and freeze one JSON artifact.

Only this command reads training data. All later transformations load the
frozen artifact and never re-estimate statistics or extend the vocabulary.

Numeric policy
--------------
* mean and population standard deviation (ddof=0) are computed from the
  training non-missing values only;
* missing values are filled with the training mean at transform time and an
  indicator column records the fill;
* a constant column keeps zero variance explicitly (std=0 -> scaled value 0);
* an all-missing column has a recomputable deterministic strategy:
  mean=0, std=0, every value fills to 0.0 and its indicator is 1.

Categorical policy
------------------
* vocabulary = sorted set of exact non-null training strings, so process and
  training-row order cannot change column identity;
* null and unseen values use reserved integer codes that can never collide
  with a real category (even one literally named "__UNKNOWN__" or "null").
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any

from . import contract
from .artifact import compute_digest, save_artifact
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
    RecordError,
    VOCAB_INDEX_OFFSET,
)


def read_jsonl(path: str | Path) -> list[Any]:
    records: list[Any] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no}: invalid JSON: {exc}") from exc
    return records


def _population_std(values: list[float], mean: float) -> float:
    # ddof=0 population standard deviation, computed in plain Python so the
    # fit path has no dependency on the batch path's NumPy implementation.
    variance = sum((v - mean) ** 2 for v in values) / len(values)
    # Negative float noise cannot occur for a sum of squares, but guard it.
    return math.sqrt(max(variance, 0.0))


def fit_artifact(train_path: str | Path, spec_path: str | Path) -> dict[str, Any]:
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    record_id_field = spec["record_id"]
    numeric_fields = list(spec["numeric"])
    categorical_field = spec["categorical"]

    records = read_jsonl(train_path)
    observed: dict[str, list[float]] = {f: [] for f in numeric_fields}
    missing_count: dict[str, int] = {f: 0 for f in numeric_fields}
    categories: set[str] = set()

    for row_index, record in enumerate(records, start=1):
        try:
            contract.validate_raw_record(
                record,
                record_id_field=record_id_field,
                numeric_fields=numeric_fields,
                categorical_field=categorical_field,
            )
        except RecordError as exc:
            raise RecordError(
                exc.path, f"train row {row_index}: {exc.message}"
            ) from exc

        for field in numeric_fields:
            value = record[field]
            if value is None:
                missing_count[field] += 1
            else:
                observed[field].append(float(value))

        cat_value = record[categorical_field]
        if cat_value is not None:
            categories.add(cat_value)

    numeric_stats: dict[str, dict[str, Any]] = {}
    for field in numeric_fields:
        values = observed[field]
        if not values:
            # Recomputable all-missing strategy: no magic mean, no division.
            numeric_stats[field] = {
                "mean": 0.0,
                "std": 0.0,
                "observed_count": 0,
                "missing_count": missing_count[field],
                "all_missing": True,
                "constant": True,
            }
            continue
        mean = sum(values) / len(values)
        if not math.isfinite(mean):
            raise RecordError(field, "training mean is not finite")
        std = _population_std(values, mean)
        if not math.isfinite(std):
            raise RecordError(field, "training std is not finite")
        numeric_stats[field] = {
            "mean": mean,
            "std": std,
            "observed_count": len(values),
            "missing_count": missing_count[field],
            "all_missing": False,
            "constant": std == 0.0,
        }

    vocab = sorted(categories)
    codes = {value: index + VOCAB_INDEX_OFFSET for index, value in enumerate(vocab)}

    columns: list[str] = []
    dtypes: dict[str, str] = {}
    for field in numeric_fields:
        scaled = f"{field}{NUMERIC_SCALED_SUFFIX}"
        indicator = f"{field}{INDICATOR_SUFFIX}"
        columns.extend([scaled, indicator])
        dtypes[scaled] = "float64"
        dtypes[indicator] = "int8"
    columns += [CATEGORY_MISSING_COLUMN, CATEGORY_KNOWN_COLUMN, CATEGORY_CODE_COLUMN]
    dtypes.update(
        {
            CATEGORY_MISSING_COLUMN: "int8",
            CATEGORY_KNOWN_COLUMN: "int8",
            CATEGORY_CODE_COLUMN: "int64",
        }
    )
    column_signature = hashlib.sha256(
        json.dumps(columns, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]

    artifact = {
        "kind": ARTIFACT_KIND,
        "artifact_version": ARTIFACT_VERSION,
        "spec": {
            "record_id": record_id_field,
            "numeric": numeric_fields,
            "categorical": categorical_field,
        },
        "input_schema": {
            record_id_field: {"type": "string"},
            **{
                field: {"type": ["number", "null"], "boolean_accepted": False}
                for field in numeric_fields
            },
            categorical_field: {
                "type": ["string", "null"],
                "note": "exact-string vocabulary",
            },
        },
        "policies": {
            "missing_numeric": "fill_train_mean_with_indicator",
            "constant_column": "scale_to_zero_and_keep_filled_value",
            "all_missing_column": "fill_zero_scale_zero_indicator_all_ones",
            "unseen_category": "reserved_code_unknown",
            "null_category": "reserved_code_null",
            "extra_fields": EXTRA_FIELDS_POLICY,
        },
        "numeric_stats": numeric_stats,
        "categorical": {
            "field": categorical_field,
            "vocab": vocab,
            "codes": codes,
            "code_null": CODE_NULL,
            "code_unknown": CODE_UNKNOWN,
            "vocab_index_offset": VOCAB_INDEX_OFFSET,
        },
        "output_layout": {
            "columns": columns,
            "dtypes": dtypes,
            "column_order_signature": column_signature,
        },
        "training": {
            "row_count": len(records),
            "std_ddof": 0,
            "source": str(train_path),
        },
    }
    return artifact


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fit and freeze the featurizer artifact")
    parser.add_argument("--train", required=True, help="training JSONL path")
    parser.add_argument("--spec", required=True, help="spec.json path")
    parser.add_argument("--out", required=True, help="output artifact JSON path")
    args = parser.parse_args(argv)

    artifact = fit_artifact(args.train, args.spec)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    save_artifact(artifact, out_path)

    digest = compute_digest(artifact)
    print(
        json.dumps(
            {
                "status": "fitted",
                "artifact": str(out_path),
                "digest": digest,
                "training_rows": artifact["training"]["row_count"],
                "vocab_size": len(artifact["categorical"]["vocab"]),
                "columns": artifact["output_layout"]["columns"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
