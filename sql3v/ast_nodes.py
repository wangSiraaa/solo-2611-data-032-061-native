"""AST 节点定义。

所有节点都带词法位置（line/col/end_col），便于错误定位与 explain 展示。
表达式节点分为叶子（整数字面量、NULL、列引用）、布尔节点（逻辑运算、
比较、NULL 判断、IN/NOT IN）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# 叶子节点（既可以做比较操作数，也可以做 IN 列表元素）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IntLit:
    value: int
    line: int
    col: int
    end_col: int
    kind: str = field(default="IntLit", compare=False)


@dataclass(frozen=True)
class NullLit:
    line: int
    col: int
    end_col: int
    kind: str = field(default="NullLit", compare=False)


@dataclass(frozen=True)
class Column:
    name: str
    line: int
    col: int
    end_col: int
    kind: str = field(default="Column", compare=False)


Leaf = IntLit | NullLit | Column


# ---------------------------------------------------------------------------
# 布尔节点
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IsNull:
    operand: Leaf
    negate: bool  # False => IS NULL, True => IS NOT NULL
    line: int
    col: int
    end_col: int
    kind: str = field(default="IsNull", compare=False)


@dataclass(frozen=True)
class InList:
    operand: Leaf
    elements: tuple[Leaf, ...]
    negate: bool  # False => IN, True => NOT IN
    line: int
    col: int
    end_col: int
    kind: str = field(default="InList", compare=False)


@dataclass(frozen=True)
class Compare:
    op: str  # =, <>, <, <=, >, >=
    left: Leaf
    right: Leaf
    line: int
    col: int
    end_col: int
    kind: str = field(default="Compare", compare=False)


@dataclass(frozen=True)
class NotExpr:
    operand: "Expr"
    line: int
    col: int
    end_col: int
    kind: str = field(default="NotExpr", compare=False)


@dataclass(frozen=True)
class BinBool:
    op: str  # AND / OR
    left: "Expr"
    right: "Expr"
    line: int
    col: int
    end_col: int
    kind: str = field(default="BinBool", compare=False)


Expr = IsNull | InList | Compare | NotExpr | BinBool

# 任意节点（叶子或布尔表达式）
Node = Expr | Leaf
