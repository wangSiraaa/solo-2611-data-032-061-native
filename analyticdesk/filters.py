"""把客户端筛选状态解析成明确的 SQL 条件。

关键语义：
- 空值(NULL, 序列化为 {"kind": "null"})与空字符串(序列化为 {"kind": "value", "value": ""})
  是两个不同的维度值，各自用 ``IS NULL`` / ``= ''`` 精确匹配。
- 同维度多选取并集，不同维度取交集。
- 日期为 [date_from, date_to) 的左闭右开区间。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

NULL_KIND = "null"
VALUE_KIND = "value"

DIMENSIONS = ("region", "category")


class FilterError(ValueError):
    """筛选状态非法（如未知维度、坏日期、重复维值）。"""


@dataclass(frozen=True)
class DimSelection:
    """某个维度选中的一个值：NULL 或具体字符串（可能是空字符串）。"""

    is_null: bool
    value: str | None = None

    def as_json(self) -> dict[str, Any]:
        if self.is_null:
            return {"kind": NULL_KIND}
        return {"kind": VALUE_KIND, "value": self.value}

    def sql(self, column: str) -> str:
        # column is a whitelisted identifier, not user data.
        if self.is_null:
            return f"{column} IS NULL"
        # 用参数占位符防止引号注入；值由调用方绑定的参数提供。
        return f"{column} = ?"

    def bind_value(self) -> str | None:
        return None if self.is_null else self.value

    def sort_key(self) -> tuple[int, str]:
        return (0, "") if self.is_null else (1, self.value or "")


@dataclass(frozen=True)
class Filters:
    date_from: date
    date_to: date
    region: tuple[DimSelection, ...] = ()
    category: tuple[DimSelection, ...] = ()

    def selections_for(self, dimension: str) -> tuple[DimSelection, ...]:
        if dimension == "region":
            return self.region
        if dimension == "category":
            return self.category
        raise FilterError(f"unknown dimension {dimension!r}")

    def without(self, dimension: str) -> "Filters":
        """返回把某维度自身筛选清空后的筛选（用于类别候选分布）。"""
        if dimension == "region":
            return Filters(self.date_from, self.date_to, (), self.category)
        if dimension == "category":
            return Filters(self.date_from, self.date_to, self.region, ())
        raise FilterError(f"unknown dimension {dimension!r}")

    def as_json(self) -> dict[str, Any]:
        return {
            "date_from": self.date_from.isoformat(),
            "date_to": self.date_to.isoformat(),
            "region": [s.as_json() for s in self.region],
            "category": [s.as_json() for s in self.category],
        }


def _parse_date(text: Any, field: str) -> date:
    if not isinstance(text, str):
        raise FilterError(f"{field} must be a YYYY-MM-DD string")
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError as exc:
        raise FilterError(f"{field} must be YYYY-MM-DD: {exc}") from exc


def _parse_selection(raw: Any, dimension: str) -> DimSelection:
    if not isinstance(raw, dict):
        raise FilterError(f"{dimension} selection must be an object")
    kind = raw.get("kind")
    if kind == NULL_KIND:
        return DimSelection(is_null=True)
    if kind == VALUE_KIND:
        value = raw.get("value")
        if not isinstance(value, str):
            raise FilterError(f"{dimension} value must be a string")
        return DimSelection(is_null=False, value=value)
    raise FilterError(f"{dimension} selection kind must be 'null' or 'value'")


def parse_filters(payload: Any) -> Filters:
    if not isinstance(payload, dict):
        raise FilterError("filters must be an object")
    date_from = _parse_date(payload.get("date_from"), "date_from")
    date_to = _parse_date(payload.get("date_to"), "date_to")
    if date_from > date_to:
        raise FilterError("date_to must not be earlier than date_from ([from, to), right-open)")
    # date_from == date_to 是合法的空区间，不静默扩展成一天。

    parsed: dict[str, tuple[DimSelection, ...]] = {}
    for dimension in DIMENSIONS:
        raw_list = payload.get(dimension, [])
        if raw_list is None:
            raw_list = []
        if not isinstance(raw_list, list):
            raise FilterError(f"{dimension} must be a list")
        seen: set[tuple[bool, str | None]] = set()
        selections: list[DimSelection] = []
        for raw in raw_list:
            sel = _parse_selection(raw, dimension)
            key = (sel.is_null, sel.value)
            if key in seen:
                continue
            seen.add(key)
            selections.append(sel)
        parsed[dimension] = tuple(sorted(selections, key=lambda s: s.sort_key()))

    return Filters(
        date_from=date_from,
        date_to=date_to,
        region=parsed["region"],
        category=parsed["category"],
    )


def filter_signature(filters: Filters) -> str:
    """规范化、稳定的筛选指纹，用于校验聚合结果与筛选一致。"""
    return json.dumps(filters.as_json(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def build_where(filters: Filters, *, exclude_dimension: str | None = None) -> tuple[str, list[Any]]:
    """构造 WHERE 子句与其参数。exclude_dimension 用于“移除该维度自身筛选”。"""
    clauses = ["business_date >= ?", "business_date < ?"]
    params: list[Any] = [filters.date_from.isoformat(), filters.date_to.isoformat()]

    for dimension in DIMENSIONS:
        if dimension == exclude_dimension:
            continue
        selections = filters.selections_for(dimension)
        if not selections:
            continue
        ors = []
        for sel in selections:
            ors.append(sel.sql(dimension))
            if not sel.is_null:
                params.append(sel.bind_value())
        clauses.append("(" + " OR ".join(ors) + ")")

    return " AND ".join(clauses), params
