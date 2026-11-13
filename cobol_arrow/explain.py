"""Human-readable layout and per-record field explanations."""
from __future__ import annotations

import json
import re
from pathlib import Path

from .decoder import RecordOK, RecordReject, decode_value, iter_records
from .model import Encoding, Field, Layout


class UserError(Exception):
    """A command-line usage problem (bad record number, bad field path, ...)."""


# ---------------------------------------------------------------------------
# explain-layout
# ---------------------------------------------------------------------------

def _storage_text(field: Field, odo: Field | None) -> str:
    parts = []
    if field.is_group:
        parts.append("group")
    else:
        pic = field.pic
        if pic.kind == "x":
            parts.append(f"{pic.text} text (charset from encoding declaration)")
        elif pic.kind == "display9":
            parts.append(f"{pic.text} unsigned DISPLAY integer")
        else:
            parts.append(f"{pic.text} COMP-3 packed decimal, scale {pic.scale}")
    if field.occurs is not None:
        if field.occurs.is_dynamic:
            dep = field.occurs.dep_field.path_string
            parts.append(f"OCCURS 0 TO {field.occurs.maximum} DEPENDING ON {dep}")
        else:
            parts.append(f"OCCURS {field.occurs.maximum}")
    if odo is not None and odo.occurs.dep_field is field:
        parts.append("<- ODO counter>")
    return "; ".join(parts)


def explain_layout(layout: Layout) -> str:
    root = layout.root
    lines = [f"explain-layout: {root.name}  (source: {layout.source})"]
    if layout.odo is not None:
        occ = layout.odo.occurs
        lines.append(f"  fixed region : {layout.fixed_length} byte(s)")
        lines.append(f"  dynamic tail : {layout.odo.path_string} OCCURS 0 TO {occ.maximum} "
                     f"DEPENDING ON {occ.dep_field.path_string}, "
                     f"{layout.odo.elem_length} byte(s)/item -> 0..{layout.odo.max_length} byte(s)")
        lines.append(f"  record data  : {layout.fixed_length}..{layout.max_length} byte(s) "
                     "(on disk, add the length prefix)")
    else:
        lines.append(f"  fixed record : {layout.fixed_length} byte(s) (no dynamic area)")
    lines.append("")
    lines.append(f"{'offset':>7} {'length':>8} {'lvl':>3}  {'path':<44} storage")
    for f in layout.fields:
        if f is layout.root:
            continue
        owner = f.parent
        while owner is not None and owner.occurs is None:
            owner = owner.parent
        if owner is not None:  # inside an array element: offset relative to the element
            offset = f"+{f.offset - owner.offset}"
        else:
            offset = str(f.offset)
        if f.occurs is not None and f.occurs.is_dynamic:
            length = f"0..{f.max_length}"
        elif f.occurs is not None:
            length = f"{f.occurs.maximum}x{f.elem_length}"
        else:
            length = str(f.elem_length)
        name = f.name if f.name is not None else "FILLER"
        lines.append(f"{offset:>7} {length:>8} {f.level:>3}  {f.path_string or name:<44} "
                     f"{_storage_text(f, layout.odo)}")
    return "\n".join(lines)


def _field_json(field: Field) -> dict:
    node = {
        "name": field.name,
        "level": field.level,
        "kind": "group" if field.is_group else field.pic.kind,
        "offset": field.offset,
        "elem_length": field.elem_length,
        "fixed_length": field.fixed_length,
        "max_length": field.max_length,
        "line": field.line,
    }
    if field.pic is not None:
        node["picture"] = field.pic.text
        if field.pic.kind != "x":
            node["digits"] = field.pic.digits
            node["scale"] = field.pic.scale
    if field.occurs is not None:
        occ = {"kind": "odo" if field.occurs.is_dynamic else "fixed",
               "min": field.occurs.minimum, "max": field.occurs.maximum}
        if field.occurs.is_dynamic:
            occ["depending_on"] = field.occurs.dep_field.path_string
        node["occurs"] = occ
    if field.children:
        node["children"] = [_field_json(c) for c in field.children]
    return node


def layout_json(layout: Layout) -> str:
    doc = {
        "record": layout.root.name,
        "source": layout.source,
        "fixed_length": layout.fixed_length,
        "max_length": layout.max_length,
        "odo": None,
        "fields": [_field_json(c) for c in layout.root.children],
        "paths": {},
    }
    if layout.odo is not None:
        doc["odo"] = {
            "path": layout.odo.path_string,
            "count_path": layout.odo.occurs.dep_field.path_string,
            "max": layout.odo.occurs.maximum,
            "item_length": layout.odo.elem_length,
            "max_length": layout.odo.max_length,
        }
    for path, field in layout.named.items():
        doc["paths"][".".join(path)] = {
            "offset": field.offset,
            "length": field.elem_length,
            "kind": "group" if field.is_group else field.pic.kind,
            "occurs": None if field.occurs is None else (
                {"kind": "odo", "max": field.occurs.maximum}
                if field.occurs.is_dynamic else
                {"kind": "fixed", "count": field.occurs.maximum}),
        }
    return json.dumps(doc, indent=2)


# ---------------------------------------------------------------------------
# explain-record
# ---------------------------------------------------------------------------

_SEGMENT = re.compile(r"^([A-Za-z0-9-]+)(?:\[(\d+)\])?$")


def _parse_field_path(text: str) -> list[tuple[str, int | None]]:
    segments = []
    for raw in text.strip().split("."):
        match = _SEGMENT.match(raw)
        if not match:
            raise UserError(f"invalid field path segment {raw!r} (use NAME or NAME[k], "
                            "segments joined by '.')")
        name, sub = match.groups()
        segments.append((name.upper(), int(sub) if sub else None))
    return segments


