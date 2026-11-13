"""把 CSV 冻结版本装入 SQLite。

CSV 字面量 ``\\N`` 是真正的 SQL NULL；空字符串是另一种真实维度值，必须原样保留。
金额按整数分存取，避免浮点误差。record_id 为唯一键。
"""
from __future__ import annotations

import csv
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

NULL_MARKER_DEFAULT = "\\N"


@dataclass(frozen=True)
class ColumnSpec:
    null_marker: str
    default_sort: tuple[str, ...]
    unique_key: str


def load_column_spec(path: Path) -> ColumnSpec:
    raw = json.loads(path.read_text(encoding="utf-8"))
    sort = raw.get("default_sort", ["business_date", "record_id"])
    if not isinstance(sort, list) or not sort:
        raise ValueError("default_sort must be a non-empty list")
    allowed = {"business_date", "record_id", "region", "category", "quantity", "amount_cents"}
    for column in sort:
        if column not in allowed:
            raise ValueError(f"unsupported sort column {column!r}")
    return ColumnSpec(
        null_marker=raw.get("null_marker", NULL_MARKER_DEFAULT),
        default_sort=tuple(sort),
        unique_key=raw.get("unique_key", "record_id"),
    )


def _convert(value: str, null_marker: str) -> str | None:
    return None if value == null_marker else value


def build_database(csv_path: Path, columns_path: Path, db_path: Path) -> ColumnSpec:
    spec = load_column_spec(columns_path)
    if db_path.exists():
        db_path.unlink()
    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(
            """
            PRAGMA journal_mode = MEMORY;
            CREATE TABLE transactions (
                record_id      TEXT PRIMARY KEY,
                business_date  TEXT NOT NULL,
                region         TEXT,
                category       TEXT,
                quantity       INTEGER NOT NULL,
                amount_cents   INTEGER NOT NULL
            );
            CREATE INDEX idx_tx_date ON transactions (business_date);
            CREATE INDEX idx_tx_region ON transactions (region);
            CREATE INDEX idx_tx_category ON transactions (category);
            """
        )
        rows: list[tuple] = []
        with csv_path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            expected = {"record_id", "business_date", "region", "category", "quantity", "amount_cents"}
            missing = expected - set(reader.fieldnames or [])
            if missing:
                raise ValueError(f"CSV missing columns: {sorted(missing)}")
            for line_no, raw in enumerate(reader, start=2):
                try:
                    rows.append(
                        (
                            raw["record_id"],
                            raw["business_date"],
                            _convert(raw["region"], spec.null_marker),
                            _convert(raw["category"], spec.null_marker),
                            int(raw["quantity"]),
                            int(raw["amount_cents"]),
                        )
                    )
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"bad row at line {line_no}: {exc}") from exc
        connection.executemany(
            "INSERT INTO transactions VALUES (?, ?, ?, ?, ?, ?)", rows
        )
        connection.commit()

        # 唯一键必须真的唯一，否则分页裁决不可靠。
        dupes = connection.execute(
            "SELECT record_id FROM transactions GROUP BY record_id HAVING COUNT(*) > 1"
        ).fetchall()
        if dupes:
            raise ValueError(f"duplicate record_id values: {[d[0] for d in dupes][:10]}")
        connection.commit()
    finally:
        connection.close()
    return spec
