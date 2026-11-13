"""服务端聚合：主指标、按日趋势、类别候选分布、分页明细。

所有聚合与分页都在 SQLite 内完成，浏览器只拿“图表结果 + 当前页”。

两种类别口径必须明确区分：
- distribution：应用完整筛选（含类别自身筛选）。
- candidates：移除类别自身筛选后，在同一地区/日期条件下各候选类别的分布，
  让用户可以继续添加类别。
"""
from __future__ import annotations

import sqlite3
from datetime import timedelta
from typing import Any

from .filters import Filters, build_where

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 200

# 维度值的展示标签：NULL 与空字符串必须一眼可辨。
NULL_LABEL = {"kind": "null", "label": "(空值 NULL)", "display": "NULL"}
EMPTY_LABEL = {"kind": "value", "value": "", "label": "(空字符串)", "display": '""'}


def _label(value: str | None) -> dict[str, Any]:
    if value is None:
        return dict(NULL_LABEL)
    if value == "":
        return dict(EMPTY_LABEL)
    return {"kind": "value", "value": value, "label": value, "display": value}


def _label_for_key(key: tuple[str, str | None]) -> dict[str, Any]:
    return _label(None if key[0] == "null" else key[1])


def _grouped(connection: sqlite3.Connection, sql: str, params: list[Any]) -> list[tuple]:
    return connection.execute(sql, params).fetchall()


def _category_rows(
    connection: sqlite3.Connection,
    filters: Filters,
    *,
    where_sql: str,
    where_params: list[Any],
    include_known: bool,
) -> list[dict[str, Any]]:
    sql = f"""
        SELECT category, COUNT(*) AS row_count,
               COALESCE(SUM(amount_cents), 0) AS amount_cents
        FROM transactions
        WHERE {where_sql}
        GROUP BY category
    """
    raw = _grouped(connection, sql, where_params)

    # 用选中状态决定展示顺序：先按当前筛选选中值，再按值/空值排序；未选值与空值都补零。
    selected = {("null", None) if s.is_null else ("value", s.value) for s in filters.category}
    buckets: dict[tuple[str, str | None], dict[str, Any]] = {}
    for category, row_count, amount_cents in raw:
        key = ("null", None) if category is None else ("value", category)
        buckets[key] = {"row_count": row_count, "amount_cents": amount_cents}

    # 候选口径：列出“移除类别自身筛选”范围内所有可选值（含计数为 0 的候选），
    # 用户因此总能继续增加类别。分布口径只显示实际出现（或被选中）的类别。
    if include_known:
        keys = _known_values(connection, filters, "category") | set(buckets)
    else:
        keys = set(buckets) | selected
    keys = sorted(
        keys,
        key=lambda k: (0 if k in selected else 1, 1 if k[0] == "null" else 0, k[1] or ""),
    )
    if not keys:
        return []

    rows: list[dict[str, Any]] = []
    for key in keys:
        stats = buckets.get(key, {"row_count": 0, "amount_cents": 0})
        rows.append(
            {
                **_label_for_key(key),
                "row_count": stats["row_count"],
                "amount_cents": stats["amount_cents"],
                "selected": key in selected,
            }
        )
    return rows


def _known_values(connection: sqlite3.Connection, filters: Filters, dimension: str) -> set[tuple[str, str | None]]:
    """从“移除该维度自身筛选”的候选中找出该维度的全部可见值。"""
    scope = filters.without(dimension)
    where_sql, params = build_where(scope)
    sql = f"SELECT DISTINCT {dimension} FROM transactions WHERE {where_sql}"
    values = connection.execute(sql, params).fetchall()
    return {("null", None) if v[0] is None else ("value", v[0]) for v in values}


def aggregate(
    connection: sqlite3.Connection,
    filters: Filters,
    *,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    sort: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """一次完整筛选的完整结果集（主指标、趋势、两种类别口径、当前页明细）。"""
    page = max(1, page)
    page_size = max(1, min(page_size, MAX_PAGE_SIZE))
    order_columns = _resolve_sort(sort)

    where_sql, where_params = build_where(filters)

    row_count, amount_cents = connection.execute(
        f"""
        SELECT COUNT(*), COALESCE(SUM(amount_cents), 0)
        FROM transactions WHERE {where_sql}
        """,
        where_params,
    ).fetchone()

    # 按日趋势：用完整筛选，按日补零。
    trend_raw = dict(
        (day, (count, total))
        for day, count, total in _grouped(
            connection,
            f"""
            SELECT business_date, COUNT(*), COALESCE(SUM(amount_cents), 0)
            FROM transactions WHERE {where_sql}
            GROUP BY business_date
            """,
            where_params,
        )
    )
    trend = []
    cursor = filters.date_from
    while cursor < filters.date_to:
        count, total = trend_raw.get(cursor.isoformat(), (0, 0))
        trend.append(
            {
                "date": cursor.isoformat(),
                "row_count": count,
                "amount_cents": total,
            }
        )
        cursor += timedelta(days=1)

    # 类别分布（含类别筛选）与候选（移除类别筛选）使用同一 where 参数集，分别绑定。
    distribution_where, distribution_params = build_where(filters)
    distribution = _category_rows(
        connection,
        filters,
        where_sql=distribution_where,
        where_params=distribution_params,
        include_known=False,
    )

    candidate_where, candidate_params = build_where(filters, exclude_dimension="category")
    candidates = _category_rows(
        connection,
        filters,
        where_sql=candidate_where,
        where_params=candidate_params,
        include_known=True,
    )

    # 稳定排序：先按可配置列，最后总以唯一 record_id 作为裁决键，保证翻页不重不漏。
    order_sql = ", ".join(f"{c} ASC" for c in order_columns)
    offset = (page - 1) * page_size
    rows = connection.execute(
        f"""
        SELECT record_id, business_date, region, category, quantity, amount_cents
        FROM transactions
        WHERE {where_sql}
        ORDER BY {order_sql}
        LIMIT ? OFFSET ?
        """,
        [*where_params, page_size, offset],
    ).fetchall()

    total_pages = (int(row_count) + page_size - 1) // page_size
    detail = [_row_json(row) for row in rows]

    return {
        "filters": filters.as_json(),
        "metrics": {"row_count": int(row_count), "amount_cents": int(amount_cents)},
        "trend": trend,
        "category_distribution": {
            "scope": "完整筛选（含类别自身筛选）",
            "basis": "filters",
            "buckets": distribution,
        },
        "category_candidates": {
            "scope": "移除类别自身筛选后的候选分布",
            "basis": "filters_without_category",
            "buckets": candidates,
        },
        "detail": {
            "page": page,
            "page_size": page_size,
            "total_rows": int(row_count),
            "total_pages": total_pages,
            "sort": list(order_columns),
            "rows": detail,
        },
    }


def _resolve_sort(sort: tuple[str, ...] | None) -> tuple[str, ...]:
    allowed = {"business_date", "record_id", "region", "category", "quantity", "amount_cents"}
    columns = [c for c in (sort or ()) if c in allowed]
    if not columns:
        columns = ["business_date"]
    # 唯一键裁决：始终以 record_id 收尾（去重后仍保留稳定顺序）。
    if "record_id" not in columns:
        columns.append("record_id")
    return tuple(columns)


def _row_json(row: tuple) -> dict[str, Any]:
    record_id, business_date, region, category, quantity, amount_cents = row
    return {
        "record_id": record_id,
        "business_date": business_date,
        "region": _label(region),
        "category": _label(category),
        "quantity": quantity,
        "amount_cents": amount_cents,
    }
