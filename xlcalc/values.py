"""受限公式语义下的值模型。

严格区分六类值：
- 空白 BLANK：单元格不存在，或存在但没有值（空单元格）
- 空字符串 EMPTY_STRING：显式写入的 ""（文本的一种，长度为 0）
- 数字 NUMBER：Python float（整数也以 float 承载，贴近 Excel 数值模型）
- 文本 TEXT：非空字符串
- 布尔 BOOLEAN：True / False
- 错误 ERROR：见 errors.py，携带可读原因

语义要点（与 SUM 的“范围聚合”语义刻意区分，不可混用）：
- 直接算术：空白按 0 参与；布尔 TRUE=1/FALSE=0；文本（含空字符串）
  参与算术一律 #VALUE!。
- 比较：空白按数字 0；布尔大于任何数字与文本；文本按字典序比较；
  同类型按各自规则；错误参与比较直接传播。
- SUM 范围聚合：跳过空白与文本（含空字符串），范围内布尔也跳过；
  SUM 的直接标量参数中的布尔按 1/0 计（见 engine.py 实现）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .errors import DIV0, ERROR_CODES, FormulaError, VALUE

BLANK = "blank"
EMPTY_STRING = "empty_string"
NUMBER = "number"
TEXT = "text"
BOOLEAN = "boolean"
ERROR = "error"


@dataclass(frozen=True)
class CellValue:
    """不可变的值对象。type 取本模块六个常量之一。"""

    type: str
    value: object = None  # number: float; text/empty_string: str; bool: bool; error: str
    reason: str | None = None  # 仅 error 类型携带具体原因

    # ---- 构造辅助 ----
    @staticmethod
    def blank() -> "CellValue":
        return CellValue(BLANK)

    @staticmethod
    def empty_string() -> "CellValue":
        return CellValue(EMPTY_STRING, "")

    @staticmethod
    def number(x: float) -> "CellValue":
        if isinstance(x, bool):  # bool 是 int 子类，必须先拦截
            return CellValue.boolean(x)
        if not math.isfinite(x):
            raise FormulaError(DIV0, f"数值运算结果非有限值: {x}")
        return CellValue(NUMBER, float(x))

    @staticmethod
    def text(s: str) -> "CellValue":
        if s == "":
            return CellValue.empty_string()
        return CellValue(TEXT, s)

    @staticmethod
    def boolean(b: bool) -> "CellValue":
        return CellValue(BOOLEAN, bool(b))

    @staticmethod
    def error(code: str, reason: str) -> "CellValue":
        if code not in ERROR_CODES:
            raise ValueError(f"未知错误码: {code!r}")
        return CellValue(ERROR, code, reason)

    # ---- 谓词 ----
    @property
    def is_blank(self) -> bool:
        return self.type == BLANK

    @property
    def is_error(self) -> bool:
        return self.type == ERROR

    # ---- 语义转换 ----
    def to_scalar_number(self, context: str = "算术运算") -> float:
        """直接算术语境的数值转换。文本（含空串）一律 #VALUE!。"""
        if self.type == NUMBER:
            return self.value  # type: ignore[return-value]
        if self.type == BLANK:
            return 0.0
        if self.type == BOOLEAN:
            return 1.0 if self.value else 0.0
        if self.type == ERROR:
            raise FormulaError(self.value, self.reason or "引用单元格为错误值")  # type: ignore[arg-type]
        raise FormulaError(
            VALUE, f"{context}不能把文本{_describe(self)}当作数字"
        )

    def to_logical(self, context: str = "IF 条件") -> bool:
        """IF 条件的布尔化：数字 0/空白为假，非零为真，布尔直接取，
        文本（含空串）在 Excel 中直接参与 IF 条件属于 #VALUE!。"""
        if self.type == BOOLEAN:
            return bool(self.value)
        if self.type == NUMBER:
            return self.value != 0.0  # type: ignore[operator]
        if self.type == BLANK:
            return False
        if self.type == ERROR:
            raise FormulaError(self.value, self.reason or "引用单元格为错误值")  # type: ignore[arg-type]
        raise FormulaError(VALUE, f"{context}不能把文本{_describe(self)}当作逻辑值")

    # ---- 序列化（值视图 / explain 用）----
    def to_json_data(self) -> dict:
        if self.type == NUMBER:
            n = float(self.value)  # type: ignore[arg-type]
            if n.is_integer():
                return {"type": NUMBER, "value": int(n)}
            return {"type": NUMBER, "value": n}
        if self.type == ERROR:
            return {"type": ERROR, "value": self.value, "reason": self.reason}
        return {"type": self.type, "value": self.value}

    def display(self) -> str:
        """人类可读展示。"""
        if self.type == BLANK:
            return "<空白>"
        if self.type == EMPTY_STRING:
            return '""'
        if self.type == ERROR:
            return f"{self.value}（{self.reason}）"
        if self.type == BOOLEAN:
            return "TRUE" if self.value else "FALSE"
        return str(self.value)


def _describe(v: CellValue) -> str:
    if v.type == EMPTY_STRING:
        return '空字符串 ""'
    return f'"{v.value}"'


# ---- 比较语义 ----
def compare(op: str, left: CellValue, right: CellValue) -> CellValue:
    """六类值的比较运算，返回布尔值；错误直接传播。

    排序类别（Excel 约定）：数字(含按 0 处理的空白) < 文本(含空串) < 布尔。
    """
    if left.is_error:
        raise FormulaError(left.value, left.reason or "引用单元格为错误值")  # type: ignore[arg-type]
    if right.is_error:
        raise FormulaError(right.value, right.reason or "引用单元格为错误值")  # type: ignore[arg-type]

    rank = {BLANK: 0, NUMBER: 0, EMPTY_STRING: 1, TEXT: 1, BOOLEAN: 2}
    del rank
    lc = _comparable(left)
    rc = _comparable(right)
    if lc < rc:
        order = -1
    elif lc > rc:
        order = 1
    else:
        order = 0
    truth = {
        "=": order == 0,
        "<>": order != 0,
        "<": order < 0,
        "<=": order <= 0,
        ">": order > 0,
        ">=": order >= 0,
    }
    if op not in truth:
        raise FormulaError(VALUE, f"不支持的比较运算符 {op!r}")
    return CellValue.boolean(truth[op])


def _comparable(v: CellValue) -> tuple[int, object]:
    if v.type in (NUMBER,):
        return (0, float(v.value))  # type: ignore[arg-type]
    if v.type == BLANK:
        return (0, 0.0)
    if v.type in (TEXT, EMPTY_STRING):
        return (1, str(v.value))
    # 布尔：FALSE < TRUE
    return (2, 1 if v.value else 0)


# ---- 四则运算（直接算术语义）----
def arithmetic(op: str, left: CellValue, right: CellValue) -> CellValue:
    a = left.to_scalar_number(f"运算符 {op} 左值")
    b = right.to_scalar_number(f"运算符 {op} 右值")
    if op == "+":
        return CellValue.number(a + b)
    if op == "-":
        return CellValue.number(a - b)
    if op == "*":
        return CellValue.number(a * b)
    if op == "/":
        if b == 0.0:
            raise FormulaError(DIV0, f"除数为零: {_num_text(a)}/0")
        return CellValue.number(a / b)
    raise FormulaError(VALUE, f"不支持的运算符 {op!r}")


def negate(v: CellValue) -> CellValue:
    return CellValue.number(-v.to_scalar_number("一元负号"))


def _num_text(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else repr(x)
