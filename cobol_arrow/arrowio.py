"""Arrow schema derivation and Parquet conversion."""
from __future__ import annotations

import os
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from .decoder import RecordOK, RecordReject, iter_records
from .model import Encoding, Field, Layout

REJECT_SCHEMA = pa.schema([
    pa.field("record_index", pa.int64(), nullable=False),
    pa.field("file_offset", pa.int64(), nullable=False),
    pa.field("field_path", pa.string()),
    pa.field("error_code", pa.string(), nullable=False),
    pa.field("message", pa.string(), nullable=False),
    pa.field("record_hex", pa.string(), nullable=False),
])


def _arrow_type(field: Field) -> pa.DataType:
    if field.is_group:
        return pa.struct(_struct_fields(field))
    pic = field.pic
    if pic.kind == "x":
        return pa.string()
    if pic.kind == "display9":
        # exact either way: int64 up to 18 digits, decimal128 beyond
        return pa.int64() if pic.digits <= 18 else pa.decimal128(pic.digits, 0)
    return pa.decimal128(pic.digits, pic.scale)


def _arrow_field(field: Field) -> pa.Field:
    typ = _arrow_type(field)
    if field.occurs is not None:
        if field.occurs.is_dynamic:
            typ = pa.list_(typ)
        else:
            typ = pa.list_(typ, field.occurs.maximum)  # fixed-size list
    return pa.field(field.name, typ)


def _struct_fields(group: Field) -> list[pa.Field]:
    out: list[pa.Field] = []
    for child in group.children:
        if child.is_filler:
            if child.is_group:  # unnamed group: children are hoisted
                out.extend(_struct_fields(child))
            continue
        out.append(_arrow_field(child))
    return out


def record_schema(layout: Layout) -> pa.Schema:
    return pa.schema(_struct_fields(layout.root))


def records_to_table(layout: Layout, rows: list[dict]) -> pa.Table:
    return pa.Table.from_pylist(rows, schema=record_schema(layout))


def rejects_to_table(rejects: list[RecordReject]) -> pa.Table:
    rows = [{
        "record_index": r.record_index,
        "file_offset": r.file_offset,
        "field_path": r.field_path,
        "error_code": r.code,
        "message": r.message,
        "record_hex": r.record_hex,
    } for r in rejects]
    return pa.Table.from_pylist(rows, schema=REJECT_SCHEMA)


def convert_file(layout: Layout, enc: Encoding, input_path: str | Path,
                 out_path: str | Path, rejects_path: str | Path) -> tuple[int, int]:
    """Convert a binary file to Parquet + a reject Parquet. Returns (ok, rejected)."""
    rows: list[dict] = []
    rejects: list[RecordReject] = []
    size = os.path.getsize(input_path)
    with open(input_path, "rb") as fh:
        for item in iter_records(layout, enc, fh, size):
            if isinstance(item, RecordOK):
                rows.append(item.values)
            else:
                rejects.append(item)
    for target in (Path(out_path), Path(rejects_path)):
        if target.parent and str(target.parent) not in ("", "."):
            target.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(records_to_table(layout, rows), str(out_path))
    pq.write_table(rejects_to_table(rejects), str(rejects_path))
    return len(rows), len(rejects)
