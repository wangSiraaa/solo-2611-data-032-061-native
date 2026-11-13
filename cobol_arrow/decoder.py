"""Bounded record reader and strict decoder.

Records on disk are a length prefix (declared by the Encoding) followed by
record data.  The prefix is validated against max_record_bytes *before* the
data buffer is allocated/read, so a corrupt prefix can never trigger a huge
allocation.  A record is decoded only after its full byte range is present;
a truncated tail yields one reject and never a partial record.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import BinaryIO, Iterator

from .model import Encoding, Field, Layout


@dataclass
class RecordOK:
    record_index: int
    file_offset: int
    total_bytes: int          # on-disk bytes including the prefix
    values: dict


@dataclass
class RecordReject:
    record_index: int
    file_offset: int
    field_path: str | None    # qualified path, [k] subscripts are 1-based; None = record level
    code: str
    message: str
    record_hex: str


class RecordError(Exception):
    def __init__(self, code: str, message: str, field_path: str | None = None):
        self.code = code
        self.field_path = field_path
        super().__init__(message)


# ---------------------------------------------------------------------------
# Primitive decoders (strict, no float intermediary)
# ---------------------------------------------------------------------------

def decode_text(field: Field, buf: bytes, enc: Encoding, path: str) -> str:
    try:
        return buf.decode(enc.character_encoding)
    except UnicodeDecodeError as exc:
        raise RecordError("BAD_ENCODING",
                          f"{path}: cannot decode {enc.character_encoding}: {exc}", path) from exc


def decode_display9(field: Field, buf: bytes, enc: Encoding, path: str) -> int:
    text = decode_text(field, buf, enc, path)
    if not all("0" <= ch <= "9" for ch in text):
        raise RecordError("BAD_DIGIT",
                          f"{path}: expected {field.pic.digits} DISPLAY digit(s), found {text!r}",
                          path)
    return int(text) if text else 0


def decode_comp3(field: Field, buf: bytes, enc: Encoding, path: str) -> Decimal:
    nibbles: list[int] = []
    for byte in buf:
        nibbles.append(byte >> 4)
        nibbles.append(byte & 0x0F)
    sign = nibbles[-1]
    if sign not in enc.positive_signs and sign != enc.negative_sign:
        allowed = "/".join(format(s, "X") for s in (*enc.positive_signs, enc.negative_sign))
        raise RecordError("BAD_SIGN",
                          f"{path}: sign nibble {sign:X} is not one of {allowed}", path)
    digits: list[int] = []
    for pos, nib in enumerate(nibbles[:-1]):
        if nib > 9:
            raise RecordError("BAD_DIGIT",
                              f"{path}: packed digit nibble {pos} is {nib:X} (> 9)", path)
        digits.append(nib)
    negative = sign == enc.negative_sign
    # exact decimal straight from the nibbles; no float involved
    return Decimal((1 if negative else 0, tuple(digits), -field.pic.scale))


def decode_value(field: Field, buf: bytes, enc: Encoding, path: str):
    kind = field.pic.kind
    if kind == "x":
        return decode_text(field, buf, enc, path)
    if kind == "display9":
        return decode_display9(field, buf, enc, path)
    return decode_comp3(field, buf, enc, path)


# ---------------------------------------------------------------------------
# Record decoding
# ---------------------------------------------------------------------------

def _lookup(values: dict, dep: Field):
    node = values
    for name in dep.path[1:]:  # skip the root name
        node = node[name]
    return node


def _decode_field(field: Field, abs_pos: int, data: bytes, out: dict,
                  enc: Encoding, path: str, root_values: dict, odo_count: int) -> None:
    if field.is_filler:
        if field.is_group:  # unnamed group: hoist its named children
            for child in field.children:
                _decode_field(child, abs_pos + (child.offset - field.offset), data, out,
                              enc, path, root_values, odo_count)
        return
    child_path = f"{path}.{field.name}" if path else field.name
    if field.occurs is not None:
        if field.occurs.is_dynamic:
            dep = field.occurs.dep_field
            dep_val = _lookup(root_values, dep)
            if dep_val != odo_count:
                raise RecordError(
                    "COUNT_MISMATCH",
                    f"{child_path}: count field {dep.path_string} = {dep_val} but the record "
                    f"carries {odo_count} item(s)", child_path)
            n = odo_count
        else:
            n = field.occurs.maximum
        items = []
        out[field.name] = items
        for i in range(n):
            items.append(_decode_node(field, abs_pos + i * field.elem_length, data, enc,
                                      f"{child_path}[{i + 1}]", root_values, odo_count))
    elif field.is_group:
        group: dict = {}
        out[field.name] = group  # link before filling, so a tail ODO sees its counter
        for child in field.children:
            _decode_field(child, abs_pos + (child.offset - field.offset), data, group,
                          enc, child_path, root_values, odo_count)
    else:
        out[field.name] = decode_value(field, data[abs_pos:abs_pos + field.pic.length],
                                       enc, child_path)


def _decode_node(field: Field, abs_pos: int, data: bytes, enc: Encoding,
                 path: str, root_values: dict, odo_count: int):
    if field.is_group:
        out: dict = {}
        for child in field.children:
            _decode_field(child, abs_pos + (child.offset - field.offset), data, out,
                          enc, path, root_values, odo_count)
        return out
    return decode_value(field, data[abs_pos:abs_pos + field.pic.length], enc, path)


def decode_record(layout: Layout, enc: Encoding, data: bytes) -> dict:
    """Decode one record's data bytes (without the length prefix)."""
    if len(data) < layout.fixed_length:
        raise RecordError(
            "SHORT_RECORD",
            f"record data is {len(data)} byte(s), shorter than the fixed layout "
            f"of {layout.fixed_length} byte(s)")
    dynamic = len(data) - layout.fixed_length
    odo_count = 0
    if layout.odo is not None:
        item = layout.odo.elem_length
        maximum = layout.odo.occurs.maximum
        if dynamic % item != 0:
            raise RecordError(
                "LENGTH_MISMATCH",
                f"dynamic tail is {dynamic} byte(s), not a multiple of "
                f"{layout.odo.path_string} item size {item}", layout.odo.path_string)
        odo_count = dynamic // item
        if odo_count > maximum:
            raise RecordError(
                "COUNT_RANGE",
                f"{layout.odo.path_string}: record carries {odo_count} item(s), "
                f"maximum is {maximum}", layout.odo.path_string)
    elif dynamic != 0:
        raise RecordError(
            "LENGTH_MISMATCH",
            f"record data is {len(data)} byte(s) but the fixed layout needs exactly "
            f"{layout.fixed_length}")
    values: dict = {}
    for child in layout.root.children:
        _decode_field(child, child.offset, data, values, enc, layout.root.name,
                      values, odo_count)
    return values


