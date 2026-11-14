"""batch: offline NumPy columnar transformation against a frozen artifact.

The whole input file is validated first; only when every record is legal do
we build NumPy columns and vectorize the math. This module never imports
``serve`` and never calls a per-record transform function: the columnar
implementation is independent and shares only the artifact + validation
contract.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np

from . import contract
from .artifact import load_artifact
from .contract import (
    CATEGORY_CODE_COLUMN,
    CATEGORY_KNOWN_COLUMN,
    CATEGORY_MISSING_COLUMN,
    INDICATOR_SUFFIX,
    NUMERIC_SCALED_SUFFIX,
    RecordError,
)


def transform_columns(
    records: list[dict[str, Any]], artifact: dict[str, Any]
) -> dict[str, np.ndarray]:
    """Validate all records, then return one NumPy array per output column."""
    spec = artifact["spec"]
    rid_field = spec["record_id"]
    numeric_fields = spec["numeric"]
    cat_field = spec["categorical"]

    # Up-front full validation: a single bad record aborts the whole batch
    # before any feature buffer is allocated.
    for row_index, record in enumerate(records, start=1):
        try:
            contract.validate_raw_record(
                record,
                record_id_field=rid_field,
                numeric_fields=numeric_fields,
                categorical_field=cat_field,
            )
        except RecordError as exc:
            raise RecordError(exc.path, f"batch row {row_index}: {exc.message}") from exc

    n_rows = len(records)
    columns: dict[str, np.ndarray] = {}

    for field in numeric_fields:
        stats = artifact["numeric_stats"][field]
        mean = float(stats["mean"])
        std = float(stats["std"])

        # None becomes NaN purely as a vectorized missing marker; non-finite
        # user values can never reach here because validation rejected them.
        raw = np.asarray(
            [np.nan if record[field] is None else float(record[field]) for record in records],
            dtype=np.float64,
        )
        missing = np.isnan(raw)
        filled = np.where(missing, mean, raw)
        if std > 0.0:
            scaled = (filled - mean) / std
        else:
            # Constant and all-missing columns: deterministic zero output,
            # no division by zero. The indicator carries the real signal.
            scaled = np.zeros(n_rows, dtype=np.float64)

        columns[f"{field}{NUMERIC_SCALED_SUFFIX}"] = scaled.astype(np.float64, copy=False)
        columns[f"{field}{INDICATOR_SUFFIX}"] = missing.astype(np.int8)

    cat_cfg = artifact["categorical"]
    code_map = cat_cfg["codes"]
    code_null = cat_cfg["code_null"]
    code_unknown = cat_cfg["code_unknown"]

    cat_codes = np.empty(n_rows, dtype=np.int64)
    for i, record in enumerate(records):
        value = record[cat_field]
        if value is None:
            cat_codes[i] = code_null
        else:
            # Exact-string lookup; unseen -> reserved unknown code. A literal
            # "__UNKNOWN__" string is just a normal vocab entry when trained.
            cat_codes[i] = code_map.get(value, code_unknown)

    columns[CATEGORY_MISSING_COLUMN] = (cat_codes == code_null).astype(np.int8)
    columns[CATEGORY_KNOWN_COLUMN] = (
        cat_codes >= cat_cfg["vocab_index_offset"]
    ).astype(np.int8)
    columns[CATEGORY_CODE_COLUMN] = cat_codes

    # Reorder strictly to the frozen layout and sanity-check dtypes.
    layout = artifact["output_layout"]
    ordered = {name: columns[name] for name in layout["columns"]}
    for name, dtype in layout["dtypes"].items():
        if ordered[name].dtype != np.dtype(dtype):
            raise RuntimeError(f"internal dtype mismatch for {name}")
    return ordered


def rows_to_jsonl(
    records: list[dict[str, Any]],
    columns: dict[str, np.ndarray],
    artifact: dict[str, Any],
) -> list[str]:
    layout = artifact["output_layout"]["columns"]
    digest = artifact["digest"]
    rid_field = artifact["spec"]["record_id"]
    lines: list[str] = []
    layout_columns = artifact["output_layout"]["columns"]
    for i, record in enumerate(records):
        features = {name: columns[name][i].item() for name in layout_columns}
        envelope = {
            "record_id": record[rid_field],
            "artifact_digest": digest,
            "columns": layout_columns,
            "features": features,
        }
        lines.append(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")))
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Batch NumPy featurization")
    parser.add_argument("--artifact", required=True)
    parser.add_argument("--input", required=True, help="input JSONL path")
    parser.add_argument("--output", required=True, help="output JSONL path")
    args = parser.parse_args(argv)

    try:
        artifact = load_artifact(args.artifact)
        records = []
        with Path(args.input).open("r", encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise RecordError("", f"{args.input}:{line_no}: invalid JSON: {exc}")
        columns = transform_columns(records, artifact)
    except RecordError as exc:
        print(
            json.dumps(
                {"status": "rejected", "error": {"path": exc.path, "message": exc.message}},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2

    lines = rows_to_jsonl(records, columns, artifact)
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

    print(
        json.dumps(
            {
                "status": "ok",
                "artifact_digest": artifact["digest"],
                "rows": len(records),
                "columns": artifact["output_layout"]["columns"],
                "output": str(out_path),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
