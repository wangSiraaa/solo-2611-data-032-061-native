"""Restricted COBOL copybook compiler.

Supported subset:
  * one 01 root group, nested level numbers 02-49
  * FILLER items (elementary or group)
  * PIC X(n)                       -> fixed text
  * PIC 9(n)                       -> unsigned DISPLAY integer
  * PIC S9(n) [V9(m)] COMP-3       -> signed packed decimal with implied scale
  * fixed OCCURS k TIMES on elementary items or groups
  * exactly one OCCURS 0 TO n TIMES DEPENDING ON <counter>, placed at the
    record tail; the counter must be a visible unsigned integer declared
    before the OCCURS and outside any OCCURS
  * comments ('*' lines and '*>' trailing comments) and entries spanning
    multiple lines

Anything else (REDEFINES, SIGN/overpunch, other USAGEs, VALUE, level 66/77/88,
...) is rejected at compile time with a line/column position.
"""
from __future__ import annotations

from dataclasses import dataclass, field as dc_field

from .model import CopybookError, Field, Layout, Occurs, Pic

MAX_RECORD_LENGTH = 1_000_000


# ---------------------------------------------------------------------------
# Lexer
# ---------------------------------------------------------------------------

@dataclass
class _Tok:
    text: str
    line: int
    col: int
    kind: str  # 'word' | '(' | ')' | '.'


def _lex(text: str, source: str | None) -> list[_Tok]:
    tokens: list[_Tok] = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("*"):
            continue  # blank line or comment line ('*' in the indicator area)
        line = raw
        marker = line.find("*>")
        if marker >= 0:
            line = line[:marker]  # trailing comment
        i = 0
        while i < len(line):
            ch = line[i]
            if ch.isspace():
                i += 1
                continue
            col = i + 1
            if ch in "().":
                tokens.append(_Tok(ch, lineno, col, ch))
                i += 1
            elif ch.isalnum() or ch == "-":
                j = i
                while j < len(line) and (line[j].isalnum() or line[j] == "-"):
                    j += 1
                tokens.append(_Tok(line[i:j].upper(), lineno, col, "word"))
                i = j
            else:
                raise CopybookError(f"unexpected character {ch!r}", lineno, col, source)
    return tokens


# ---------------------------------------------------------------------------
# PICTURE strings
# ---------------------------------------------------------------------------

@dataclass
class _PicSpec:
    kind: str      # 'x' | '9'
    signed: bool
    n: int         # integer positions (X length, or 9s before V)
    m: int         # 9s after V
    has_v: bool


def _parse_picture(text: str, line: int, col: int, source: str | None) -> _PicSpec:
    def err(msg: str, off: int = 0):
        raise CopybookError(msg, line, col + off, source)

    i = 0
    signed = False
    if text.startswith("S"):
        signed = True
        i = 1
    kind = None
    n = m = 0
    has_v = False
    seen = 0
    while i < len(text):
        c = text[i]
        if c == "V":
            if has_v:
                err("duplicate V in PICTURE", i)
            if kind != "9" or seen == 0:
                err("V is only allowed after 9s in a numeric PICTURE", i)
            has_v = True
            i += 1
            continue
        if c in ("X", "9"):
            if kind is None:
                kind = c
            elif kind != c:
                err(f"cannot mix {kind} and {c} in one PICTURE", i)
            j = i
            while j < len(text) and text[j] == c:
                j += 1
            run = j - i
            if j < len(text) and text[j] == "(":
                if run > 1:
                    err(f"write the repeat count as {c}(n) without repeating {c}", i)
                k = text.find(")", j + 1)
                if k < 0:
                    err("unclosed '(' in PICTURE", j)
                num = text[j + 1:k]
                if not num.isdigit() or int(num) == 0:
                    err(f"bad repeat count {num!r} in PICTURE", j)
                run = int(num)
                j = k + 1
            if has_v:
                m += run
            else:
                n += run
            seen += run
            i = j
            continue
        err(f"unsupported PICTURE character {c!r} (subset allows only S, 9, V, X and repeat counts)", i)
    if kind is None:
        err("PICTURE has no X or 9 characters")
    if kind == "X":
        if signed:
            err("S is not allowed in an alphanumeric PICTURE")
        if n > 32767:
            err("alphanumeric length exceeds 32767")
    else:
        if n == 0:
            err("numeric PICTURE needs at least one 9 before V")
        if has_v and m == 0:
            err("V must be followed by at least one 9")
        if n + m > 38:
            err("numeric PICTURE exceeds 38 digits (decimal128 limit)")
    return _PicSpec("x" if kind == "X" else "9", signed, n, m, has_v)