def _resolve(layout: Layout, segments: list[tuple[str, int | None]],
             values: dict) -> tuple[Field, int, int, str, object]:
    """Resolve a path to (field, record-data offset, length, display path, value)."""
    node = layout.root
    shift = 0  # byte displacement from array subscripts, inherited by descendants
    display = node.name
    scope: tuple[str, ...] = (node.name,)
    vals: object = values
    if segments and segments[0][0] == node.name:
        segments = segments[1:]
    if not segments:
        total = layout.fixed_length
        if layout.odo is not None:
            total += _odo_actual_count(layout, values) * layout.odo.elem_length
        return node, 0, total, display, vals
    for name, sub in segments:
        key = scope + (name,)
        child = layout.named.get(key)
        if child is None:
            options = sorted(".".join(k) for k in layout.named if k[:1] == (layout.root.name,))
            raise UserError(f"no field {name!r} under {'.'.join(scope)}; "
                            f"valid paths include: {', '.join(options[:12])}")
        if vals is None:
            raise UserError(f"cannot descend into {display}: it is an array; "
                            "give a subscript like NAME[k]")
        node = child
        scope = key
        display = f"{display}.{name}"
        base = node.offset + shift
        length = node.elem_length
        if node.occurs is not None:
            actual = (len(vals[name]) if node.occurs.is_dynamic else node.occurs.maximum)
            if sub is None:
                length = actual * node.elem_length
                display += "[*]"
                vals = vals[name]
            else:
                if not 1 <= sub <= actual:
                    raise UserError(f"subscript [{sub}] out of range for {display} "
                                    f"(this record has {actual} occurrence(s))")
                shift += (sub - 1) * node.elem_length
                base = node.offset + shift
                length = node.elem_length
                display += f"[{sub}]"
                vals = vals[name][sub - 1]
        elif sub is not None:
            raise UserError(f"{display} is not an array; remove the [{sub}] subscript")
        else:
            vals = vals[name]
    return node, base, length, display, vals


def _odo_actual_count(layout: Layout, values: dict) -> int:
    node = values
    for name in layout.odo.path[1:-1]:
        node = node[name]
    return len(node[layout.odo.path[-1]])


def _decode_explanation(field: Field, raw: bytes, enc: Encoding) -> tuple[str, str]:
    """Return (decode steps, rendered value) for one field's raw bytes."""
    if field.is_group:
        return f"group of {len(field.children)} field(s), {len(raw)} byte(s)", "(group)"
    pic = field.pic
    if pic.kind == "x":
        value = decode_value(field, raw, enc, field.path_string)
        return f"{enc.character_encoding} text, {len(raw)} byte(s)", repr(value)
    if pic.kind == "display9":
        value = decode_value(field, raw, enc, field.path_string)
        return (f"unsigned DISPLAY integer, {pic.digits} {enc.character_encoding} "
                f"digit byte(s) {raw.decode(enc.character_encoding)!r}", str(value))
    nibbles = []
    for byte in raw:
        nibbles.extend((byte >> 4, byte & 0x0F))
    sign = nibbles[-1]
    value = decode_value(field, raw, enc, field.path_string)
    sign_txt = "negative" if sign == enc.negative_sign else "positive"
    steps = (f"COMP-3 packed decimal {pic.text}: digit nibbles "
             f"{' '.join(format(n, 'X') for n in nibbles[:-1])} + sign nibble "
             f"{sign:X} ({sign_txt}), scale {pic.scale}")
    return steps, str(value)


def explain_record(layout: Layout, enc: Encoding, bin_path: str | Path,
                   record_index: int, field_path: str) -> str:
    if record_index < 0:
        raise UserError("record number must be >= 0 (record numbers are 0-based)")
    segments = _parse_field_path(field_path)
    found = None
    size = Path(bin_path).stat().st_size
    with open(bin_path, "rb") as fh:
        for item in iter_records(layout, enc, fh, size):
            if item.record_index == record_index:
                found = item
                break
            if item.record_index > record_index:
                break
    if found is None:
        raise UserError(f"record #{record_index} not found (file holds fewer records)")
    if isinstance(found, RecordReject):
        where = f" at {found.field_path}" if found.field_path else ""
        return (f"record #{record_index} at file offset {found.file_offset} was rejected"
                f"{where}: [{found.code}] {found.message}")
    data_start = found.file_offset + enc.prefix_bytes
    data_len = found.total_bytes - enc.prefix_bytes
    with open(bin_path, "rb") as fh:
        fh.seek(data_start)
        data = fh.read(data_len)
    node, base, length, display, value = _resolve(layout, segments, found.values)
    raw = data[base:base + length]
    lines = [
        f"record #{record_index}: file offset {found.file_offset}, on-disk "
        f"{found.total_bytes} byte(s) ({enc.prefix_bytes} prefix + {data_len} data)",
        f"field  : {display}",
        f"bytes  : file [{data_start + base}, {data_start + base + length}) "
        f"= record-data offset {base}, length {length}",
        f"hex    : {raw.hex()}",
    ]
    if node.occurs is not None and display.endswith("[*]"):
        count = length // node.elem_length
        lines.append(f"decode : array of {count} item(s) x {node.elem_length} byte(s)")
        lines.append(f"value  : {json.dumps(value, default=str, ensure_ascii=False)}")
    elif node.is_group:
        steps, _ = _decode_explanation(node, raw, enc)
        lines.append(f"decode : {steps}")
        lines.append(f"value  : {json.dumps(value, default=str, ensure_ascii=False)}")
    else:
        steps, rendered = _decode_explanation(node, raw, enc)
        lines.append(f"decode : {steps}")
        lines.append(f"value  : {rendered}")
    return "\n".join(lines)
