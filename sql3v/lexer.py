"""词法分析器。

支持的词法单元：
  整数（有符号 64 位，负号与数字紧贴时并入整数字面量）、
  普通未加引号标识符、NULL/IS/IN/NOT/AND/OR 关键字、
  = <> < <= > >= ( ) ,
不支持引号字符串、算术运算符、函数调用等；遇到一律定位报错。
每个谓词最多 200 个词法单元（不含 EOF）。
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import LexError

INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1
MAX_TOKENS = 200

KEYWORDS = {
    "NULL": "NULL",
    "IS": "IS",
    "IN": "IN",
    "NOT": "NOT",
    "AND": "AND",
    "OR": "OR",
}


@dataclass(frozen=True)
class Token:
    kind: str          # INT / IDENT / NULL / IS / IN / NOT / AND / OR / 运算符原文
    text: str
    value: int | None  # 仅 INT 有值
    line: int
    col: int            # 从 1 开始
    end_col: int        # 词法单元结束列（不含）

    def loc(self) -> tuple[int, int]:
        return self.line, self.col


_SIMPLE_OPS = {"=": "=", "<": "<", ">": ">", "(": "(", ")": ")", ",": ","}


def tokenize(text: str) -> list[Token]:
    tokens: list[Token] = []
    i = 0
    line = 1
    line_start = 0
    n = len(text)

    def col_of(pos: int) -> int:
        return pos - line_start + 1

    while i < n:
        ch = text[i]
        if ch in " \t\r":
            i += 1
            continue
        if ch == "\n":
            line += 1
            i += 1
            line_start = i
            continue

        start = i
        c = ch

        # 负号仅在与数字紧贴、且前一个 token 不是右括号/整数/标识符/NULL 时，
        # 才作为有符号整数字面量的一部分；其余位置的 '-' 属于不支持的算术。
        if c == "-" and i + 1 < n and text[i + 1].isdigit():
            prev = tokens[-1] if tokens else None
            sign_starts_literal = prev is None or prev.kind not in (
                "INT", "IDENT", "NULL", ")"
            )
            if sign_starts_literal:
                i += 1
                while i < n and text[i].isdigit():
                    i += 1
                _emit_int(text, start, i, line, col_of(start), tokens)
                continue
            raise LexError(
                "不支持的运算符 '-'（本工具不支持算术运算；带符号整数的负号必须与数字紧贴）",
                line, col_of(start),
            )

        if c.isdigit():
            while i < n and text[i].isdigit():
                i += 1
            _emit_int(text, start, i, line, col_of(start), tokens)
            continue

        if c.isalpha() or c == "_":
            while i < n and (text[i].isalnum() or text[i] == "_"):
                i += 1
            word = text[start:i]
            upper = word.upper()
            if upper in KEYWORDS:
                tokens.append(Token(KEYWORDS[upper], word, None, line, col_of(start), col_of(i)))
            else:
                tokens.append(Token("IDENT", word, None, line, col_of(start), col_of(i)))
            continue

        if c in "=<>(,)":
            if c == "<" and i + 1 < n and text[i + 1] == "=":
                tokens.append(Token("<=", "<=", None, line, col_of(start), col_of(start) + 2))
                i += 2
                continue
            if c == "<" and i + 1 < n and text[i + 1] == ">":
                tokens.append(Token("<>", "<>", None, line, col_of(start), col_of(start) + 2))
                i += 2
                continue
            if c == ">" and i + 1 < n and text[i + 1] == "=":
                tokens.append(Token(">=", ">=", None, line, col_of(start), col_of(start) + 2))
                i += 2
                continue
            kind = _SIMPLE_OPS[c]
            tokens.append(Token(kind, c, None, line, col_of(start), col_of(start) + 1))
            i += 1
            continue

        if c in "+*/%":
            raise LexError(f"不支持的运算符 '{c}'（本工具不支持算术运算）", line, col_of(start))
        if c in "'\"`":
            raise LexError(
                f"不支持带引号的词法单元 '{c}'（列名只能是未加引号的普通标识符，值只能是整数或 NULL）",
                line, col_of(start),
            )
        if c == "[":
            raise LexError("不支持带方括号的标识符，列名只能是未加引号的普通标识符", line, col_of(start))
        if c == ";":
            raise LexError("不支持语句分隔符 ';'，每个谓词只能是单个布尔表达式", line, col_of(start))
        raise LexError(f"非法字符 {c!r}", line, col_of(start))

    if len(tokens) > MAX_TOKENS:
        t = tokens[MAX_TOKENS]
        raise LexError(
            f"谓词词法单元数 {len(tokens)} 超过上限 {MAX_TOKENS}",
            t.line, t.col,
        )

    tokens.append(Token("EOF", "", None, line, col_of(i) if i == n else col_of(i), col_of(i) + 1))
    return tokens


def _emit_int(text: str, start: int, end: int, line: int, col: int,
              tokens: list[Token]) -> None:
    raw = text[start:end]
    try:
        value = int(raw)
    except ValueError:
        raise LexError(f"非法整数 {raw!r}", line, col) from None
    if not INT64_MIN <= value <= INT64_MAX:
        raise LexError(
            f"整数 {raw} 超出有符号 64 位范围 [{INT64_MIN}, {INT64_MAX}]",
            line, col,
        )
    tokens.append(Token("INT", raw, value, line, col, col + (end - start)))