# ---------------------------------------------------------------------------
# Parser: token stream -> raw entries
# ---------------------------------------------------------------------------

_UNSUPPORTED_CLAUSES = {
    "REDEFINES", "RENAMES", "SIGN", "VALUE", "JUSTIFIED", "JUST", "BLANK",
    "SYNCHRONIZED", "SYNC", "EXTERNAL", "GLOBAL", "LIKE", "TYPE",
}
_USAGE_WORDS = {"COMP-3": "comp3", "PACKED-DECIMAL": "comp3", "DISPLAY": "display"}
_UNSUPPORTED_USAGE = {
    "COMP", "COMPUTATIONAL", "COMP-1", "COMP-2", "COMP-4", "COMP-5",
    "COMPUTATIONAL-1", "COMPUTATIONAL-2", "COMPUTATIONAL-3", "COMPUTATIONAL-4",
    "COMPUTATIONAL-5", "BINARY", "INDEX", "POINTER", "PROCEDURE-POINTER",
    "FUNCTION-POINTER", "NATIONAL", "UTF-8", "FLOAT", "DOUBLE",
}
_KEYWORDS = (
    {"PIC", "PICTURE", "USAGE", "OCCURS", "IS", "TO", "TIMES", "DEPENDING", "ON", "OF", "IN"}
    | _UNSUPPORTED_CLAUSES | set(_USAGE_WORDS) | _UNSUPPORTED_USAGE
)


@dataclass
class _Entry:
    level: int
    name: str | None
    line: int
    col: int
    pic_text: str | None = None
    pic_line: int = 0
    pic_col: int = 0
    usage: str | None = None
    usage_line: int = 0
    usage_col: int = 0
    occurs: Occurs | None = None


