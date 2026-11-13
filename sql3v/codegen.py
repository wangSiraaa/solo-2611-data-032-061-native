"""由已校验 AST 生成受控 SQLite 表达式。

安全约束：
  * SQL 文本只由本模块根据 AST 节点类型拼出固定结构与运算符，
    绝不把用户输入文本直接拼进 SQL；
  * 两种渲染方式：
      - generate()：列名渲染为带双引号的定界标识符（标识符来自已校验
        声明，且对内部双引号翻倍转义），字面量渲染为 '?' 占位符，
        用于展示受控 SQL 模板；
      - parameterized_query()：列引用与字面量全部渲染为 '?'，所有值
        通过 sqlite3 绑定参数传入，执行期 SQL 文本与任何用户值无关。
生成结果只用于 SELECT <expr> 与 SELECT 1 WHERE <expr>，
不生成、不执行任何其他 SQL 语句。

顺序约定：IN 节点按“先左操作数、后列表元素”的顺序渲染并追加参数，
使参数序列与 SQL 文本中 '?' 从左到右的位置严格一致
（文本与参数同为 operand, e1, e2, ...）。
"""

from __future__ import annotations

from dataclasses import dataclass

from . import ast_nodes as ast


@dataclass(frozen=True)
class GeneratedSQL:
    expression: str                          # 带定界列名、字面量为 ? 的模板
    literal_params: tuple[int | None, ...]   # 模板中 ? 对应的字面量值
    leaf_kinds: tuple[str, ...]              # 叶子参数出现顺序（Column/IntLit/NullLit）


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def generate(node: ast.Node) -> GeneratedSQL:
    literal_params: list[int | None] = []
    leaf_kinds: list[str] = []

    def render(n: ast.Node) -> str:
        k = n.kind
        if k == "IntLit":
            literal_params.append(n.value)
            leaf_kinds.append(k)
            return "?"
        if k == "NullLit":
            literal_params.append(None)
            leaf_kinds.append(k)
            return "?"
        if k == "Column":
            leaf_kinds.append(k)
            return _quote_ident(n.name)
        if k == "Compare":
            return f"({render(n.left)} {n.op} {render(n.right)})"
        if k == "IsNull":
            # IS [NOT] NULL 中的 NULL 是语法关键字，不产生占位符与参数
            op = "IS NOT NULL" if n.negate else "IS NULL"
            operand = n.operand
            if operand.kind == "Column":
                leaf_kinds.append(operand.kind)
                operand_sql = _quote_ident(operand.name)
            elif operand.kind == "IntLit":
                literal_params.append(operand.value)
                leaf_kinds.append(operand.kind)
                operand_sql = "?"
            else:  # NullLit IS NULL：渲染为 NULL IS NULL，无参数
                operand_sql = "NULL"
            return f"({operand_sql} {op})"
        if k == "InList":
            # 先渲染左操作数再渲染列表元素，使参数追加顺序与 SQL 文本中
            # '?' 从左到右的位置严格一致（operand, e1, e2, ...）；
            # SQL 文本拼接顺序不影响参数绑定，绑定只看占位符位置。
            operand_sql = render(n.operand)
            rendered_elems = [render(e) for e in n.elements]
            elems = ", ".join(rendered_elems)
            kw = "NOT IN" if n.negate else "IN"
            return f"({operand_sql} {kw} ({elems}))"
        if k == "NotExpr":
            return f"(NOT {render(n.operand)})"
        if k == "BinBool":
            return f"({render(n.left)} {n.op} {render(n.right)})"
        raise ValueError(f"无法为节点 {k} 生成 SQL")

    expression = render(node)
    gen = GeneratedSQL(expression, tuple(literal_params), tuple(leaf_kinds))
    # 模板里每个 '?' 都必须有一个字面量参数
    assert expression.count("?") == len(literal_params)
    return gen


def parameterized_query(node: ast.Expr,
                        env: dict[str, int | None]) -> tuple[str, tuple[int | None, ...]]:
    """完全参数化查询：列引用与字面量全部为 '?'，值全部走绑定参数。

    叶子在渲染时即时追加参数，保证占位符顺序与绑定参数顺序严格一致，
    即使同一列在表达式中多次出现（如 x = x、x NOT IN (...)）。
    """
    params: list[int | None] = []

    def push(value: int | None) -> str:
        params.append(value)
        return "?"

    def render(n: ast.Node) -> str:
        k = n.kind
        if k == "IntLit":
            return push(n.value)
        if k == "NullLit":
            return push(None)
        if k == "Column":
            return push(env[n.name])
        if k == "Compare":
            return f"({render(n.left)} {n.op} {render(n.right)})"
        if k == "IsNull":
            op = "IS NOT NULL" if n.negate else "IS NULL"
            return f"({render(n.operand)} {op})"
        if k == "InList":
            operand_sql = render(n.operand)
            rendered_elems = [render(e) for e in n.elements]
            elems = ", ".join(rendered_elems)
            kw = "NOT IN" if n.negate else "IN"
            return f"({operand_sql} {kw} ({elems}))"
        if k == "NotExpr":
            return f"(NOT {render(n.operand)})"
        if k == "BinBool":
            return f"({render(n.left)} {n.op} {render(n.right)})"
        raise ValueError(k)

    expression = render(node)
    assert expression.count("?") == len(params), "占位符数量与绑定参数数量不一致"
    return expression, tuple(params)
