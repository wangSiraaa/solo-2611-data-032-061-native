"""有限域声明的加载与校验。

声明 JSON 结构：
{
  "columns": [
    {"name": "x", "values": [null, 1, -2]},
    ...
  ],
  "predicate_a": "x IS NULL OR x > 0",
  "predicate_b": "NOT (x <= 0)"
}

规则（违反即拒绝，非零退出）：
  * 1..6 个列；列名是普通未加引号标识符，区分大小写，不可重复；
  * 每列 1..5 个允许值，值只能是有符号 64 位整数或显式 null；
  * 同列内值不得重复（null 至多一个）；
  * 全部赋值组合不超过 20000，超过拒绝；
  * 两个谓词文本必填。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from itertools import product
from typing import Any

from .errors import DomainError
from .lexer import INT64_MAX, INT64_MIN

MAX_COLUMNS = 6
MAX_VALUES_PER_COLUMN = 5
MAX_ASSIGNMENTS = 20_000
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# 即便语法上是标识符，SQL 保留字也不允许当列名，避免后续受控 SQL 歧义。
_RESERVED = {
    "NULL", "IS", "IN", "NOT", "AND", "OR", "TRUE", "FALSE", "UNKNOWN",
    "SELECT", "FROM", "WHERE", "BETWEEN", "LIKE", "CASE", "WHEN", "THEN",
    "ELSE", "END", "AS", "ON", "JOIN", "BY", "ORDER", "GROUP", "HAVING",
}


@dataclass(frozen=True)
class DomainColumn:
    name: str
    # 规范顺序：null 在前，其余整数升序（与声明书写顺序无关）
    values: tuple[int | None, ...]

    @staticmethod
    def normalized(name: str, values: tuple[int | None, ...] | list[int | None]) -> "DomainColumn":
        """无论输入书写顺序如何，都规范化为 null 在前、整数升序。"""
        ordered: list[int | None] = sorted(v for v in values if v is not None)
        if any(v is None for v in values):
            ordered = [None] + ordered  # type: ignore[list-item]
        return DomainColumn(name, tuple(ordered))


@dataclass(frozen=True)
class Spec:
    columns: tuple[DomainColumn, ...]
    predicate_a: str
    predicate_b: str
    raw: dict[str, Any]

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns)

    @property
    def assignment_count(self) -> int:
        total = 1
        for c in self.columns:
            total *= len(c.values)
        return total

    def canonical_columns(self) -> tuple[DomainColumn, ...]:
        """按列名字典序排列的列（反例选择与输出均以此为准）。"""
        return tuple(sorted(self.columns, key=lambda c: c.name))

    def enumerate_assignments(self) -> list[dict[str, int | None]]:
        """按规范顺序枚举全部赋值：列名字典序；每列 null 在前、整数升序。"""
        cols = self.canonical_columns()
        result: list[dict[str, int | None]] = []
        for combo in product(*(c.values for c in cols)):
            result.append({c.name: v for c, v in zip(cols, combo)})
        return result


def _canonical_values(values: list[Any], col_name: str) -> tuple[int | None, ...]:
    seen: set[int] = set()
    null_seen = False
    for v in values:
        if v is None:
            if null_seen:
                raise DomainError(f"列 {col_name!r} 的允许值重复: null")
            null_seen = True
        elif isinstance(v, bool) or not isinstance(v, int):
            raise DomainError(
                f"列 {col_name!r} 的允许值只能是有符号 64 位整数或显式 null，"
                f"发现非法值 {v!r}"
            )
        else:
            if not INT64_MIN <= v <= INT64_MAX:
                raise DomainError(f"列 {col_name!r} 的值 {v} 超出有符号 64 位范围")
            if v in seen:
                raise DomainError(f"列 {col_name!r} 的允许值重复: {v}")
            seen.add(v)
    ints = sorted(seen)
    return tuple(([None] if null_seen else []) + ints)  # type: ignore[list-item]


def load_spec(data: Any) -> Spec:
    if not isinstance(data, dict):
        raise DomainError("声明文件顶层必须是 JSON 对象")
    if "columns" not in data or "predicate_a" not in data or "predicate_b" not in data:
        raise DomainError("声明必须包含 columns、predicate_a、predicate_b 三个字段")

    raw_cols = data["columns"]
    if not isinstance(raw_cols, list) or not raw_cols:
        raise DomainError("columns 必须是非空数组")
    if len(raw_cols) > MAX_COLUMNS:
        raise DomainError(f"列数 {len(raw_cols)} 超过上限 {MAX_COLUMNS}")

    columns: list[DomainColumn] = []
    names: set[str] = set()
    for idx, item in enumerate(raw_cols):
        if not isinstance(item, dict) or "name" not in item or "values" not in item:
            raise DomainError(f"columns[{idx}] 必须是含 name 和 values 的对象")
        name = item["name"]
        if not isinstance(name, str) or not _IDENT_RE.match(name):
            raise DomainError(
                f"columns[{idx}] 的列名 {name!r} 不合法：只能是未加引号的普通标识符 "
                "（字母或下划线开头，仅含字母、数字、下划线）"
            )
        if name.upper() in _RESERVED:
            raise DomainError(f"列名 {name!r} 是 SQL 保留字，请换用普通标识符")
        if name in names:
            raise DomainError(f"列名重复: {name!r}（列名区分大小写）")
        values = item["values"]
        if not isinstance(values, list) or not values:
            raise DomainError(f"列 {name!r} 必须声明 1..{MAX_VALUES_PER_COLUMN} 个非空允许值列表")
        if len(values) > MAX_VALUES_PER_COLUMN:
            raise DomainError(
                f"列 {name!r} 的允许值个数 {len(values)} 超过上限 {MAX_VALUES_PER_COLUMN}"
            )
        canon = _canonical_values(values, name)
        names.add(name)
        columns.append(DomainColumn(name, canon))

    for key in ("predicate_a", "predicate_b"):
        text = data[key]
        if not isinstance(text, str) or not text.strip():
            raise DomainError(f"{key} 必须是非空字符串")
        if len(text) > 4000:
            raise DomainError(f"{key} 文本过长")

    spec = Spec(tuple(columns), data["predicate_a"], data["predicate_b"], data)
    if spec.assignment_count > MAX_ASSIGNMENTS:
        raise DomainError(
            f"全部赋值组合数 {spec.assignment_count} 超过上限 {MAX_ASSIGNMENTS}，已拒绝"
        )
    return spec


def load_spec_file(path: str) -> Spec:
    if path == "-":
        import sys
        text = sys.stdin.read()
    else:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DomainError(f"声明文件不是合法 JSON: {exc.msg}（第 {exc.lineno} 行第 {exc.colno} 列）")
    return load_spec(data)
