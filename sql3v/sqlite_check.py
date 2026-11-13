"""SQLite 交叉核对。

对每个赋值打开一个独立的 in-memory SQLite 库，仅执行两条受控语句：
  SELECT (参数化表达式)        —— 取三值结果（1/0/NULL）
  SELECT 1 WHERE (参数化表达式) —— 核对 WHERE 是否收录（仅 TRUE 收录）
不创建表、不运行任何 DDL/PRAGMA/其他 SQL；用户文本绝不进入 SQL 字符串。
"""

from __future__ import annotations

import sqlite3

from . import ast_nodes as ast
from .codegen import parameterized_query
from .errors import VerifyError
from .trit import Trit


def _sqlite_to_trit(row_value: object) -> Trit:
    if row_value == 1:
        return Trit.TRUE
    if row_value == 0:
        return Trit.FALSE
    if row_value is None:
        return Trit.UNKNOWN
    raise VerifyError(f"SQLite 返回了意外的表达式结果 {row_value!r}")


def sqlite_probe(tree: ast.Expr, env: dict[str, int | None]) -> tuple[Trit, bool]:
    """在全新内存库上核对单个赋值，返回（三值结果, WHERE 是否收录）。"""
    expr_sql, params = parameterized_query(tree, env)

    con = sqlite3.connect(":memory:")
    try:
        row = con.execute(f"SELECT {expr_sql}", params).fetchone()
        if row is None or len(row) != 1:
            raise VerifyError("SQLite 标量 SELECT 未返回单列结果")
        trit = _sqlite_to_trit(row[0])

        where_row = con.execute(f"SELECT 1 WHERE {expr_sql}", params).fetchone()
        accepted = where_row is not None
    finally:
        con.close()

    if accepted != (trit is Trit.TRUE):
        raise VerifyError(
            f"SQLite WHERE 收录情况 {accepted} 与表达式三值结果 {trit.value} 不一致"
        )
    return trit, accepted
