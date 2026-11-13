"""SQL 三值逻辑（3VL）的纯 Python 实现。

三个值：TRUE / FALSE / UNKNOWN。比较中只要任一操作数是 NULL，
结果即为 UNKNOWN；AND/OR/NOT 严格按 SQL 三值语义计算，UNKNOWN
绝不当作普通 false 处理。
"""

from __future__ import annotations

from enum import Enum


class Trit(Enum):
    TRUE = "TRUE"
    FALSE = "FALSE"
    UNKNOWN = "UNKNOWN"

    @property
    def short(self) -> str:
        return {"TRUE": "T", "FALSE": "F", "UNKNOWN": "U"}[self.value]

    @staticmethod
    def from_short(text: str) -> "Trit":
        return {"T": Trit.TRUE, "F": Trit.FALSE, "U": Trit.UNKNOWN}[text]


def not3(v: Trit) -> Trit:
    """NOT: TRUE<->FALSE, UNKNOWN->UNKNOWN（未知取反仍是未知）。"""
    if v is Trit.TRUE:
        return Trit.FALSE
    if v is Trit.FALSE:
        return Trit.TRUE
    return Trit.UNKNOWN


def and3(a: Trit, b: Trit) -> Trit:
    """SQL AND：任一 FALSE 即 FALSE；否则任一 UNKNOWN 即 UNKNOWN。"""
    if a is Trit.FALSE or b is Trit.FALSE:
        return Trit.FALSE
    if a is Trit.UNKNOWN or b is Trit.UNKNOWN:
        return Trit.UNKNOWN
    return Trit.TRUE


def or3(a: Trit, b: Trit) -> Trit:
    """SQL OR：任一 TRUE 即 TRUE；否则任一 UNKNOWN 即 UNKNOWN。"""
    if a is Trit.TRUE or b is Trit.TRUE:
        return Trit.TRUE
    if a is Trit.UNKNOWN or b is Trit.UNKNOWN:
        return Trit.UNKNOWN
    return Trit.FALSE


# 完整真值表（供测试与文档生成使用）
NOT_TABLE = ((Trit.TRUE, Trit.FALSE), (Trit.FALSE, Trit.TRUE), (Trit.UNKNOWN, Trit.UNKNOWN))
AND_TABLE = tuple((a, b, and3(a, b)) for a in Trit for b in Trit)
OR_TABLE = tuple((a, b, or3(a, b)) for a in Trit for b in Trit)

# WHERE 子句只收录结果为 TRUE 的行；FALSE 与 UNKNOWN 都被过滤。
def where_accepts(v: Trit) -> bool:
    return v is Trit.TRUE