class _Parser:
    def __init__(self, toks: list[_Tok], source: str | None):
        self.toks = toks
        self.pos = 0
        self.source = source

    def peek(self) -> _Tok | None:
        return self.toks[self.pos] if self.pos < len(self.toks) else None

    def advance(self) -> _Tok:
        tok = self.toks[self.pos]
        self.pos += 1
        return tok

    def error(self, msg: str, tok: _Tok | None):
        if tok is None:
            tok = self.toks[-1] if self.toks else _Tok("", 1, 1, "word")
            msg = f"{msg} (end of input)"
        raise CopybookError(msg, tok.line, tok.col, self.source)

    def expect_word(self, what: str) -> _Tok:
        tok = self.peek()
        if tok is None or tok.kind != "word":
            self.error(f"expected {what}", tok)
        return self.advance()

    def parse(self) -> list[_Entry]:
        entries = []
        while self.pos < len(self.toks):
            entries.append(self.parse_entry())
        return entries

    def parse_entry(self) -> _Entry:
        tok = self.advance()
        if tok.kind != "word" or not tok.text.isdigit():
            self.error(f"expected a level number (01-49), found {tok.text!r}", tok)
        level = int(tok.text)
        if not 1 <= level <= 49:
            raise CopybookError(
                f"level number {tok.text} is not supported (use 01-49; 66/77/88 are outside the subset)",
                tok.line, tok.col, self.source)
        entry = _Entry(level=level, name=None, line=tok.line, col=tok.col)
        nxt = self.peek()
        if nxt is not None and nxt.kind == "word" and nxt.text == "FILLER":
            self.advance()  # explicit FILLER, name stays None
        elif nxt is not None and nxt.kind == "word" and nxt.text not in _KEYWORDS:
            entry.name = self.advance().text
            if entry.name.isdigit():
                raise CopybookError(f"invalid data-name {entry.name!r}", nxt.line, nxt.col, self.source)
        self.parse_clauses(entry)
        return entry

    def parse_clauses(self, entry: _Entry):
        while True:
            tok = self.peek()
            if tok is None:
                self.error("data entry is missing its terminating '.'", None)
            if tok.kind == ".":
                self.advance()
                return
            if tok.kind != "word":
                self.error(f"unexpected {tok.text!r} in data entry", tok)
            word = tok.text
            if word in ("PIC", "PICTURE"):
                if entry.pic_text is not None:
                    self.error("duplicate PICTURE clause", tok)
                self.advance()
                self.parse_picture_clause(entry)
            elif word == "USAGE":
                self.advance()
                nxt = self.peek()
                if nxt is not None and nxt.kind == "word" and nxt.text == "IS":
                    self.advance()
                self.set_usage(entry, self.expect_word("a USAGE name"))
            elif word in _USAGE_WORDS:
                self.set_usage(entry, self.advance())
            elif word in _UNSUPPORTED_USAGE:
                raise CopybookError(
                    f"USAGE {word} is not supported (only COMP-3/PACKED-DECIMAL and DISPLAY are)",
                    tok.line, tok.col, self.source)
            elif word == "OCCURS":
                if entry.occurs is not None:
                    self.error("duplicate OCCURS clause", tok)
                self.parse_occurs(entry)
            elif word in _UNSUPPORTED_CLAUSES:
                raise CopybookError(
                    f"{word} clause is not supported by this restricted compiler",
                    tok.line, tok.col, self.source)
            else:
                self.error(f"unsupported or misplaced clause {word!r}", tok)

    def parse_picture_clause(self, entry: _Entry):
        start = self.expect_word("a picture string")
        if start.text == "IS":
            start = self.expect_word("a picture string")
        parts = [start]
        while True:
            tok = self.peek()
            if tok is None or tok.kind == ".":
                break
            if tok.kind == "word" and tok.text in _KEYWORDS:
                break
            parts.append(self.advance())
        entry.pic_text = "".join(p.text for p in parts)
        entry.pic_line = start.line
        entry.pic_col = start.col

    def set_usage(self, entry: _Entry, tok: _Tok):
        if tok.text not in _USAGE_WORDS:
            if tok.text in _UNSUPPORTED_USAGE:
                raise CopybookError(
                    f"USAGE {tok.text} is not supported (only COMP-3/PACKED-DECIMAL and DISPLAY are)",
                    tok.line, tok.col, self.source)
            self.error(f"expected COMP-3, PACKED-DECIMAL or DISPLAY, found {tok.text!r}", tok)
        usage = _USAGE_WORDS[tok.text]
        if entry.usage is not None and entry.usage != usage:
            raise CopybookError("conflicting USAGE clauses", tok.line, tok.col, self.source)
        entry.usage = usage
        entry.usage_line = tok.line
        entry.usage_col = tok.col

    def parse_occurs(self, entry: _Entry):
        occ_tok = self.advance()  # the OCCURS keyword
        lo_tok = self.expect_word("an OCCURS bound")
        if not lo_tok.text.isdigit():
            self.error(f"OCCURS bound must be a non-negative integer, found {lo_tok.text!r}", lo_tok)
        lo = int(lo_tok.text)
        nxt = self.peek()
        if nxt is not None and nxt.kind == "word" and nxt.text == "TO":
            self.advance()
            hi_tok = self.expect_word("an upper OCCURS bound after TO")
            if not hi_tok.text.isdigit():
                self.error(f"OCCURS upper bound must be an integer, found {hi_tok.text!r}", hi_tok)
            hi = int(hi_tok.text)
            nxt = self.peek()
            if nxt is not None and nxt.kind == "word" and nxt.text == "TIMES":
                self.advance()
            nxt = self.peek()
            if nxt is None or nxt.kind != "word" or nxt.text != "DEPENDING":
                self.error("expected DEPENDING ON after OCCURS lo TO hi [TIMES]", nxt)
            self.advance()
            nxt = self.peek()
            if nxt is not None and nxt.kind == "word" and nxt.text == "ON":
                self.advance()
            dep = [self.expect_word("a DEPENDING ON counter name").text]
            while True:
                nxt = self.peek()
                if nxt is not None and nxt.kind == "word" and nxt.text in ("OF", "IN"):
                    self.advance()
                    dep.append(self.expect_word("a qualifier after OF/IN").text)
                else:
                    break
            entry.occurs = Occurs(lo, hi, tuple(dep), None, occ_tok.line, occ_tok.col)
        else:
            if nxt is not None and nxt.kind == "word" and nxt.text == "TIMES":
                self.advance()
            entry.occurs = Occurs(lo, lo, None, None, occ_tok.line, occ_tok.col)