# ---------------------------------------------------------------------------
# Bounded record iteration
# ---------------------------------------------------------------------------

def iter_records(layout: Layout, enc: Encoding, fileobj: BinaryIO,
                 size: int) -> Iterator[RecordOK | RecordReject]:
    """Yield records in file order.  Length-prefix problems and truncation
    cannot be resynchronized, so they end the scan after one reject."""
    offset = 0
    index = 0
    while offset < size:
        prefix = fileobj.read(enc.prefix_bytes)
        if len(prefix) < enc.prefix_bytes:
            yield RecordReject(index, offset, None, "TRUNCATED_PREFIX",
                               f"file ends with {len(prefix)} dangling byte(s); a "
                               f"{enc.prefix_bytes}-byte length prefix was expected",
                               prefix.hex())
            return
        total = int.from_bytes(prefix, enc.prefix_byte_order)
        data_len = total - enc.prefix_bytes if enc.prefix_includes_prefix else total
        on_disk = enc.prefix_bytes + max(data_len, 0)
        if data_len < 0 or on_disk > enc.max_record_bytes:
            yield RecordReject(index, offset, None, "BAD_PREFIX",
                               f"length prefix value {total} implies {on_disk} on-disk byte(s); "
                               f"allowed range is {enc.prefix_bytes}..{enc.max_record_bytes}; "
                               "cannot resynchronize, stopping the scan", prefix.hex())
            return
        data = fileobj.read(data_len)  # bounded: data_len <= max_record_bytes
        if len(data) < data_len:
            yield RecordReject(index, offset, None, "TRUNCATED",
                               f"record declares {data_len} data byte(s) but only "
                               f"{len(data)} remain in the file", (prefix + data).hex())
            return
        try:
            values = decode_record(layout, enc, data)
        except RecordError as exc:
            yield RecordReject(index, offset, exc.field_path, exc.code, str(exc), data.hex())
        else:
            yield RecordOK(index, offset, on_disk, values)
        offset += on_disk
        index += 1
