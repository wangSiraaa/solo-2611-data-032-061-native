"""纯 Python 三值逻辑求值器。

赋值用 dict[列名, int | None] 表示，None 即 SQL NULL。
比较严格遵循 SQL：任一操作数为 NULL 时结果为 UNKNOWN。
IN / NOT IN 按列表语义求值；NOT IN 由 IN 的三值结果取三值 NOT 得到，
因此列表含 NULL 时产生的 UNKNOWN 不会被错误地当作 false 再取反。
"""

from __future__ import annotations

from . import ast_nodes as ast
from .trit import Trit, and3, not3, or3


class Evaluator:
    def __init__(self, env: dict[str, int | None]):
        self.env = env

    # -- 叶子取值 -----------------------------------------------------------

    def leaf_value(self, node: ast.Leaf) -> int | None:
        if node.kind == "IntLit":
            return node.value
        if node.kind == "NullLit":
            return None
        return self.env[node.name]

    # -- 求值入口 -----------------------------------------------------------

    def eval(self, node: ast.Node) -> Trit:
        k = node.kind
        if k == "Compare":
            return self._compare(node)
        if k == "IsNull":
            v = self.leaf_value(node.operand)
            is_null = v is None
            return Trit.TRUE if (is_null != node.negate) else Trit.FALSE
        if k == "InList":
            return self._in_list(node)
        if k == "NotExpr":
            return not3(self.eval(node.operand))
        if k == "BinBool":
            a = self.eval(node.left)
            b = self.eval(node.right)
            return and3(a, b) if node.op == "AND" else or3(a, b)
        raise ValueError(f"叶子节点 {k} 不能作为布尔结果")  # 解析阶段已拦截

    def _compare(self, node: ast.Compare) -> Trit:
        left = self.leaf_value(node.left)
        right = self.leaf_value(node.right)
        if left is None or right is None:
            return Trit.UNKNOWN
        op = node.op
        if op == "=":
            return Trit.TRUE if left == right else Trit.FALSE
        if op == "<>":
            return Trit.TRUE if left != right else Trit.FALSE
        if op == "<":
            return Trit.TRUE if left < right else Trit.FALSE
        if op == "<=":
            return Trit.TRUE if left <= right else Trit.FALSE
        if op == ">":
            return Trit.TRUE if left > right else Trit.FALSE
        if op == ">=":
            return Trit.TRUE if left >= right else Trit.FALSE
        raise ValueError(f"未知比较运算符 {op}")

    def _in_list(self, node: ast.InList) -> Trit:
        """IN / NOT IN 的 SQL 三值语义。

        IN：针为 NULL -> UNKNOWN；命中任一非 NULL 元素 -> TRUE；
            否则列表含 NULL -> UNKNOWN；再否则 FALSE。
        NOT IN：针为 NULL -> UNKNOWN；命中任一非 NULL 元素 -> FALSE；
            否则列表含 NULL -> UNKNOWN；再否则 TRUE。
        即 NOT IN 是 IN 三值结果的三值 NOT，UNKNOWN 不会被降级成 false 再取反。
        """
        needle = self.leaf_value(node.operand)
        saw_null = False
        matched = False
        for elem in node.elements:
            v = self.leaf_value(elem)
            if v is None:
                saw_null = True
            elif needle is not None and needle == v:
                matched = True

        if node.negate:
            if needle is None:
                return Trit.UNKNOWN
            if matched:
                return Trit.FALSE
            return Trit.UNKNOWN if saw_null else Trit.TRUE

        if needle is None:
            return Trit.UNKNOWN
        if matched:
            return Trit.TRUE
        return Trit.UNKNOWN if saw_null else Trit.FALSE


def evaluate(node: ast.Expr, env: dict[str, int | None]) -> Trit:
    return Evaluator(env).eval(node)


# ---------------------------------------------------------------------------
# 子表达式快照（explain 与反例关键步骤）
# ---------------------------------------------------------------------------

def _leaf_text(node: ast.Leaf, env: dict[str, int | None]) -> str:
    if node.kind == "IntLit":
        return str(node.value)
    if node.kind == "NullLit":
        return "NULL"
    v = env[node.name]
    # 用箭头表示“列在当前赋值下的取值”，避免与比较运算符 '=' 混淆
    return f"{node.name}→{'NULL' if v is None else v}"


def _render(node: ast.Node, env: dict[str, int | None]) -> str:
    """带当前取值的可读子表达式文本。"""
    k = node.kind
    if k in ("IntLit", "NullLit", "Column"):
        return _leaf_text(node, env)  # type: ignore[arg-type]
    if k == "Compare":
        return f"({_leaf_text(node.left, env)} {node.op} {_leaf_text(node.right, env)})"
    if k == "IsNull":
        op = "IS NOT NULL" if node.negate else "IS NULL"
        return f"({_leaf_text(node.operand, env)} {op})"
    if k == "InList":
        parts = ["NULL" if e.kind == "NullLit" else str(e.value) for e in node.elements]
        kw = "NOT IN" if node.negate else "IN"
        return f"({_leaf_text(node.operand, env)} {kw} ({', '.join(parts)}))"
    if k == "NotExpr":
        return f"NOT {_render(node.operand, env)}"
    if k == "BinBool":
        return f"({_render(node.left, env)} {node.op} {_render(node.right, env)})"
    raise ValueError(k)


def snapshot(node: ast.Expr, env: dict[str, int | None]) -> list[dict[str, object]]:
    """后序遍历，返回每个布尔子表达式的文本与三值结果（含最终表达式）。"""
    steps: list[dict[str, object]] = []

    def walk(n: ast.Node) -> object:
        ev = Evaluator(env)
        if n.kind in ("IntLit", "NullLit", "Column"):
            return ev.leaf_value(n)  # type: ignore[return-value]
        if n.kind == "Compare":
            result = ev._compare(n)
        elif n.kind == "IsNull":
            result = ev.eval(n)
        elif n.kind == "InList":
            result = ev.eval(n)
        elif n.kind == "NotExpr":
            walk(n.operand)
            result = ev.eval(n)
        elif n.kind == "BinBool":
            walk(n.left)
            walk(n.right)
            result = ev.eval(n)
        else:  # pragma: no cover - 防御性分支
            raise ValueError(n.kind)
        steps.append({
            "line": n.line,
            "col": n.col,
            "expr": _render(n, env),
            "result": result,
        })
        return result

    walk(node)
    return steps