# ---------------------------------------------------------------------------
# Tree building, validation and layout
# ---------------------------------------------------------------------------

def _finalize_pic(entry: _Entry, source: str | None) -> Pic | None:
    if entry.pic_text is None:
        if entry.usage is not None:
            raise CopybookError("USAGE is only allowed on elementary items",
                                entry.usage_line, entry.usage_col, source)
        return None
    spec = _parse_picture(entry.pic_text, entry.pic_line, entry.pic_col, source)
    if spec.kind == "x":
        if entry.usage == "comp3":
            raise CopybookError("COMP-3 requires a numeric PICTURE, not X",
                                entry.pic_line, entry.pic_col, source)
        return Pic("x", spec.n, text=f"X({spec.n})")
    if entry.usage == "comp3":
        if not spec.signed:
            raise CopybookError(
                "COMP-3 must use a signed picture S9(...)[V9(...)] in this subset "
                "(unsigned packed decimal is not supported)",
                entry.pic_line, entry.pic_col, source)
        digits = spec.n + spec.m
        text = f"S9({spec.n})" + (f"V9({spec.m})" if spec.has_v else "")
        return Pic("comp3", digits // 2 + 1, digits, spec.m, text)
    if spec.signed:
        raise CopybookError(
            "signed DISPLAY numbers (overpunch sign) are not supported; "
            "use PIC S9(...)[V9(...)] COMP-3",
            entry.pic_line, entry.pic_col, source)
    if spec.has_v:
        raise CopybookError(
            "implied decimal V is only supported for signed COMP-3 fields",
            entry.pic_line, entry.pic_col, source)
    return Pic("display9", spec.n, spec.n, 0, f"9({spec.n})")


def _compute_offsets(field: Field, offset: int) -> None:
    field.offset = offset
    if field.is_group:
        cur = offset
        for child in field.children:
            _compute_offsets(child, cur)
            cur += child.fixed_length
        base_fixed = cur - offset
        base_max = sum(c.max_length for c in field.children)
    else:
        base_fixed = base_max = field.pic.length
    field.elem_length = base_fixed
    if field.occurs is None:
        field.fixed_length = base_fixed
        field.max_length = base_max
    elif field.occurs.is_dynamic:
        field.fixed_length = 0
        field.max_length = field.occurs.maximum * base_fixed
    else:
        field.fixed_length = field.occurs.maximum * base_fixed
        field.max_length = field.fixed_length


def _resolve_dep(odo: Field, fields: list[Field], source: str | None) -> Field:
    dep = odo.occurs.dep_names
    wanted = " OF ".join(dep)
    suffix = dep[::-1]  # NAME OF QUALIFIER -> path ends with (..., QUALIFIER, NAME)
    candidates = []
    for f in fields:
        if f.name is None or f.seq >= odo.seq:
            continue
        if f.pic is None or f.pic.kind != "display9":
            continue
        if f.occurs is not None or f.under_occurs() or not f.is_visible:
            continue
        if f.path[-len(suffix):] == suffix:
            candidates.append(f)
    if not candidates:
        raise CopybookError(
            f"DEPENDING ON target {wanted} does not name a visible unsigned PIC 9(n) integer "
            "declared before this OCCURS (forward references are not allowed)",
            odo.occurs.line, odo.occurs.col, source)
    if len(candidates) > 1:
        paths = ", ".join(c.path_string for c in candidates)
        raise CopybookError(
            f"DEPENDING ON target {wanted} is ambiguous ({paths}); qualify it with OF/IN",
            odo.occurs.line, odo.occurs.col, source)
    return candidates[0]


def compile_copybook(text: str, source: str | None = None) -> Layout:
    """Compile copybook text into an inspectable Layout, or raise CopybookError."""
    src = source or "<copybook>"
    entries = _Parser(_lex(text, source), source).parse()
    if not entries:
        raise CopybookError("copybook contains no data entries", 1, 1, source)

    root: Field | None = None
    stack: list[Field] = []
    fields: list[Field] = []
    for seq, entry in enumerate(entries):
        node = Field(entry.name, entry.level, entry.line, entry.col,
                     pic=_finalize_pic(entry, source), occurs=entry.occurs, seq=seq)
        if entry.level == 1:
            if root is not None:
                raise CopybookError("only one 01 root record is allowed", entry.line, entry.col, source)
            if node.pic is not None:
                raise CopybookError("the 01 root must be a group (no PICTURE clause)",
                                    entry.line, entry.col, source)
            if node.occurs is not None:
                raise CopybookError("the 01 root cannot have an OCCURS clause",
                                    entry.line, entry.col, source)
            if node.name is None:
                raise CopybookError("the 01 root record must have a name",
                                    entry.line, entry.col, source)
            root = node
            stack = [node]
            fields.append(node)
            continue
        if root is None:
            raise CopybookError("the first entry must be the 01 root group",
                                entry.line, entry.col, source)
        while stack and stack[-1].level >= entry.level:
            stack.pop()
        if not stack:
            raise CopybookError(f"level {entry.level:02d} item has no enclosing group",
                                entry.line, entry.col, source)
        parent = stack[-1]
        if parent.pic is not None:
            raise CopybookError(
                f"level {entry.level:02d} item cannot be subordinate to elementary item "
                f"{parent.name or 'FILLER'} (declared at line {parent.line}); check the level numbers",
                entry.line, entry.col, source)
        node.parent = parent
        parent.children.append(node)
        stack.append(node)
        fields.append(node)

    for f in fields:
        if f.is_group and not f.children:
            raise CopybookError(f"group item {f.name or 'FILLER'} has no subordinate items",
                                f.line, f.col, source)
        if f.occurs is not None:
            if f.is_filler:
                raise CopybookError("OCCURS on FILLER is not supported (an array needs a name)",
                                    f.occurs.line, f.occurs.col, source)
            if f.occurs.is_dynamic:
                if f.occurs.minimum != 0:
                    raise CopybookError("only OCCURS 0 TO n DEPENDING ON is supported",
                                        f.occurs.line, f.occurs.col, source)
                if f.occurs.maximum < 1:
                    raise CopybookError("OCCURS upper bound must be at least 1",
                                        f.occurs.line, f.occurs.col, source)
            elif f.occurs.maximum < 1:
                raise CopybookError("fixed OCCURS requires a bound of at least 1",
                                    f.occurs.line, f.occurs.col, source)

    odos = [f for f in fields if f.occurs is not None and f.occurs.is_dynamic]
    if len(odos) > 1:
        raise CopybookError("only one OCCURS ... DEPENDING ON is allowed per record",
                            odos[1].occurs.line, odos[1].occurs.col, source)
    odo = odos[0] if odos else None
    if odo is not None:
        node: Field | None = odo.parent
        while node is not None:
            if node.occurs is not None:
                raise CopybookError(
                    "OCCURS ... DEPENDING ON must not be nested inside another OCCURS "
                    "(the dynamic area must be the record tail)",
                    odo.occurs.line, odo.occurs.col, source)
            node = node.parent
        node = odo
        while node.parent is not None:
            if node.parent.children[-1] is not node:
                raise CopybookError(
                    "OCCURS ... DEPENDING ON must be the last item of the record; "
                    "entries after the dynamic area are not allowed",
                    odo.occurs.line, odo.occurs.col, source)
            node = node.parent
        odo.occurs.dep_field = _resolve_dep(odo, fields, source)

    named: dict[tuple[str, ...], Field] = {}
    for f in fields:
        if f.name is None:
            continue
        path = f.path
        if path in named:
            raise CopybookError(
                f"duplicate qualified path {f.path_string} "
                f"(first defined at line {named[path].line})",
                f.line, f.col, source)
        named[path] = f

    _compute_offsets(root, 0)
    if root.max_length > MAX_RECORD_LENGTH:
        raise CopybookError(
            f"record maximum length {root.max_length} exceeds the {MAX_RECORD_LENGTH} byte limit",
            root.line, root.col, source)
    return Layout(root=root, source=src, fields=fields, named=named, odo=odo)
