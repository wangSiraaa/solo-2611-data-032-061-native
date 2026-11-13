"""问题数据模型与输入校验。

输入 JSON 结构::

    {
      "tables": [{"table_id": "A", "rows": 1000}, ...],
      "conditions": [{"left": "A", "right": "B", "selectivity": "0.1"}, ...]
    }

条件是无向的，内部统一按 Unicode 码点序把端点存成 (low, high)，
因此输入顺序（表顺序、条件顺序、端点左右方向）不影响任何结果。
"""

from dataclasses import dataclass
from fractions import Fraction
from typing import Mapping

from .decimalx import decimal_to_fraction
from .errors import InputValidationError


@dataclass(frozen=True)
class Condition:
    left: str          # 已按码点序规范化：left < right
    right: str
    selectivity: Fraction

    @property
    def edge(self) -> tuple[str, str]:
        return self.left, self.right


@dataclass(frozen=True)
class Problem:
    table_ids: tuple[str, ...]
    rows: Mapping[str, int]
    conditions: tuple[Condition, ...]

    @property
    def n(self) -> int:
        return len(self.table_ids)

    def cond_map(self) -> dict[tuple[str, str], Condition]:
        return {c.edge: c for c in self.conditions}

    def selectivities_between(self, table: str, joined: frozenset[str]) -> tuple[Condition, ...]:
        """table 与已加入集合 joined 之间的全部条件，按边键码点序排列。"""
        out: list[Condition] = []
        for cond in self.conditions:
            if cond.left == table and cond.right in joined:
                out.append(cond)
            elif cond.right == table and cond.left in joined:
                out.append(cond)
        return tuple(sorted(out, key=lambda c: c.edge))


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise InputValidationError(message)


def parse_problem(payload: object) -> Problem:
    """校验并解析输入 JSON；任何违规都抛 InputValidationError。"""
    _require(isinstance(payload, dict), "输入必须是 JSON 对象")
    _require("tables" in payload, "缺少字段 'tables'")
    raw_tables = payload["tables"]
    _require(isinstance(raw_tables, list), "'tables' 必须是数组")
    _require(1 <= len(raw_tables) <= 8, "表数量必须在 1 到 8 之间")

    table_ids: list[str] = []
    rows: dict[str, int] = {}
    for i, item in enumerate(raw_tables):
        _require(isinstance(item, dict), f"tables[{i}] 必须是对象")
        tid = item.get("table_id")
        _require(isinstance(tid, str) and tid != "", f"tables[{i}].table_id 必须是非空字符串")
        _require(tid not in rows, f"table_id 重复: {tid!r}")
        nrows = item.get("rows")
        # bool 是 int 的子类，显式排除
        _require(
            isinstance(nrows, int) and not isinstance(nrows, bool) and nrows >= 0,
            f"tables[{i}].rows 必须是非负整数，收到 {nrows!r}",
        )
        table_ids.append(tid)
        rows[tid] = nrows

    raw_conds = payload.get("conditions", [])
    _require(isinstance(raw_conds, list), "'conditions' 必须是数组")
    seen_edges: set[tuple[str, str]] = set()
    conditions: list[Condition] = []
    for i, item in enumerate(raw_conds):
        _require(isinstance(item, dict), f"conditions[{i}] 必须是对象")
        left, right = item.get("left"), item.get("right")
        _require(isinstance(left, str) and isinstance(right, str),
                 f"conditions[{i}] 的 left/right 必须是字符串")
        _require(left != right, f"conditions[{i}] 不允许自连接: {left!r}")
        _require(left in rows and right in rows,
                 f"conditions[{i}] 引用了未知表: {left!r}, {right!r}")
        edge = tuple(sorted((left, right)))
        _require(edge not in seen_edges,
                 f"表对 {edge[0]!r} 与 {edge[1]!r} 之间存在重复条件")
        sel = decimal_to_fraction(item.get("selectivity"), field=f"conditions[{i}].selectivity")
        _require(Fraction(0) <= sel <= Fraction(1),
                 f"conditions[{i}].selectivity 必须在 [0, 1] 之间，收到 {item.get('selectivity')!r}")
        seen_edges.add(edge)
        conditions.append(Condition(edge[0], edge[1], sel))

    # 表 id 按码点序规范化存放，保证后续枚举与输出不受输入顺序影响
    canonical_ids = tuple(sorted(table_ids))
    conditions.sort(key=lambda c: c.edge)
    return Problem(canonical_ids, dict(rows), tuple(conditions))


def validate_order(problem: Problem, order: object) -> tuple[str, ...]:
    """校验用户给定顺序：必须是恰好覆盖全部表的排列。"""
    from .errors import UserOrderError

    if not isinstance(order, list) or not all(isinstance(x, str) for x in order):
        raise UserOrderError("用户顺序必须是 table_id 字符串数组")
    if len(order) != problem.n or set(order) != set(problem.table_ids) or len(set(order)) != len(order):
        raise UserOrderError(
            f"用户顺序必须是全部 {problem.n} 张表的无重复排列，收到 {order!r}"
        )
    return tuple(order)
