"""递归下降语法分析与共享公式位移。

支持文法（优先级从低到高）：
    expr     := comparison
    comparison := arith (('=' | '<>' | '<' | '<=' | '>' | '>=') arith)?
    arith    := term (('+' | '-') term)*
    term     := factor (('*' | '/') factor)*
    factor   := ('-' | '+') factor | primary
    primary  := NUMBER | STRING | TRUE | FALSE
              | REF（单元格或矩形范围，可带表名）
              | NAME '(' args ')'   仅 SUM/IF/IFERROR
              | '(' expr ')'

引用支持：A1、$A$1、A$1、$A1、A1:B2、Sheet!A1、'含空格 表名'!A1。
共享公式跟随格通过 shift() 按主格→跟随格的行列位移，仅对相对部分
（无 $ 锁定的列/行）平移，从而还原跟随格自己的公式，绝不复用主格公式。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import UNSUPPORTED, FormulaError
from .lexer import (
    T_EXTERNAL,
    T_NAME,
    T_NUMBER,
    T_OP,
    T_REF,
    T_STRING,
    tokenize,
)

SUPPORTED_FUNCTIONS = frozenset({"SUM", "IF", "IFERROR"})
MAX_COL = 16384  # XFD
MAX_ROW = 1048576


# ---------------- AST 节点 ----------------
@dataclass(frozen=True)
class Number:
    value: float


@dataclass(frozen=True)
class Text:
    value: str


@dataclass(frozen=True)
class Bool:
    value: bool


@dataclass(frozen=True)
class Reference:
    """单元格或矩形范围引用。

    sheet 为 None 表示本表；col/row 为 1 起整数；abs_col/abs_row
    为 True 表示该维度被 $ 锁定。范围端点存于 end_*，单格时为 None。
    """

    col: int
    row: int
    abs_col: bool
    abs_row: bool
    end_col: int | None = None
    end_row: int | None = None
    end_abs_col: bool = False
    end_abs_row: bool = False
    sheet: str | None = None
    raw: str = ""

    @property
    def is_range(self) -> bool:
        return self.end_col is not None


@dataclass(frozen=True)
class Unary:
    op: str
    operand: object


@dataclass(frozen=True)
class Binary:
    op: str
    left: object
    right: object


@dataclass(frozen=True)
class FunctionCall:
    name: str
    args: list = field(default_factory=list)


@dataclass(frozen=True)
class Unsupported:
    """不支持特性的显式 AST 节点，求值时必定抛出带原因的错误。"""

    reason: str
    raw: str = ""


# ---------------- 列字母换算 ----------------
def col_to_letters(col: int) -> str:
    if not 1 <= col <= MAX_COL:
        raise FormulaError(UNSUPPORTED, f"列号越界: {col}")
    letters = ""
    while col > 0:
        col, rem = divmod(col - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def letters_to_col(letters: str) -> int:
    col = 0
    for ch in letters:
        col = col * 26 + (ord(ch.upper()) - 64)
    if not 1 <= col <= MAX_COL:
        raise FormulaError(UNSUPPORTED, f"列号越界: {letters}")
    return col


# ---------------- 引用文本解析 ----------------
def parse_reference(raw: str) -> Reference:
    """把形如 'Sales Detail'!$B$2:D5 的文本解析为 Reference。"""
    sheet = None
    rest = raw
    if rest.startswith("'"):
        end = _closing_quote(rest)
        sheet = rest[1:end].replace("''", "'")
        rest = rest[end + 1 :]
        if not rest.startswith("!"):
            raise FormulaError(UNSUPPORTED, f"引用缺少 ! 分隔: {raw!r}")
        rest = rest[1:]
    elif "!" in rest:
        sheet, rest = rest.split("!", 1)

    if ":" in rest:
        head, tail = rest.split(":", 1)
        c1, r1, ac1, ar1 = _parse_a1(head)
        c2, r2, ac2, ar2 = _parse_a1(tail)
        return Reference(c1, r1, ac1, ar1, c2, r2, ac2, ar2, sheet, raw)
    c, r, ac, ar = _parse_a1(rest)
    return Reference(c, r, ac, ar, None, None, False, False, sheet, raw)


def _closing_quote(s: str) -> int:
    j = 1
    while j < len(s):
        if s[j] == "'":
            if j + 1 < len(s) and s[j + 1] == "'":
                j += 2
                continue
            return j
        j += 1
    raise FormulaError(UNSUPPORTED, f"引号表名未闭合: {s!r}")


def _parse_a1(token: str) -> tuple[int, int, bool, bool]:
    s = token
    abs_col = False
    abs_row = False
    if s.startswith("$"):
        abs_col = True
        s = s[1:]
    k = 0
    while k < len(s) and s[k].isalpha():
        k += 1
    if k == 0:
        raise FormulaError(UNSUPPORTED, f"非法单元格引用: {token!r}")
    col = letters_to_col(s[:k])
    s = s[k:]
    # 行前的 $（形如 C$2）表示行绝对；列前的 $ 已在上面处理
    if s.startswith("$"):
        abs_row = True
        s = s[1:]
    if not s.isdigit() or not s:
        raise FormulaError(UNSUPPORTED, f"非法单元格引用: {token!r}")
    row = int(s)
    if not 1 <= row <= MAX_ROW:
        raise FormulaError(UNSUPPORTED, f"行号越界: {row}")
    return col, row, abs_col, abs_row


# ---------------- 公式渲染 ----------------
def render_a1(col: int, row: int, abs_col: bool, abs_row: bool) -> str:
    return f"{'$' if abs_col else ''}{col_to_letters(col)}{'$' if abs_row else ''}{row}"


def render(node: object, top: bool = True) -> str:
    if isinstance(node, Number):
        f = float(node.value)
        return str(int(f)) if f.is_integer() else repr(f)
    if isinstance(node, Text):
        return '"' + node.value.replace('"', '""') + '"'
    if isinstance(node, Bool):
        return "TRUE" if node.value else "FALSE"
    if isinstance(node, Reference):
        prefix = ""
        if node.sheet is not None:
            safe = node.sheet.replace("'", "''")
            prefix = f"'{safe}'!" if _needs_quotes(node.sheet) else f"{node.sheet}!"
        head = render_a1(node.col, node.row, node.abs_col, node.abs_row)
        if node.is_range:
            tail = render_a1(node.end_col, node.end_row, node.end_abs_col, node.end_abs_row)  # type: ignore[arg-type]
            return prefix + head + ":" + tail
        return prefix + head
    if isinstance(node, Unary):
        return f"{node.op}{render(node.operand, top=False)}"
    if isinstance(node, Binary):
        return _render_binary(node)
    if isinstance(node, FunctionCall):
        return f"{node.name}({','.join(render(a, top=False) for a in node.args)})"
    if isinstance(node, Unsupported):
        return node.raw or f"<unsupported: {node.reason}>"
    raise FormulaError(UNSUPPORTED, f"无法渲染 AST 节点 {type(node).__name__}")


# 运算符优先级：比较 < 加减 < 乘除；同级左结合
_PRECEDENCE = {"=": 1, "<>": 1, "<": 1, "<=": 1, ">": 1, ">=": 1,
               "+": 2, "-": 2, "*": 3, "/": 3}


def _render_binary(node: "Binary") -> str:
    def child(child_node, is_right: bool) -> str:
        if isinstance(child_node, Binary):
            child_prec = _PRECEDENCE[child_node.op]
            parent_prec = _PRECEDENCE[node.op]
            # 子表达式优先级更低，或同级右操作数（左结合），需要括号
            need = child_prec < parent_prec or (
                is_right and child_prec == parent_prec
            )
            text = _render_binary(child_node)
            return f"({text})" if need else text
        return render(child_node, top=False)

    return f"{child(node.left, False)}{node.op}{child(node.right, True)}"


def _needs_quotes(sheet: str) -> bool:
    if not sheet:
        return True
    if any(c.isspace() for c in sheet):
        return True
    if not (sheet[0].isalpha() or sheet[0] == "_"):
        return True
    return any(not (c.isalnum() or c == "_") for c in sheet)


# ---------------- 共享公式位移 ----------------
def shift(node: object, dcol: int, drow: int) -> object:
    """按 (dcol, drow) 平移引用的相对部分；绝对部分不动。"""
    if isinstance(node, Reference):
        col = node.col if node.abs_col else node.col + dcol
        row = node.row if node.abs_row else node.row + drow
        if not (1 <= col <= MAX_COL and 1 <= row <= MAX_ROW):
            raise FormulaError(
                UNSUPPORTED,
                f"共享公式位移后引用越界: {node.raw!r} -> 列{col} 行{row}",
            )
        end_col = node.end_col
        end_row = node.end_row
        if node.is_range:
            end_col = node.end_col if node.end_abs_col else node.end_col + dcol  # type: ignore[operator]
            end_row = node.end_row if node.end_abs_row else node.end_row + drow  # type: ignore[operator]
            if not (1 <= end_col <= MAX_COL and 1 <= end_row <= MAX_ROW):  # type: ignore[operator]
                raise FormulaError(
                    UNSUPPORTED,
                    f"共享公式位移后范围越界: {node.raw!r} -> 列{end_col} 行{end_row}",
                )
        return Reference(
            col, row, node.abs_col, node.abs_row,
            end_col, end_row, node.end_abs_col, node.end_abs_row,
            node.sheet, node.raw,
        )
    if isinstance(node, Unary):
        return Unary(node.op, shift(node.operand, dcol, drow))
    if isinstance(node, Binary):
        return Binary(node.op, shift(node.left, dcol, drow), shift(node.right, dcol, drow))
    if isinstance(node, FunctionCall):
        return FunctionCall(node.name, [shift(a, dcol, drow) for a in node.args])
    return node  # Number/Text/Bool/Unsupported 不变


# ---------------- 递归下降分析 ----------------
class Parser:
    def __init__(self, tokens: list, formula_text: str):
        self.tokens = tokens
        self.text = formula_text
        self.pos = 0

    def peek(self):
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def take(self):
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def expect_op(self, op: str):
        tok = self.peek()
        if tok is None or tok.kind != T_OP or tok.text != op:
            raise FormulaError(
                UNSUPPORTED,
                f"公式 {self.text!r} 缺少期望的 {op!r}（位置 {self.pos + 1}）",
            )
        return self.take()

    def parse(self):
        node = self.parse_comparison()
        if self.pos != len(self.tokens):
            tok = self.peek()
            raise FormulaError(
                UNSUPPORTED, f"公式 {self.text!r} 在 token {tok.text!r} 后存在多余内容"
            )
        return node

    def parse_comparison(self):
        left = self.parse_arith()
        tok = self.peek()
        if tok is not None and tok.kind == T_OP and tok.text in (
            "=", "<>", "<", "<=", ">", ">="
        ):
            self.take()
            right = self.parse_arith()
            return Binary(tok.text, left, right)
        return left

    def parse_arith(self):
        node = self.parse_term()
        while True:
            tok = self.peek()
            if tok is not None and tok.kind == T_OP and tok.text in ("+", "-"):
                self.take()
                node = Binary(tok.text, node, self.parse_term())
            else:
                return node

    def parse_term(self):
        node = self.parse_factor()
        while True:
            tok = self.peek()
            if tok is not None and tok.kind == T_OP and tok.text in ("*", "/"):
                self.take()
                node = Binary(tok.text, node, self.parse_factor())
            else:
                return node

    def parse_factor(self):
        tok = self.peek()
        if tok is None:
            raise FormulaError(UNSUPPORTED, f"公式 {self.text!r} 意外结束")
        if tok.kind == T_OP and tok.text in ("+", "-"):
            self.take()
            operand = self.parse_factor()
            return Unary(tok.text, operand) if tok.text == "-" else operand
        return self.parse_primary()

    def parse_primary(self):
        tok = self.take()
        if tok.kind == T_OP and tok.text == "(":
            inner = self.parse_comparison()
            self.expect_op(")")
            return inner
        if tok.kind == T_NUMBER:
            return Number(float(tok.text))
        if tok.kind == T_STRING:
            return Text(_unquote(tok.text))
        if tok.kind == T_REF:
            return parse_reference(tok.text)
        if tok.kind == T_EXTERNAL:
            return Unsupported(
                f"外部链接引用 {tok.text!r} 不在支持范围内（禁止跨工作簿取值）",
                tok.text,
            )
        if tok.kind == T_NAME:
            upper = tok.text.upper()
            if upper == "TRUE":
                return Bool(True)
            if upper == "FALSE":
                return Bool(False)
            nxt = self.peek()
            if nxt is None or not (nxt.kind == T_OP and nxt.text == "("):
                # 裸名称：命名区域 / 未定义名称，本工具明确不支持
                return Unsupported(
                    f"名称 {tok.text!r} 未作为函数调用；命名区域与未定义名称不在支持范围内",
                    tok.text,
                )
            return self.parse_function(tok.text, upper)
        raise FormulaError(
            UNSUPPORTED, f"公式 {self.text!r} 中 token {tok.text!r} 无法作为表达式开头"
        )

    def parse_function(self, name: str, upper: str):
        self.expect_op("(")
        raw_args = self._split_args()
        if upper not in SUPPORTED_FUNCTIONS:
            # 不支持的函数：优先报告“函数不支持”，不被参数里的子集外
            # 语法（如 B:C 整列引用）掩盖
            return Unsupported(
                f"函数 {name} 不在支持集合 {sorted(SUPPORTED_FUNCTIONS)} 内",
                f"{name}(...)",
            )
        args = []
        for tokens in raw_args:
            # 支持的函数参数必须在子集内；独立 ':' 等会在此显式报错
            args.append(Parser(tokens, self.text).parse())
        arities = {"SUM": (1, 255), "IF": (2, 3), "IFERROR": (2, 2)}
        lo, hi = arities[upper]
        if not lo <= len(args) <= hi:
            raise FormulaError(
                UNSUPPORTED, f"函数 {upper} 参数个数 {len(args)} 不在 {lo}..{hi} 范围"
            )
        return FunctionCall(upper, args)

    def _split_args(self) -> list[list]:
        """按顶层逗号切分参数 token，直到匹配的右括号。"""
        groups: list[list] = []
        depth = 0
        current: list = []
        nxt = self.peek()
        if nxt is not None and nxt.kind == T_OP and nxt.text == ")":
            self.take()
            return []
        while True:
            tok = self.take()
            if tok is None:
                raise FormulaError(UNSUPPORTED, "函数参数缺少右括号")
            if tok.kind == T_OP and tok.text == "(":
                depth += 1
            elif tok.kind == T_OP and tok.text == ")":
                if depth == 0:
                    groups.append(current)
                    return groups
                depth -= 1
            elif tok.kind == T_OP and tok.text == "," and depth == 0:
                groups.append(current)
                current = []
                continue
            current.append(tok)


def _unquote(literal: str) -> str:
    return literal[1:-1].replace('""', '"')


def parse_formula(formula_text: str) -> object:
    """词法 + 语法分析的统一入口。"""
    tokens = tokenize(formula_text)
    if not tokens:
        raise FormulaError(UNSUPPORTED, f"空公式: {formula_text!r}")
    return Parser(tokens, formula_text).parse()
