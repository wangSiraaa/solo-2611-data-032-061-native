"""命令行入口：generate / compare / explain / verify。

用法见模块文档或 `python -m sql3v --help`。
所有命令只读取本地声明文件，不连接任何业务数据库。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .analysis import parse_both, run
from .codegen import generate
from .domain import Spec, load_spec_file
from .errors import Sql3vError
from .evaluator import Evaluator
from .lexer import INT64_MAX, INT64_MIN
from .render import (compare_to_json, render_compare_text, sexpr, token_list)
from .trit import AND_TABLE, NOT_TABLE, OR_TABLE, Trit, where_accepts


# ---------------------------------------------------------------------------
# 赋值参数解析（explain 使用）
# ---------------------------------------------------------------------------

def _parse_assign(text: str | None, spec: Spec) -> dict[str, int | None]:
    domain = {c.name: set(c.values) for c in spec.columns}
    env: dict[str, int | None] = {}
    if text:
        for piece in text.split(","):
            piece = piece.strip()
            if "=" not in piece:
                raise Sql3vError(f"--assign 片段 {piece!r} 应为 列=值 形式")
            name, raw = piece.split("=", 1)
            name = name.strip()
            raw = raw.strip()
            if name not in domain:
                raise Sql3vError(f"--assign 引用了未声明的列 {name!r}")
            if raw.lower() == "null":
                value: int | None = None
            else:
                try:
                    value = int(raw, 10)
                except ValueError:
                    raise Sql3vError(f"--assign 的值 {raw!r} 不是整数或 null")
                if not INT64_MIN <= value <= INT64_MAX:
                    raise Sql3vError(f"--assign 的值 {raw} 超出有符号 64 位范围")
            if value not in domain[name]:
                raise Sql3vError(
                    f"赋值 {name}={raw} 不在该列声明的允许域内"
                )
            env[name] = value
    # 未显式赋值的列取规范域的第一个值（null 优先，否则最小整数）
    for col in spec.columns:
        if col.name not in env:
            env[col.name] = col.values[0]
    return env


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------

def cmd_generate(args: argparse.Namespace) -> int:
    spec = load_spec_file(args.spec)
    pa, pb = parse_both(spec)
    payload: dict[str, Any] = {
        "note": "SQL 文本仅由已校验 AST 生成；'?' 为绑定参数，用户原文不会被拼接执行。",
        "predicate_a": {
            "text": pa.text,
            "tokens": token_list(pa.tokens),
            "token_count": len(pa.tokens),
            "ast": sexpr(pa.tree),
            "sqlite_expression": generate(pa.tree).expression,
            "literal_params": list(generate(pa.tree).literal_params),
        },
        "predicate_b": {
            "text": pb.text,
            "tokens": token_list(pb.tokens),
            "token_count": len(pb.tokens),
            "ast": sexpr(pb.tree),
            "sqlite_expression": generate(pb.tree).expression,
            "literal_params": list(generate(pb.tree).literal_params),
        },
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    print("受控 SQL 生成结果（不执行；字面量全部参数化）")
    print("=" * 40)
    for label, item in (("A", payload["predicate_a"]), ("B", payload["predicate_b"])):
        print(f"谓词 {label}: {item['text']}")
        print(f"  词法单元数: {item['token_count']}")
        print(f"  AST: {item['ast']}")
        print(f"  SQLite 表达式: {item['sqlite_expression']}")
        print(f"  字面量绑定参数: {item['literal_params']}")
    print("\n说明: 列名仅来自已校验声明；运行期所有值通过 sqlite3 参数绑定传入。")
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    spec = load_spec_file(args.spec)
    result = run(spec, cross_check=args.sqlite)
    if args.json:
        print(json.dumps(compare_to_json(result), ensure_ascii=False, indent=2))
    else:
        print(render_compare_text(result))
    return 0


def cmd_explain(args: argparse.Namespace) -> int:
    spec = load_spec_file(args.spec)
    pa, pb = parse_both(spec)
    env = _parse_assign(args.assign, spec)
    ordered = [c.name for c in spec.canonical_columns()]

    payload: dict[str, Any] = {
        "assignment": {c: env[c] for c in ordered},
        "predicates": {},
    }
    text_lines = [
        "单赋值三值求值说明",
        "=" * 24,
        "赋值: " + ", ".join(
            f"{c}={'NULL' if env[c] is None else env[c]}" for c in ordered
        ),
        "",
    ]
    for label, parsed in (("A", pa), ("B", pb)):
        evaluator = Evaluator(env)
        final = evaluator.eval(parsed.tree)
        steps = []
        from .evaluator import snapshot
        for step in snapshot(parsed.tree, env):
            steps.append({
                "line": step["line"], "col": step["col"],
                "expr": step["expr"], "result": step["result"].value,
            })
        payload["predicates"][label] = {
            "text": parsed.text,
            "ast": sexpr(parsed.tree),
            "final": final.value,
            "where_accepted": where_accepts(final),
            "steps": steps,
        }
        text_lines.append(f"谓词 {label}: {parsed.text}")
        text_lines.append(f"  AST: {sexpr(parsed.tree)}")
        for step in steps:
            text_lines.append(
                f"  行{step['line']}列{step['col']}  {step['expr']} => {step['result']}"
            )
        text_lines.append(f"  最终逻辑值: {final.value}；WHERE 收录: {where_accepts(final)}")
        text_lines.append("")
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print("\n".join(text_lines))
    return 0


def _truth_table_check() -> list[dict[str, Any]]:
    """手工真值表：用字面量谓词在 SQLite 上核对 AND/OR/NOT 全部组合。"""
    cases = []
    for a in Trit:
        expected_not = next(r for (x, r) in NOT_TABLE if x is a)
        cases.append(("NOT", (a,), expected_not))
    for a in Trit:
        for b in Trit:
            expected_and = next(r for (x, y, r) in AND_TABLE if x is a and y is b)
            cases.append(("AND", (a, b), expected_and))
            expected_or = next(r for (x, y, r) in OR_TABLE if x is a and y is b)
            cases.append(("OR", (a, b), expected_or))

    # 用表达式 1=1 / 1=2 / NULL 分别构造 TRUE/FALSE/UNKNOWN
    def atom_sql(t: Trit) -> str:
        return {Trit.TRUE: "(1 = 1)", Trit.FALSE: "(1 = 2)",
                Trit.UNKNOWN: "(NULL = 1)"}[t]

    import sqlite3
    con = sqlite3.connect(":memory:")
    details = []
    try:
        for op, operands, expected in cases:
            if op == "NOT":
                sql = f"SELECT (NOT {atom_sql(operands[0])})"
            else:
                sql = f"SELECT ({atom_sql(operands[0])} {op} {atom_sql(operands[1])})"
            row = con.execute(sql).fetchone()
            got = Trit.TRUE if row[0] == 1 else (Trit.FALSE if row[0] == 0 else Trit.UNKNOWN)
            details.append({
                "operator": op,
                "operands": [t.value for t in operands],
                "expected": expected.value,
                "sqlite_got": got.value,
                "pass": got is expected,
            })
            if got is not expected:
                raise Sql3vError(f"真值表核对失败: {op} {operands} 期望 {expected} 实得 {got}")
    finally:
        con.close()
    return details


def cmd_verify(args: argparse.Namespace) -> int:
    spec = load_spec_file(args.spec)
    pa, pb = parse_both(spec)
    result = run(spec, cross_check=True)  # 每个赋值独立内存库交叉核对

    details = _truth_table_check()
    payload = {
        "assignments_cross_checked": result.traversed,
        "per_assignment_consistent": True,
        "truth_table_cases": len(details),
        "truth_table_all_pass": all(d["pass"] for d in details),
        "truth_table": details,
        "value_equivalent": result.value_equivalent,
        "filter_equivalent": result.filter_equivalent,
        "scope_notice": "结论仅覆盖给定有限域，不声称证明全部整数域上的等价。",
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    print("SQLite 交叉核对（独立内存库 + 绑定参数，仅执行 SELECT 表达式/WHERE）")
    print("=" * 52)
    print(f"逐赋值核对: {result.traversed} 个赋值 × 2 个谓词，三值结果与 WHERE 收录全部一致")
    print(f"手工真值表: NOT {sum(1 for d in details if d['operator'] == 'NOT')} 组、"
          f"AND {sum(1 for d in details if d['operator'] == 'AND')} 组、"
          f"OR {sum(1 for d in details if d['operator'] == 'OR')} 组，全部通过")
    print(f"值等价: {'成立' if result.value_equivalent else '不成立'}；"
          f"筛选等价: {'成立' if result.filter_equivalent else '不成立'}")
    print("声明: 结论仅覆盖给定有限域，不声称证明全部整数域上的等价。")
    return 0


# ---------------------------------------------------------------------------
# argparse 装配
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sql3v",
        description="有限域 SQL 三值谓词核验 CLI（纯本地 SQLite，不连业务库）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_spec(p: argparse.ArgumentParser) -> None:
        p.add_argument("spec", help="声明 JSON 文件路径；'-' 表示从标准输入读取")
        p.add_argument("--json", action="store_true", help="输出机器可读 JSON")

    p_gen = sub.add_parser("generate", help="词法/语法分析、输出 AST 与受控 SQLite 表达式（不执行）")
    add_spec(p_gen)
    p_gen.set_defaults(func=cmd_generate)

    p_cmp = sub.add_parser("compare", help="枚举有限域，判定值等价与筛选等价并给规范反例")
    add_spec(p_cmp)
    p_cmp.add_argument("--sqlite", action="store_true",
                       help="同时对每个赋值做 SQLite 交叉核对（独立内存库）")
    p_cmp.set_defaults(func=cmd_compare)

    p_ex = sub.add_parser("explain", help="对单个赋值逐步解释两个谓词的三值求值")
    add_spec(p_ex)
    p_ex.add_argument("--assign", default=None,
                      help="赋值，形如 x=1,y=NULL；未给列取规范域首值（null 优先）")
    p_ex.set_defaults(func=cmd_explain)

    p_v = sub.add_parser("verify", help="全量 SQLite 交叉核对 + 手工 AND/OR/NOT 真值表")
    add_spec(p_v)
    p_v.set_defaults(func=cmd_verify)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except Sql3vError as exc:
        print(f"错误: {exc.located()}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"错误: 找不到声明文件 {exc.filename!r}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(f"错误: 声明文件不是合法 JSON: {exc.msg}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
