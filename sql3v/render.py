"""AST 的 S-表达式渲染、文本报告与 JSON 序列化。"""

from __future__ import annotations

from typing import Any

from . import ast_nodes as ast
from .analysis import CompareResult, Counterexample
from .codegen import generate
from .trit import Trit


# ---------------------------------------------------------------------------
# AST
# ---------------------------------------------------------------------------

def sexpr(node: ast.Node) -> str:
    k = node.kind
    if k == "IntLit":
        return str(node.value)
    if k == "NullLit":
        return "NULL"
    if k == "Column":
        return node.name
    if k == "Compare":
        return f"({node.op} {sexpr(node.left)} {sexpr(node.right)})"
    if k == "IsNull":
        tag = "is-not-null" if node.negate else "is-null"
        return f"({tag} {sexpr(node.operand)})"
    if k == "InList":
        tag = "not-in" if node.negate else "in"
        return f"({tag} {sexpr(node.operand)} [{' '.join(sexpr(e) for e in node.elements)}])"
    if k == "NotExpr":
        return f"(not {sexpr(node.operand)})"
    if k == "BinBool":
        return f"({node.op.lower()} {sexpr(node.left)} {sexpr(node.right)})"
    raise ValueError(k)


def token_list(tokens: tuple[Any, ...]) -> list[dict[str, object]]:
    return [
        {"kind": t.kind, "text": t.text, "line": t.line, "col": t.col}
        for t in tokens
    ]


# ---------------------------------------------------------------------------
# 反例
# ---------------------------------------------------------------------------

def _format_vector(ce: Counterexample) -> str:
    return "(" + ", ".join(f"{c}={v}" for c, v in zip(ce.ordered_columns, ce.vector())) + ")"


def _format_steps(steps: tuple[dict[str, object], ...]) -> list[str]:
    lines = []
    for step in steps:
        t: Trit = step["result"]  # type: ignore[assignment]
        lines.append(f"    行{step['line']}列{step['col']}  {step['expr']} => {t.value}")
    return lines


def _counterexample_block(title: str, ce: Counterexample) -> list[str]:
    lines = [
        f"  {title}:",
        f"    最小反例赋值向量: {_format_vector(ce)}",
        f"    谓词 A 最终结果: {ce.result_a.value}",
        f"    谓词 B 最终结果: {ce.result_b.value}",
        "    关键子表达式（A）:",
    ]
    lines += _format_steps(ce.steps_a)
    lines.append("    关键子表达式（B）:")
    lines += _format_steps(ce.steps_b)
    return lines


# ---------------------------------------------------------------------------
# 文本报告
# ---------------------------------------------------------------------------

def render_compare_text(result: CompareResult) -> str:
    spec = result.spec
    lines: list[str] = []
    lines.append("有限域三值谓词核验报告")
    lines.append("=" * 32)
    lines.append(f"列声明（规范序，null 在前、整数升序）:")
    for col in spec.canonical_columns():
        vals = ["NULL" if v is None else str(v) for v in col.values]
        lines.append(f"  {col.name}: {{{', '.join(vals)}}}")
    lines.append(f"遍历赋值组合数: {result.traversed}（上限 20000）")
    lines.append("")
    for label, parsed, report in (
        ("A", result.parsed_a, result.report_a),
        ("B", result.parsed_b, result.report_b),
    ):
        c = report.counts
        lines.append(f"谓词 {label}: {parsed.text}")
        lines.append(
            f"  逻辑值计数: TRUE={c['TRUE']}  FALSE={c['FALSE']}  "
            f"UNKNOWN={c['UNKNOWN']}；WHERE 收录(仅 TRUE)={report.where_count}"
        )
    lines.append("")
    lines.append("等价结论（仅针对上述有限域，不代表全整数域）:")
    lines.append(f"  值等价（TRUE/FALSE/UNKNOWN 逐项相同）: "
                 f"{'成立' if result.value_equivalent else '不成立'}")
    if not result.value_equivalent:
        assert result.value_counterexample is not None
        lines += _counterexample_block("值等价最小反例", result.value_counterexample)
    lines.append(f"  筛选等价（仅比较 WHERE 是否收录 TRUE）: "
                 f"{'成立' if result.filter_equivalent else '不成立'}")
    if not result.filter_equivalent:
        assert result.filter_counterexample is not None
        lines += _counterexample_block("筛选等价最小反例", result.filter_counterexample)
    if result.sqlite_verified:
        lines.append("")
        lines.append("SQLite 交叉核对: 全部赋值的三值结果与 WHERE 收录均一致（独立内存库、绑定参数）。")
    lines.append("")
    lines.append("声明: 结论仅覆盖给定有限域内的赋值，不声称证明全部整数域上的等价。")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------

def _ce_to_json(ce: Counterexample) -> dict[str, Any]:
    return {
        "ordered_columns": list(ce.ordered_columns),
        "assignment": {c: ce.assignment[c] for c in ce.ordered_columns},
        "vector": list(ce.vector()),
        "result_a": ce.result_a.value,
        "result_b": ce.result_b.value,
        "steps_a": [
            {"line": s["line"], "col": s["col"], "expr": s["expr"],
             "result": s["result"].value}  # type: ignore[union-attr]
            for s in ce.steps_a
        ],
        "steps_b": [
            {"line": s["line"], "col": s["col"], "expr": s["expr"],
             "result": s["result"].value}  # type: ignore[union-attr]
            for s in ce.steps_b
        ],
    }


def compare_to_json(result: CompareResult) -> dict[str, Any]:
    return {
        "traversed_assignments": result.traversed,
        "predicate_a": {
            "text": result.parsed_a.text,
            "token_count": len(result.parsed_a.tokens),
            "ast": sexpr(result.parsed_a.tree),
            "counts": result.report_a.counts,
            "where_accepted": result.report_a.where_count,
            "sqlite_template": generate(result.parsed_a.tree).expression,
        },
        "predicate_b": {
            "text": result.parsed_b.text,
            "token_count": len(result.parsed_b.tokens),
            "ast": sexpr(result.parsed_b.tree),
            "counts": result.report_b.counts,
            "where_accepted": result.report_b.where_count,
            "sqlite_template": generate(result.parsed_b.tree).expression,
        },
        "value_equivalence": {
            "equivalent": result.value_equivalent,
            "counterexample": None if result.value_counterexample is None
            else _ce_to_json(result.value_counterexample),
        },
        "filter_equivalence": {
            "equivalent": result.filter_equivalent,
            "counterexample": None if result.filter_counterexample is None
            else _ce_to_json(result.filter_counterexample),
        },
        "sqlite_cross_checked": result.sqlite_verified,
        "scope_notice": "结论仅覆盖给定有限域，不声称证明全部整数域上的等价。",
    }
