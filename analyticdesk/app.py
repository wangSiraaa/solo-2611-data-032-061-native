"""FastAPI 应用：小型联动分析台。

不变量：
1. 每个请求/响应都带 client 提供的 state_id（筛选状态标识），响应回显同一 state_id，
   并对聚合结果做筛选指纹校验，避免“把不同状态的数值拼成完整页面”。
2. 前端不取消旧请求，而是让旧请求真实完成；用按视图递增的 generation + state_id 决定
   丢弃过期响应。后端提供可控延迟与单部件失败开关，用于真实复现乱序与部分失败。
3. 聚合全部在服务端完成；浏览器只拿图表结果和当前页。
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .aggregations import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, aggregate
from .filters import FilterError, Filters, filter_signature, parse_filters
from .loader import build_database

BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent
FIXTURES = PROJECT_ROOT / "fixtures"
RUNTIME = PROJECT_ROOT / ".runtime"
DB_PATH = RUNTIME / "analyticdesk.sqlite3"

# 可配置的故障注入上限，防止误配置成超长挂起。
MAX_DELAY_MS = 30_000

app = FastAPI(title="小型联动分析台", version="1.0")


class AppState:
    def __init__(self) -> None:
        self.db_path = DB_PATH
        self.spec = None

    def ensure_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        csv_path = Path(os.environ.get("ANALYTICDESK_CSV", FIXTURES / "transactions.csv"))
        columns_path = Path(os.environ.get("ANALYTICDESK_COLUMNS", FIXTURES / "columns.json"))
        self.spec = build_database(csv_path, columns_path, self.db_path)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        return connection


state = AppState()


@app.on_event("startup")
def _startup() -> None:
    state.ensure_db()


def _error(status: int, state_id: str | None, code: str, message: str, **extra: Any) -> JSONResponse:
    payload = {"state_id": state_id, "error": {"code": code, "message": message, **extra}}
    return JSONResponse(payload, status_code=status)


def _state_header(state_id: str | None) -> dict[str, str]:
    # HTTP header 只能是 latin-1；把 state_id 百分号编码，正文仍是原文。
    if not state_id:
        return {}
    from urllib.parse import quote

    return {"X-State-Id": quote(state_id, safe="")}


def _parse_request_state(payload: Any) -> tuple[str | None, Filters]:
    if not isinstance(payload, dict):
        raise FilterError("request body must be an object")
    state_id = payload.get("state_id")
    if state_id is not None and not isinstance(state_id, str):
        raise FilterError("state_id must be a string")
    filters = parse_filters(payload.get("filters"))
    return state_id, filters


def _state_id_hint(payload: Any) -> str | None:
    if isinstance(payload, dict) and isinstance(payload.get("state_id"), str):
        return payload["state_id"]
    return None


def _reject_unknown_keys(payload: dict[str, Any], allowed: set[str]) -> None:
    extra = set(payload) - allowed
    if extra:
        raise FilterError(f"unknown request keys: {sorted(extra)}")


def _parse_response_controls(request: Request) -> tuple[int, set[str]]:
    """从请求里读取延迟与故障开关。

    - 查询参数 ``delay_ms``（或 header ``X-Delay-Ms``）：服务端在返回前等待，用于复现乱序。
    - 查询参数 ``fail``（可重复，或逗号分隔）：让指定部件 500 失败，如 ``fail=category``。
    """
    raw_delay = request.query_params.get("delay_ms") or request.headers.get("x-delay-ms")
    delay_ms = 0
    if raw_delay:
        try:
            delay_ms = max(0, min(int(raw_delay), MAX_DELAY_MS))
        except ValueError as exc:
            raise FilterError("delay_ms must be an integer") from exc

    raw_fail = []
    for value in request.query_params.getlist("fail") + request.headers.getlist("x-fail"):
        raw_fail.extend(part.strip() for part in value.split(",") if part.strip())
    return delay_ms, set(raw_fail)


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True}


@app.get("/api/options")
def options() -> dict[str, Any]:
    """返回可选维度值（含 NULL 与空字符串的明确标记）与日期边界。"""
    connection = state.connect()
    try:
        dates = connection.execute(
            "SELECT MIN(business_date) AS lo, MAX(business_date) AS hi FROM transactions"
        ).fetchone()
        region_values = _distinct(connection, "region")
        category_values = _distinct(connection, "category")
    finally:
        connection.close()
    from datetime import date, timedelta

    lo = date.fromisoformat(dates["lo"])
    hi = date.fromisoformat(dates["hi"]) + timedelta(days=1)  # 右开的默认上界
    return {
        "date_min": lo.isoformat(),
        "date_max": hi.isoformat(),
        "region": region_values,
        "category": category_values,
        "notes": {
            "date_range": "[date_from, date_to) 左闭右开",
            "amount_unit": "integer cents",
            "null_vs_empty": "NULL 与空字符串是两个不同取值",
        },
    }


def _distinct(connection: sqlite3.Connection, dimension: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        f"SELECT {dimension} AS v, COUNT(*) AS n FROM transactions GROUP BY {dimension} ORDER BY n DESC"
    ).fetchall()
    result = []
    for row in rows:
        if row["v"] is None:
            result.append({"kind": "null", "label": "(空值 NULL)", "display": "NULL", "count": row["n"]})
        elif row["v"] == "":
            result.append({"kind": "value", "value": "", "label": "(空字符串)", "display": '""', "count": row["n"]})
        else:
            result.append({"kind": "value", "value": row["v"], "label": row["v"], "display": row["v"], "count": row["n"]})
    return result


async def _maybe_delay(delay_ms: int) -> None:
    if delay_ms > 0:
        await asyncio.sleep(delay_ms / 1000.0)


def _run_query(state_id: str | None, filters: Filters, *, page: int, page_size: int) -> dict[str, Any]:
    connection = state.connect()
    try:
        result = aggregate(connection, filters, page=page, page_size=page_size, sort=state.spec.default_sort)
    finally:
        connection.close()
    fingerprint = filter_signature(filters)
    return {
        "state_id": state_id,
        "filter_fingerprint": fingerprint,
        **result,
    }


@app.post("/api/query")
async def query(request: Request) -> JSONResponse:
    """完整页面查询：主指标、趋势、两种类别口径、当前页明细，同一 state_id。"""
    state_id: str | None = None
    try:
        payload = await request.json()
        state_id = _state_id_hint(payload)  # 即使筛选非法，也尽量回显客户端状态标识
        state_id, filters = _parse_request_state(payload)
        page = int(payload.get("page", 1))
        page_size = int(payload.get("page_size", DEFAULT_PAGE_SIZE))
        if page < 1 or page_size < 1:
            raise FilterError("page and page_size must be >= 1")
        if page_size > MAX_PAGE_SIZE:
            raise FilterError(f"page_size must be <= {MAX_PAGE_SIZE}")
        delay_ms, failures = _parse_response_controls(request)
    except (FilterError, ValueError, json.JSONDecodeError) as exc:
        return _error(400, state_id, "bad_request", str(exc))

    await _maybe_delay(delay_ms)

    # 整页失败：任一部件失败都不拼接部分结果，返回完整错误并携带 state_id。
    if "all" in failures:
        return _error(503, state_id, "forced_failure", "query forced to fail", component="all", retryable=True)

    try:
        result = _run_query(state_id, filters, page=page, page_size=page_size)
    except sqlite3.Error as exc:
        return _error(500, state_id, "db_error", str(exc), retryable=True)

    if failures:
        # 单部件失败：返回 200，但明确标记哪些部件不可用，其余部件照常可用且同属一个 state_id。
        # 失败部件的数据置空，杜绝前端拿旧值或其他口径拼成“完整页面”。
        for name in failures:
            result[name] = None
        component_errors = {
            name: {"code": "forced_failure", "message": f"component {name} forced to fail", "retryable": True}
            for name in failures
        }
        result["component_errors"] = component_errors
        result["partial"] = True
    else:
        result["component_errors"] = {}
        result["partial"] = False

    return JSONResponse(result, headers=_state_header(state_id))


@app.post("/api/component")
async def component(request: Request) -> JSONResponse:
    """单个部件查询：用于图表独立重试，不牵动其余部件。

    component ∈ {metrics, trend, category_distribution, category_candidates, detail}
    """
    state_id: str | None = None
    try:
        payload = await request.json()
        state_id = _state_id_hint(payload)
        state_id, filters = _parse_request_state(payload)
        which = payload.get("component")
        if which not in {
            "metrics",
            "trend",
            "category_distribution",
            "category_candidates",
            "detail",
        }:
            raise FilterError("unknown component")
        page = int(payload.get("page", 1))
        page_size = int(payload.get("page_size", DEFAULT_PAGE_SIZE))
        delay_ms, failures = _parse_response_controls(request)
        _reject_unknown_keys(payload, {"state_id", "filters", "component", "page", "page_size"})
    except (FilterError, ValueError, json.JSONDecodeError) as exc:
        return _error(400, state_id, "bad_request", str(exc))

    await _maybe_delay(delay_ms)
    if which in failures or "all" in failures:
        return _error(503, state_id, "forced_failure", f"component {which} forced to fail", component=which, retryable=True)

    try:
        result = _run_query(state_id, filters, page=page, page_size=page_size)
    except sqlite3.Error as exc:
        return _error(500, state_id, "db_error", str(exc), retryable=True)

    selected = {
        "metrics": result["metrics"],
        "trend": result["trend"],
        "category_distribution": result["category_distribution"],
        "category_candidates": result["category_candidates"],
        "detail": result["detail"],
    }[which]
    return JSONResponse(
        {
            "state_id": state_id,
            "filter_fingerprint": result["filter_fingerprint"],
            "component": which,
            "data": selected,
        },
        headers=_state_header(state_id),
    )


app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


@app.get("/")
def index() -> Any:
    from fastapi.responses import FileResponse

    return FileResponse(BASE_DIR / "static" / "index.html")
