"""Data model shared by the restricted copybook compiler and the record decoder."""
from __future__ import annotations

import codecs
import json
from dataclasses import dataclass, field as dc_field
from pathlib import Path


class CopybookError(Exception):
    """Compile-time rejection carrying a 1-based line/column position."""

    def __init__(self, message: str, line: int, col: int, source: str | None = None):
        self.message = message
        self.line = line
        self.col = col
        self.source = source
        super().__init__(f"{source or '<copybook>'}:{line}:{col}: {message}")


class EncodingError(ValueError):
    """Raised when an encoding.json declaration is invalid."""


def _sign_nibble(value) -> int:
    if not isinstance(value, str) or len(value) != 1 or value.upper() not in "ABCDEF":
        raise EncodingError(f"packed-decimal sign must be one of A-F, got {value!r}")
    return int(value.upper(), 16)


@dataclass(frozen=True)
class Encoding:
    """Storage conventions, normally loaded from an encoding.json declaration."""

    character_encoding: str = "cp037"
    prefix_bytes: int = 4
    prefix_byte_order: str = "big"
    prefix_includes_prefix: bool = True
    positive_signs: tuple[int, ...] = (0x0C, 0x0F)
    negative_sign: int = 0x0D
    max_record_bytes: int = 4096

    @classmethod
    def default(cls) -> "Encoding":
        return cls()

    @classmethod
    def load(cls, path: str | Path) -> "Encoding":
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise EncodingError(f"cannot read encoding declaration {path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise EncodingError("encoding declaration must be a JSON object")
        try:
            char_enc = raw.get("character_encoding", "cp037")
            codecs.lookup(char_enc)
            prefix = raw.get("record_prefix", {})
            pb = int(prefix.get("bytes", 4))
            order = str(prefix.get("byte_order", "big"))
            inc = bool(prefix.get("includes_prefix", True))
            packed = raw.get("packed_decimal", {})
            pos = tuple(_sign_nibble(s) for s in packed.get("positive_signs", ["C", "F"]))
            neg = _sign_nibble(packed.get("negative_sign", "D"))
            max_rec = int(raw.get("max_record_bytes", 4096))
        except (TypeError, ValueError) as exc:
            raise EncodingError(f"invalid encoding declaration: {exc}") from exc
        if not 1 <= pb <= 8:
            raise EncodingError(f"record_prefix.bytes must be 1..8, got {pb}")
        if order not in ("big", "little"):
            raise EncodingError(f"record_prefix.byte_order must be big|little, got {order!r}")
        if not pos:
            raise EncodingError("packed_decimal.positive_signs must not be empty")
        if neg in pos:
            raise EncodingError("negative sign nibble collides with a positive sign")
        if len(set(pos)) != len(pos):
            raise EncodingError("duplicate positive sign nibbles")
        if max_rec < pb + 1:
            raise EncodingError(f"max_record_bytes must exceed the {pb}-byte prefix, got {max_rec}")
        return cls(char_enc, pb, order, inc, pos, neg, max_rec)

    def sign_name(self, nibble: int) -> str:
        return format(nibble, "X")


@dataclass(frozen=True)
class Pic:
    """A validated PICTURE clause reduced to its storage facts."""

    kind: str          # 'x' | 'display9' | 'comp3'
    length: int        # bytes occupied by one value
    digits: int = 0    # numeric digit count (excluding the sign nibble)
    scale: int = 0     # implied decimal places
    text: str = ""     # normalized picture string, e.g. S9(5)V9(2)


@dataclass
class Occurs:
    minimum: int
    maximum: int
    dep_names: tuple[str, ...] | None = None   # None => fixed OCCURS
    dep_field: "Field | None" = None           # resolved by the compiler
    line: int = 0
    col: int = 0

    @property
    def is_dynamic(self) -> bool:
        return self.dep_names is not None


@dataclass
class Field:
    name: str | None                 # None => FILLER
    level: int
    line: int
    col: int
    pic: Pic | None = None
    occurs: Occurs | None = None
    children: list["Field"] = dc_field(default_factory=list)
    parent: "Field | None" = None
    seq: int = 0                     # lexical order in the copybook
    offset: int = 0                  # absolute byte offset within the record data
    elem_length: int = 0             # bytes of one occurrence
    fixed_length: int = 0            # bytes contributed to the static region
    max_length: int = 0              # maximum bytes occupied

    @property
    def is_group(self) -> bool:
        return self.pic is None

    @property
    def is_filler(self) -> bool:
        return self.name is None

    @property
    def path(self) -> tuple[str, ...]:
        """Qualified path from the root; FILLER segments are skipped."""
        parts = []
        node: Field | None = self
        while node is not None:
            if node.name is not None:
                parts.append(node.name)
            node = node.parent
        return tuple(reversed(parts))

    @property
    def path_string(self) -> str:
        return ".".join(self.path)

    @property
    def is_visible(self) -> bool:
        """True when every ancestor (and self) is named, so the field is addressable."""
        node: Field | None = self
        while node is not None:
            if node.name is None:
                return False
            node = node.parent
        return True

    def under_occurs(self) -> bool:
        node = self.parent
        while node is not None:
            if node.occurs is not None:
                return True
            node = node.parent
        return False


@dataclass
class Layout:
    """Compiled, inspectable result: field tree + lengths + count dependency + paths."""

    root: Field
    source: str
    fields: list[Field]                       # pre-order, includes FILLER
    named: dict[tuple[str, ...], Field]       # qualified path -> field
    odo: Field | None

    @property
    def fixed_length(self) -> int:
        return self.root.fixed_length

    @property
    def max_length(self) -> int:
        return self.root.max_length
