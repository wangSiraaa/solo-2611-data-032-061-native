"""Restricted COBOL copybook compiler and Arrow/Parquet converter."""
from .arrowio import convert_file, record_schema, records_to_table, rejects_to_table
from .compiler import compile_copybook
from .decoder import RecordOK, RecordReject, decode_record, iter_records
from .explain import explain_layout, explain_record, layout_json
from .model import CopybookError, Encoding, EncodingError, Layout

__all__ = [
    "compile_copybook", "convert_file", "record_schema", "records_to_table",
    "rejects_to_table", "decode_record", "iter_records", "explain_layout",
    "explain_record", "layout_json", "CopybookError", "Encoding",
    "EncodingError", "Layout", "RecordOK", "RecordReject",
]
