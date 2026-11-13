#!/usr/bin/env python3
"""独立核验脚本：只用标准库逐行扫描 CSV 重算，再与 HTTP 服务对照。

不导入应用代码，也不复用其筛选实现 —— 以同一份声明子集的真实输入为唯一事实来源。

覆盖：
1. 两种类别口径（完整筛选 / 移除类别自身筛选）。
2. 精确金额（整数分）与条数。
3. 所有明细：稳定排序、唯一记录号裁决、翻页不重不漏。
4. 多选并集、跨维度交集。
5. NULL 与空字符串区分、日期左闭右开边界。
6. state_id 在请求与响应中的回显。
7. 服务端可控延迟下的乱序结果一致性（同一 state 只接受自己那份）。
8. 单部件失败时明确标记未完成，且不拼接其它状态的数值。

用法：
    python tests/verify.py --base-url http://127.0.0.1:8000
    python tests/verify.py --csv fixtures/transactions.csv --columns fixtures/columns.json --base-url ...
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

FAILS: list[str] = []
CHECKS = 0


def check(condition: bool, message: str) -> None:
    global CHECKS
    CHECKS += 1
    if not condition:
        FAILS.append(message)
        print(f"  FAIL  {message}")


def section(name: str) -> None:
    print(f"\n== {name}")


# --------------------------------------------------------------------------- #
# 纯 Python 参考实现（逐行扫描，不复用被测代码）
# --------------------------------------------------------------------------- #

NULL = object()
EMPTY = ""


@dataclass(frozen=True)
class Row:
    record_id: str
    business_date: str
    region: Any  # str | EMPTY | NULL
    category: Any
    quantity: int
    amount_cents: int


def load_rows(csv_path: Path, null_marker: str) -> list[Row]:
    rows = []
    with csv_path.open(newline="", encoding="utf-8") as handle:
        for raw in csv.DictReader(handle):
            rows.append(
                Row(
                    record_id=raw["record_id"],
                    business_date=raw["business_date"],
                    region=NULL if raw["region"] == null_marker else raw["region"],
                    category=NULL if raw["category"] == null_marker else raw["category"],
                    quantity=int(raw["quantity"]),
                    amount_cents=int(raw["amount_cents"]),
                )
            )
    return rows


def match_dim(value: Any, selections: list[Any] | None) -> bool:
    """selections=None 表示该维度不设筛选（全部）。"""
    if not selections:
        return True
    for sel in selections:
        if sel is NULL and value is NULL:
            return True
        if sel is not NULL and value is not NULL and sel == value:
            return True
    return False


def ref_select(
    rows: list[Row],
    date_from: str,
    date_to: str,
    regions: list[Any] | None,
    categories: list[Any] | None,
) -> list[Row]:
    out = [
        row
        for row in rows
        if date_from <= row.business_date < date_to
        and match_dim(row.region, regions)
        and match_dim(row.category, categories)
    ]
    out.sort(key=lambda r: (r.business_date, r.record_id))
    return out


def ref_metrics(rows: list[Row]) -> dict[str, int]:
    return {"row_count": len(rows), "amount_cents": sum(r.amount_cents for r in rows)}


def ref_trend(rows: list[Row], date_from: str, date_to: str) -> list[dict[str, Any]]:
    by_day: dict[str, list[Row]] = {}
    for row in rows:
        by_day.setdefault(row.business_date, []).append(row)
    out = []
    cursor = date.fromisoformat(date_from)
    end = date.fromisoformat(date_to)
    while cursor < end:
        key = cursor.isoformat()
        bucket = by_day.get(key, [])
        out.append(
            {
                "date": key,
                "row_count": len(bucket),
                "amount_cents": sum(r.amount_cents for r in bucket),
            }
        )
        cursor += timedelta(days=1)
    return out


def ref_category_buckets(rows: list[Row]) -> dict[Any, dict[str, int]]:
    buckets: dict[Any, dict[str, int]] = {}
    for row in rows:
        key = "NULL" if row.category is NULL else ("EMPTY" if row.category == "" else row.category)
        entry = buckets.setdefault(key, {"row_count": 0, "amount_cents": 0})
        entry["row_count"] += 1
        entry["amount_cents"] += row.amount_cents
    return buckets


def key_of(bucket: dict[str, Any]) -> Any:
    if bucket["kind"] == "null":
        return "NULL"
    return "EMPTY" if bucket["value"] == "" else bucket["value"]


def http_json(url: str, payload: dict | None = None, headers: dict | None = None) -> tuple[int, dict]:
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:  # 4xx/5xx 也要能读 body
        return exc.code, json.loads(exc.read().decode("utf-8"))


def sel_json(value: Any) -> dict[str, Any]:
    if value is NULL:
        return {"kind": "null"}
    return {"kind": "value", "value": value}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", default="fixtures/transactions.csv")
    parser.add_argument("--columns", default="fixtures/columns.json")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    columns = json.loads(Path(args.columns).read_text(encoding="utf-8"))
    rows = load_rows(Path(args.csv), columns["null_marker"])
    print(f"CSV: {len(rows)} rows")

    # 数据自检：唯一键、空值标记、空字符串并存。
    ids = [r.record_id for r in rows]
    check(len(set(ids)) == len(ids), "record_id 必须唯一（分页裁决前提）")
    check(any(r.region is NULL for r in rows), "数据应包含地区 NULL")
    check(any(r.region == "" for r in rows), "数据应包含地区空字符串")
    check(any(r.category is NULL for r in rows), "数据应包含类别 NULL")
    check(any(r.amount_cents < 0 for r in rows), "数据应包含负金额")

    date_min = min(r.business_date for r in rows)
    date_max = max(r.business_date for r in rows)
    date_to_default = (date.fromisoformat(date_max) + timedelta(days=1)).isoformat()
    print(f"日期范围: {date_min} .. {date_max} → 右开上界 {date_to_default}")

    region_values = sorted(
        {r.region for r in rows if r.region is not NULL and r.region != ""}
    )
    category_values = sorted(
        {r.category for r in rows if r.category is not NULL and r.category != ""}
    )

    # ------------------------------------------------------------------ #
    section("A. 全量对照：指标 / 趋势 / 明细")
    full = ref_select(rows, date_min, date_to_default, None, None)
    status, resp = http_json(
        f"{base}/api/query",
        {
            "state_id": "verify-full",
            "filters": {
                "date_from": date_min,
                "date_to": date_to_default,
                "region": [],
                "category": [],
            },
            "page": 1,
            "page_size": 100,
        },
    )
    check(status == 200, f"全量查询应 200，得到 {status}")
    check(resp.get("state_id") == "verify-full", "响应必须回显请求的 state_id")
    check(resp.get("partial") is False, "未注入故障时不应标记 partial")
    check(resp["metrics"] == ref_metrics(full), f"全量指标不符：{resp['metrics']} vs {ref_metrics(full)}")
    check(resp["trend"] == ref_trend(full, date_min, date_to_default), "全量按日趋势不符（含补零）")

    # 明细逐行精确对照（翻页覆盖全部记录，不重不漏）。
    page_size = 37
    seen: list[str] = []
    check(resp["detail"]["total_rows"] == len(full), "明细 total_rows 不符")
    check(resp["detail"]["page_size"] == 100, "全量查询应回显 page_size=100")
    expected_pages = (len(full) + page_size - 1) // page_size
    check(resp["detail"]["total_pages"] == (len(full) + 99) // 100, "total_pages 应按请求的 page_size 计算")
    for page in range(1, expected_pages + 1):
        status, page_resp = http_json(
            f"{base}/api/query",
            {
                "state_id": f"verify-page-{page}",
                "filters": {
                    "date_from": date_min,
                    "date_to": date_to_default,
                    "region": [],
                    "category": [],
                },
                "page": page,
                "page_size": page_size,
            },
        )
        check(status == 200, f"第 {page} 页应 200")
        check(page_resp["state_id"] == f"verify-page-{page}", f"第 {page} 页 state_id 回显不符")
        seen.extend(row["record_id"] for row in page_resp["detail"]["rows"])

    check(len(seen) == len(full), f"翻页记录数不符：{len(seen)} vs {len(full)}")
    check(len(set(seen)) == len(seen), "翻页出现重复记录")
    check(seen == [r.record_id for r in full], "翻页顺序与参考稳定排序（日期→记录号）不一致")

    # 逐行核对字段（取全量一次拉齐）。
    status, dump = http_json(
        f"{base}/api/query",
        {
            "state_id": "verify-detail",
            "filters": {
                "date_from": date_min,
                "date_to": date_to_default,
                "region": [],
                "category": [],
            },
            "page": 1,
            "page_size": 200,
        },
    )
    check(len(dump["detail"]["rows"]) == 200, "单页最多 200 条")
    for expected, actual in zip(full[:200], dump["detail"]["rows"]):
        check(actual["record_id"] == expected.record_id, "记录号不符")
        check(actual["business_date"] == expected.business_date, "日期字段不符")
        check(actual["quantity"] == expected.quantity, "数量字段不符")
        check(actual["amount_cents"] == expected.amount_cents, f"金额字段不符 {actual['amount_cents']} vs {expected.amount_cents}")
        exp_region = (
            {"kind": "null"} if expected.region is NULL else {"kind": "value", "value": expected.region}
        )
        exp_category = (
            {"kind": "null"} if expected.category is NULL else {"kind": "value", "value": expected.category}
        )
        check(
            {"kind": actual["region"]["kind"], **({"value": actual["region"].get("value")} if actual["region"]["kind"] == "value" else {})}
            == exp_region,
            f"地区值不符 {actual['region']} vs {exp_region}",
        )
        check(
            {"kind": actual["category"]["kind"], **({"value": actual["category"].get("value")} if actual["category"]["kind"] == "value" else {})}
            == exp_category,
            f"类别值不符 {actual['category']} vs {exp_category}",
        )

    # ------------------------------------------------------------------ #
    section("B. 两种类别口径")
    scenarios = [
        ("无类别筛选", [], []),
        ("单类别", [], ["服务"]),
        ("多类别并集", [], ["服务", "运输"]),
        ("含 NULL 类别", [], [NULL, "办公"]),
        ("含空串与 NULL", [], [NULL, ""]),
        ("地区并集 + 类别并集 + 交集", ["华北", "华东"], ["办公", "服务"]),
        ("空串地区 + NULL 类别", [""], [NULL]),
        ("NULL 地区 + 空串类别", [NULL], [""]),
    ]
    for name, regions, categories in scenarios:
        expected_full = ref_select(rows, date_min, date_to_default, regions, categories)
        expected_cand = ref_select(rows, date_min, date_to_default, regions, None)  # 移除类别自身筛选
        status, data = http_json(
            f"{base}/api/query",
            {
                "state_id": "v-" + str(abs(hash(name)) % 100000),
                "filters": {
                    "date_from": date_min,
                    "date_to": date_to_default,
                    "region": [sel_json(v) for v in regions],
                    "category": [sel_json(v) for v in categories],
                },
                "page": 1,
                "page_size": 10,
            },
        )
        check(status == 200, f"[{name}] 应 200，得到 {status}")
        check(data["metrics"] == ref_metrics(expected_full), f"[{name}] 主指标不符")
        check(
            data["detail"]["total_rows"] == len(expected_full),
            f"[{name}] 明细 total_rows 不符",
        )

        # 分布口径 == 完整筛选下的类别聚合。
        expected_dist = ref_category_buckets(expected_full)
        actual_dist = {
            key_of(b): {"row_count": b["row_count"], "amount_cents": b["amount_cents"]}
            for b in data["category_distribution"]["buckets"]
            if b["row_count"] > 0
        }
        check(actual_dist == expected_dist, f"[{name}] 类别分布（完整筛选）不符：{actual_dist} vs {expected_dist}")
        check(
            data["category_distribution"]["basis"] == "filters",
            f"[{name}] 分布口径标记错误",
        )

        # 候选口径 == 移除类别自身筛选后的类别聚合。
        expected_cand_buckets = ref_category_buckets(expected_cand)
        actual_cand = {
            key_of(b): {"row_count": b["row_count"], "amount_cents": b["amount_cents"]}
            for b in data["category_candidates"]["buckets"]
            if b["row_count"] > 0
        }
        check(
            actual_cand == expected_cand_buckets,
            f"[{name}] 类别候选（移除类别筛选）不符：{actual_cand} vs {expected_cand_buckets}",
        )
        check(
            data["category_candidates"]["basis"] == "filters_without_category",
            f"[{name}] 候选口径标记错误",
        )
        # 每个候选类别都应出现在分布里，带 selected 标记，便于继续添加。
        check(
            all("selected" in b for b in data["category_candidates"]["buckets"]),
            f"[{name}] 候选桶缺少 selected 标记",
        )

    # ------------------------------------------------------------------ #
    section("C. 空值 vs 空字符串（不能被 SQL NULL 语义吞掉）")
    for label, sel, forbidden in [
        ("仅 NULL", [NULL], [""]),
        ("仅空字符串", [""], [NULL]),
    ]:
        expected = ref_select(rows, date_min, date_to_default, sel, None)
        _, data = http_json(
            f"{base}/api/query",
            {
                "state_id": "v-null-" + label,
                "filters": {
                    "date_from": date_min,
                    "date_to": date_to_default,
                    "region": [sel_json(v) for v in sel],
                    "category": [],
                },
                "page": 1,
                "page_size": 200,
            },
        )
        observed = {r["region"].get("value") if r["region"]["kind"] == "value" else NULL for r in data["detail"]["rows"]}
        check(data["metrics"] == ref_metrics(expected), f"[{label}] 指标不符")
        check(all(r["region"]["kind"] == ("null" if sel[0] is NULL else "value") for r in data["detail"]["rows"]), f"[{label}] 明细混入另一类取值")
        check(data["metrics"]["row_count"] == len(expected), f"[{label}] 条数不符")

    # ------------------------------------------------------------------ #
    section("D. 日期左闭右开边界")
    if len(rows) >= 2:
        ordered_dates = sorted({r.business_date for r in rows})
        d1 = ordered_dates[1]
        d2 = ordered_dates[2]
        # [d1, d2) 应只含 d1 当天。
        expected = [r for r in rows if d1 <= r.business_date < d2]
        _, data = http_json(
            f"{base}/api/query",
            {
                "state_id": "v-date",
                "filters": {"date_from": d1, "date_to": d2, "region": [], "category": []},
                "page": 1,
                "page_size": 200,
            },
        )
        check(data["metrics"]["row_count"] == len(expected), f"[{d1},{d2}) 条数不符：{data['metrics']['row_count']} vs {len(expected)}")
        check(all(r["business_date"] == d1 for r in data["detail"]["rows"]), f"[{d1},{d2}) 内出现区间外日期")
        check(len(data["trend"]) == 1, "区间长度 1 天的趋势应只有 1 个点")

        # 右端点不含：取 [d1, d1) 应为空。
        _, empty = http_json(
            f"{base}/api/query",
            {
                "state_id": "v-date-empty",
                "filters": {"date_from": d1, "date_to": d1, "region": [], "category": []},
                "page": 1,
                "page_size": 10,
            },
        )
        check(empty["metrics"]["row_count"] == 0, "空区间应为 0 条")
        check(empty["trend"] == [], "空区间趋势应为空")

        # 右端当天若应是开区间则排除：用 [d1, d1+1) 与含 d2 的区间比较。
        d2_plus = (date.fromisoformat(d2) + timedelta(days=1)).isoformat()
        _, wide = http_json(
            f"{base}/api/query",
            {
                "state_id": "v-date-wide",
                "filters": {"date_from": d1, "date_to": d2_plus, "region": [], "category": []},
                "page": 1,
                "page_size": 200,
            },
        )
        expected_wide = [r for r in rows if d1 <= r.business_date < d2_plus]
        check(wide["metrics"]["row_count"] == len(expected_wide), "宽区间条数不符（右开）")
        check(
            any(r["business_date"] == d2 for r in wide["detail"]["rows"]),
            "右开区间应包含结束日之前但不含更晚日期",
        )

    # ------------------------------------------------------------------ #
    section("E. 随机筛选性质测试（并集/交集/金额精确）")
    rng = random.Random(20250101)
    for trial in range(12):
        regions = rng.sample([NULL, ""] + region_values, rng.randint(0, 2))
        categories = rng.sample([NULL, ""] + category_values, rng.randint(0, 2))
        a = date.fromisoformat(date_min) + timedelta(days=rng.randint(0, 30))
        b = a + timedelta(days=rng.randint(0, 20))
        if b > date.fromisoformat(date_to_default):
            b = date.fromisoformat(date_to_default)
        expected = ref_select(rows, a.isoformat(), b.isoformat(), regions, categories)
        _, data = http_json(
            f"{base}/api/query",
            {
                "state_id": f"rand-{trial}",
                "filters": {
                    "date_from": a.isoformat(),
                    "date_to": b.isoformat(),
                    "region": [sel_json(v) for v in regions],
                    "category": [sel_json(v) for v in categories],
                },
                "page": 1,
                "page_size": 200,
            },
        )
        check(
            data["metrics"] == ref_metrics(expected),
            f"[随机 {trial}] 指标不符：{data['metrics']} vs {ref_metrics(expected)}",
        )

    # ------------------------------------------------------------------ #
    section("F. state_id 透传与筛选指纹")
    _, data = http_json(
        f"{base}/api/query",
        {
            "state_id": "state-xyz",
            "filters": {"date_from": date_min, "date_to": date_to_default, "region": [], "category": []},
            "page": 1,
            "page_size": 5,
        },
    )
    check(data["state_id"] == "state-xyz", "state_id 未回显")
    check(isinstance(data.get("filter_fingerprint"), str) and data["filter_fingerprint"], "缺少筛选指纹")
    fp_a = data["filter_fingerprint"]
    _, data2 = http_json(
        f"{base}/api/query",
        {
            "state_id": "state-abc",
            "filters": {
                "date_from": date_min,
                "date_to": date_to_default,
                "region": [{"kind": "value", "value": "华北"}],
                "category": [],
            },
            "page": 1,
            "page_size": 5,
        },
    )
    check(data2["filter_fingerprint"] != fp_a, "不同筛选应产生不同指纹")

    # ------------------------------------------------------------------ #
    section("G. 服务端可控延迟：慢请求晚到不改变各自结果")
    slow_payload = {
        "state_id": "slow-state",
        "filters": {
            "date_from": date_min,
            "date_to": date_to_default,
            "region": [{"kind": "value", "value": "华北"}],
            "category": [],
        },
        "page": 1,
        "page_size": 5,
    }
    fast_payload = {
        "state_id": "fast-state",
        "filters": {
            "date_from": date_min,
            "date_to": date_to_default,
            "region": [{"kind": "value", "value": "华南"}],
            "category": [],
        },
        "page": 1,
        "page_size": 5,
    }
    import threading

    results: dict[str, tuple[int, dict, float]] = {}

    def call(tag: str, payload: dict, delay: int) -> None:
        started = time.time()
        status, data = http_json(f"{base}/api/query?delay_ms={delay}", payload)
        results[tag] = (status, data, time.time() - started)

    slow = threading.Thread(target=call, args=("slow", slow_payload, 1200))
    slow.start()
    time.sleep(0.15)
    fast = threading.Thread(target=call, args=("fast", fast_payload, 0))
    fast.start()
    slow.join()
    fast.join()

    check(results["slow"][0] == 200 and results["fast"][0] == 200, "延迟场景两次请求都应成功")
    check(results["fast"][2] < results["slow"][2], "延迟注入未生效：快请求应先返回")
    check(results["slow"][1]["state_id"] == "slow-state", "慢响应 state_id 不符")
    check(results["fast"][1]["state_id"] == "fast-state", "快响应 state_id 不符")
    expected_slow = ref_select(rows, date_min, date_to_default, ["华北"], [])
    expected_fast = ref_select(rows, date_min, date_to_default, ["华南"], [])
    check(results["slow"][1]["metrics"] == ref_metrics(expected_slow), "慢响应数值不是自身筛选的结果")
    check(results["fast"][1]["metrics"] == ref_metrics(expected_fast), "快响应数值不是自身筛选的结果")
    check(
        results["slow"][1]["filter_fingerprint"] != results["fast"][1]["filter_fingerprint"],
        "两种状态的指纹应不同",
    )

    # ------------------------------------------------------------------ #
    section("H. 单部件失败：明确未完成，且不拼接数值")
    status, data = http_json(
        f"{base}/api/query?fail=category_distribution",
        {
            "state_id": "fail-state",
            "filters": {"date_from": date_min, "date_to": date_to_default, "region": [], "category": []},
            "page": 1,
            "page_size": 5,
        },
    )
    check(status == 200, "单部件失败应返回 200（整页仍可显示其余部件）")
    check(data["state_id"] == "fail-state", "失败响应仍须带 state_id")
    check(data["partial"] is True, "应标记 partial")
    check("category_distribution" in data["component_errors"], "应明确标记失败的部件")
    check(data["category_distribution"] is None, "失败部件的数据应为 None，不能拼旧值")
    check(
        data["component_errors"]["category_distribution"]["retryable"] is True,
        "失败部件应标记可重试",
    )
    check(data["metrics"] == ref_metrics(full), "同一 state 的其余部件仍是完整筛选的结果")

    # 整页失败
    status, data = http_json(
        f"{base}/api/query?fail=all",
        {
            "state_id": "fail-all",
            "filters": {"date_from": date_min, "date_to": date_to_default, "region": [], "category": []},
            "page": 1,
            "page_size": 5,
        },
    )
    check(status == 503, f"整页失败应 503，得到 {status}")
    check(data.get("state_id") == "fail-all", "整页失败也应回显 state_id")
    check("metrics" not in data, "整页失败不应返回任何数值")

    # 独立部件重试
    status, data = http_json(
        f"{base}/api/component",
        {
            "state_id": "retry-state",
            "filters": {"date_from": date_min, "date_to": date_to_default, "region": [], "category": []},
            "component": "category_distribution",
        },
    )
    check(status == 200, "部件重试接口应 200")
    check(data["component"] == "category_distribution", "部件名不符")
    expected_dist = ref_category_buckets(full)
    actual = {
        key_of(b): {"row_count": b["row_count"], "amount_cents": b["amount_cents"]}
        for b in data["data"]["buckets"]
    }
    check(actual == expected_dist, "部件重试数据不符")

    # ------------------------------------------------------------------ #
    section("I. 非法输入")
    for bad in [
        {"date_from": "2025-02-15", "date_to": "2025-01-01"},
        {"date_from": "2025/01/01", "date_to": "2025-01-02"},
    ]:
        status, data = http_json(
            f"{base}/api/query",
            {"state_id": "bad", "filters": {**bad, "region": [], "category": []}},
        )
        check(status == 400, f"非法日期应 400：{bad}")
        check(data.get("state_id") == "bad", "400 也应回显 state_id")

    # ------------------------------------------------------------------ #
    print(f"\n{'='*60}")
    if FAILS:
        print(f"失败 {len(FAILS)} / {CHECKS} 项检查")
        for fail in FAILS:
            print("  -", fail)
        return 1
    print(f"全部通过：{CHECKS} 项检查")
    return 0


if __name__ == "__main__":
    sys.exit(main())
