"""公式词法分析器。

只做词法切分，绝不调用 eval/compile。支持的 token：
- 数字（含小数、科学计数法）
- 双引号字符串（"" 转义）
- 引用：A1、$A$1、A1:B2、可选表名前缀（'Sales Detail'! 或 Inputs!）
- 名称/函数标识符（TRUE/FALSE 与函数名在 parser 层区分）
- 括号、逗号、四则与比较运算符

遇到外部链接（[1]! 形态）、未知运算符（^、&、% 等）立即标记
#UNSUPPORTED!，绝不静默跳过。
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import FormulaError, UNSUPPORTED

# token 类型
T_NUMBER = "number"
T_STRING = "string"
T_REF = "ref"            # 单元格或矩形范围，可能带表名
T_NAME = "name"          # 裸标识符（函数名 / TRUE / FALSE / 未知名）
T_OP = "op"              # 运算符与括号逗号
T_EXTERNAL = "external"  # [1] 外部链接引用

_OPENERS = set("(,")
_COMPARISONS = {"=", "<>", "<", "<=", ">", ">="}


@dataclass(frozen=True)
class Token:
    kind: str
    text: str
    pos: int  # 在原公式中的起始位置，便于报错定位


def tokenize(formula: str) -> list[Token]:
    src = formula.strip()
    # 等号前缀：公式节点的内容通常不带 '='，但容错剥离唯一一个
    if src.startswith("="):
        src = src[1:]
    tokens: list[Token] = []
    i = 0
    n = len(src)
    while i < n:
        ch = src[i]
        if ch in " \t\n\r":
            i += 1
            continue
        start = i

        # 字符串字面量
        if ch == '"':
            i = _read_string(src, i)
            tokens.append(Token(T_STRING, src[start:i], start))
            continue

        # 数字
        if ch.isdigit() or (ch == "." and i + 1 < n and src[i + 1].isdigit()):
            i = _read_number(src, i)
            tokens.append(Token(T_NUMBER, src[start:i], start))
            continue

        # 外部链接 [n]... 明确不支持：吞掉整段外部引用（可含后续 !A1）
        if ch == "[":
            j = _read_external(src, i)
            # [1] 之后可能紧跟 Sheet1!A1 形态，一并归入外部 token
            j = _skip_external_tail(src, j)
            tokens.append(Token(T_EXTERNAL, src[start:j], start))
            i = j
            continue

        # 引号表名：'Sales Detail'!A1
        if ch == "'":
            i = _read_quoted_sheet(src, i)
            i = _read_ref_tail(src, i, tokens, start)
            continue

        # 比较运算符
        two = src[i : i + 2]
        if two in _COMPARISONS:
            tokens.append(Token(T_OP, two, start))
            i += 2
            continue
        if ch in "+-*/(),<>=:":
            # ':' 仅允许出现在已被识别的引用内部（由引用读取逻辑吞掉）；
            # 独立出现说明是非法范围拼接，交给 parser 报 unsupported
            tokens.append(Token(T_OP, ch, start))
            i += 1
            continue

        # $ 开头的绝对/混合引用，如 $B$2、B$2 不会走到这里（B 开头）
        if ch == "$":
            j = _skip_a1_or_range(src, i)
            tokens.append(Token(T_REF, src[i:j], start))
            i = j
            continue

        # 标识符或引用开头：字母 / 下划线
        if ch.isalpha() or ch == "_":
            word_start = i
            i = _read_identifier(src, i)
            word = src[word_start:i]
            # 标识符后若紧跟单元格/范围形态，则整体是引用（可含表名前缀）
            if _is_ref_continuation(src, i, word):
                i = _read_ref_tail(src, i, tokens, start, head=word)
            else:
                tokens.append(Token(T_NAME, word, start))
            continue

        raise FormulaError(
            UNSUPPORTED, f"公式含无法识别的字符 {ch!r}（位置 {start + 1}）"
        )
    return tokens


def _read_a1_token(src: str, i: int) -> int:
    """读取一个 A1 标记（允许 $ 出现在列前或行前），如 $B$2、C$2、$D3。"""
    j = i
    n = len(src)
    if j < n and src[j] == "$":
        j += 1
    while j < n and src[j].isalpha():
        j += 1
    if j < n and src[j] == "$":
        j += 1
    while j < n and src[j].isdigit():
        j += 1
    if j == i:
        raise FormulaError(UNSUPPORTED, f"非法引用（位置 {i + 1}）")
    return j


def _read_string(src: str, i: int) -> int:
    j = i + 1
    n = len(src)
    while j < n:
        if src[j] == '"':
            if j + 1 < n and src[j + 1] == '"':  # 转义的 ""
                j += 2
                continue
            return j + 1
        j += 1
    raise FormulaError(UNSUPPORTED, "字符串字面量缺少结束双引号")


def _read_number(src: str, i: int) -> int:
    j = i
    n = len(src)
    seen_dot = False
    while j < n and (src[j].isdigit() or src[j] == "."):
        if src[j] == ".":
            if seen_dot:
                raise FormulaError(UNSUPPORTED, f"非法数字（位置 {j + 1}）")
            seen_dot = True
        j += 1
    # 科学计数法
    if j < n and src[j] in "eE":
        k = j + 1
        if k < n and src[k] in "+-":
            k += 1
        if k < n and src[k].isdigit():
            j = k
            while j < n and src[j].isdigit():
                j += 1
    return j


def _read_external(src: str, i: int) -> int:
    j = i + 1
    n = len(src)
    while j < n and src[j] != "]":
        j += 1
    if j >= n:
        raise FormulaError(UNSUPPORTED, "外部链接引用缺少 ] 结束符")
    return j + 1  # 调用方整体作为 T_EXTERNAL，求值时再报 unsupported


def _skip_external_tail(src: str, i: int) -> int:
    """[1] 之后允许 Sheet1!A1 或 'S 1'!A1 或直接运算符，仅吞引用部分。"""
    n = len(src)
    j = i
    if j < n and src[j] == "'":
        j = _read_quoted_sheet(src, j)
    elif j < n and (src[j].isalpha() or src[j] == "_"):
        while j < n and (src[j].isalnum() or src[j] == "_"):
            j += 1
    else:
        return j
    if j < n and src[j] == "!":
        j += 1
        j = _skip_a1_or_range(src, j)
    return j


def _read_quoted_sheet(src: str, i: int) -> int:
    j = i + 1
    n = len(src)
    while j < n:
        if src[j] == "'":
            if j + 1 < n and src[j + 1] == "'":  # 表名中的 '' 转义
                j += 2
                continue
            return j + 1
        j += 1
    raise FormulaError(UNSUPPORTED, "引号表名缺少结束单引号")


def _is_ref_continuation(src: str, i: int, head: str) -> bool:
    """判断标识符后是否构成引用：Sheet1!A1、A1 形态，或 C$2 的锁定符。"""
    n = len(src)
    if i < n and src[i] == "!":
        return True
    if i < n and src[i] == "$":
        return True
    # head 本身形如 A1 / AB12 时由 _read_ref_tail 继续读可能的 :B2
    if _looks_like_cell_start(head):
        return True
    return False


def _looks_like_cell_start(word: str) -> bool:
    body = word.lstrip("$")
    if not body or not body[0].isalpha():
        return False
    return any(c.isdigit() for c in body) or "$" in word


def _read_identifier(src: str, i: int) -> int:
    j = i
    n = len(src)
    # 不含 $：$ 是引用锁定标记，由引用读取逻辑处理
    while j < n and (src[j].isalnum() or src[j] == "_"):
        j += 1
    return j


def _read_ref_tail(
    src: str, i: int, tokens: list[Token], start: int, head: str | None = None
) -> int:
    """从表名或列字母之后读取完整引用，写入一个 T_REF。"""
    n = len(src)
    j = i
    if head is not None:
        # head 可能是纯表名（后跟 !），也可能本身就是完整 A1
        if j < n and src[j] == "!":
            j += 1
            j = _read_a1_token(src, j)
            if j < n and src[j] == ":":
                j = _read_a1_token(src, j + 1)
        elif j < n and src[j] == "$":
            # head 是列字母（如 C），后跟 $2 行锁定
            j = _read_a1_token(src, j)
        elif j < n and src[j] == ":":
            # head 已是完整 A1，仅续读范围右端
            j = _read_a1_token(src, j + 1)
    else:
        # 引号表名之后必须是 !
        if j >= n or src[j] != "!":
            raise FormulaError(
                UNSUPPORTED, f"引号表名后缺少 !（位置 {j + 1}）"
            )
        j += 1
        j = _read_a1_token(src, j)
        if j < n and src[j] == ":":
            j = _read_a1_token(src, j + 1)
    tokens.append(Token(T_REF, src[start:j], start))
    return j


def _skip_a1_or_range(src: str, i: int) -> int:
    j = _read_a1_token(src, i)
    if j < len(src) and src[j] == ":":
        j = _read_a1_token(src, j + 1)
    return j
