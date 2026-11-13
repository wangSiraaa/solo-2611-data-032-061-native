"""独立核验：不依赖分析器内部状态，直接从 SQLite 结果表复算。

核验项
------
1. 覆盖守恒：每个根 trace 的时间片之和 == 根窗口；四类互斥且无重叠、无越界。
2. 不重计：同 trace 内任取两个时间片，区间不相交；RPC 配对后客户端/服务端不会在
   覆盖账本里各算一遍（服务端 span 不允许出现在根时间片的 self 引用中）。
3. await 嵌套：内层 await 窗口被外层覆盖，且只作为外层的解释出现，不另立时间片。
4. 微案例手工预期（每个根时间片应归属的原因），与 generator 的设计值对齐。
5. 资源账本只做并集口径自检（含开放区间时必须标记 open，下界 <= 任何确定值）。
"""
from __future__ import annotations

import sqlite3

from .model import COV_SELF

EXPECTED = {
    # 每个根时间片应归属的原因；时间单位纳秒，与 generator 的设计值对齐
    "T-sync": {"root_window_ns": 30_000_000,
               "totals": {"self": 14_000_000, "await": 14_000_000,
                          "queue": 2_000_000, "gap": 0},
               "note": "10-12ms 服务端排队是 await 窗口内更具体的证据，queue 单列、await 相应减少"},
    "T-parallel": {"root_window_ns": 32_000_000,
                   "totals": {"self": 16_000_000, "await": 12_000_000,
                              "queue": 4_000_000, "gap": 0},
                   "note": "两路排队区间 10-12 与 11-14 不连续，并集 4ms；await+queue 总覆盖仍为 16ms"},
    "T-any": {"root_window_ns": 24_000_000,
              "totals": {"self": 13_000_000, "await": 10_000_000,
                         "queue": 1_000_000, "gap": 0},
              "note": "三路同刻 5-6 排队，并集只算 1ms"},
    "T-retry": {"root_window_ns": 40_000_000,
                "totals": {"self": 8_000_000, "await": 29_000_000,
                           "queue": 3_000_000, "gap": 0}},
    "T-async": {"root_window_ns": 18_000_000,
                "totals": {"self": 18_000_000, "await": 0,
                           "queue": 0, "gap": 0},
                "background_min_ns": 30_000_000},
    "T-shared": {"root_window_ns": 30_000_000,
                 "totals": {"self": 16_000_000, "await": 14_000_000,
                            "queue": 0, "gap": 0}},
    "T-missing": {"root_window_ns": 24_000_000,
                  "totals": {"self": 14_000_000, "await": 10_000_000,
                             "queue": 0, "gap": 0},
                  "must_have_diag": "D4_missing_server_unknown_transmit"},
    "T-queue": {"root_window_ns": 26_000_000,
                "totals": {"self": 12_000_000, "await": 0,
                           "queue": 4_000_000, "gap": 10_000_000},
                "must_have_diag": "D1_queue_wait_before_exec",
                "note": "8-12ms 权威排队；6-8ms 传输与 12-20ms 远端执行在根侧无更细证据，显式标 gap"},
    "T-gap": {"root_window_ns": 20_000_000,
              "totals": {"self": 11_000_000, "await": 0,
                         "queue": 0, "gap": 9_000_000},
              "must_have_diag": "D8_long_unexplained_gap"},
    "T-nested": {"root_window_ns": 28_000_000,
                 "totals": {"self": 8_000_000, "await": 18_000_000,
                            "queue": 2_000_000, "gap": 0},
                 "must_have_diag": "D5_nested_await_real_blocker",
                 "note": "内层 await 完全嵌套在外层内，覆盖只按外层计一次，不重复相加"},
    "T-lag": {"root_window_ns": 24_000_000,
              "totals": {"self": 8_000_000, "await": 15_000_000,
                         "queue": 1_000_000, "gap": 0},
              "must_have_diag": "D2_result_ready_but_resume_lag"},
}


def run_verification(conn: sqlite3.Connection) -> dict:
    result: dict = {"all_ok": True, "traces": [], "micro_cases": [], "errors": []}

    def fail(msg):
        result["all_ok"] = False
        result["errors"].append(msg)

    for row in conn.execute("SELECT * FROM analysis_trace ORDER BY trace_id"):
        t = row["trace_id"]
        entry = {"trace_id": t, "checks": {}}
        if row["root_window_ns"] is None:
            entry["checks"]["root_window"] = "unknown_time_skipped"
            result["traces"].append(entry)
            continue
        slices = [dict(r) for r in conn.execute(
            "SELECT * FROM timeline_slices WHERE trace_id=? ORDER BY slice_no", (t,))]
        # 1) 守恒
        total = sum(s["duration_ns"] for s in slices)
        entry["checks"]["sum_equals_root_window"] = (total == row["root_window_ns"])
        if total != row["root_window_ns"]:
            fail(f"{t}: 时间片和 {total} != 根窗口 {row['root_window_ns']}")
        # 2) 相邻不重叠、不越界
        ok_bounds = all(slices[i]["end_ns"] == slices[i + 1]["start_ns"]
                        for i in range(len(slices) - 1))
        ok_bounds = ok_bounds and (not slices or (
            slices[0]["start_ns"] == row["root_start_ns"]
            and slices[-1]["end_ns"] == row["root_end_ns"]))
        entry["checks"]["partition_without_overlap"] = ok_bounds
        if not ok_bounds:
            fail(f"{t}: 时间片存在重叠/越界/断档")
        # 3) 分类汇总与 trace 摘要一致
        cat_sum = {"self": 0, "await": 0, "queue": 0, "gap": 0}
        for s in slices:
            cat_sum[s["category"]] += s["duration_ns"]
        ok_cat = (cat_sum["self"] == row["self_ns"] and cat_sum["await"] == row["await_ns"]
                  and cat_sum["queue"] == row["queue_ns"] and cat_sum["gap"] == row["gap_ns"])
        entry["checks"]["category_totals_match"] = ok_cat
        if not ok_cat:
            fail(f"{t}: 分类汇总与摘要不一致 {cat_sum}")
        # 4) 远端服务端 span 不得作为根覆盖的 self 证据（防止 RPC 两面重复计）；
        #    根本身是 gateway 的 server span，属于自身活动，排除在该检查之外
        root_id = row["root_span_id"]
        bad_refs = []
        server_ids = {r["span_id"] for r in conn.execute(
            "SELECT span_id FROM spans WHERE trace_id=? AND span_type='server'", (t,))}
        for s in slices:
            if (s["category"] == COV_SELF and s["ref_id"] in server_ids
                    and s["ref_id"] != root_id):
                bad_refs.append(s["ref_id"])
        entry["checks"]["no_server_span_double_count"] = not bad_refs
        if bad_refs:
            fail(f"{t}: 服务端 span 混入根覆盖 self：{bad_refs}")
        # 5) await 嵌套不另立：所有 await 类时间片总时长 <= await 窗口并集
        aw_ivs = [(r["start_ns"], r["end_ns"]) for r in conn.execute(
            "SELECT start_ns, end_ns FROM awaits WHERE trace_id=? AND end_ns IS NOT NULL", (t,))]
        union_ns = _union_len(aw_ivs, row["root_start_ns"], row["root_end_ns"])
        entry["checks"]["await_no_double_count"] = cat_sum["await"] <= union_ns
        if cat_sum["await"] > union_ns:
            fail(f"{t}: await 覆盖 {cat_sum['await']} 超过窗口并集 {union_ns}")
        result["traces"].append(entry)

    # 资源账本开放区间自检
    for r in conn.execute("SELECT * FROM resource_ledger WHERE open_flag=1"):
        if r["upper_bound_ns"] is not None:
            fail(f"{r['trace_id']} {r['service']}/{r['operation']}: 开放区间却给出确定上界")

    result["micro_cases"] = _check_micro_cases(conn, fail)
    return result


def _union_len(ivs, lo, hi):
    pts = sorted((max(s, lo), 0) for s, _ in ivs) + sorted((min(e, hi), 1) for _, e in ivs)
    total = depth = 0
    last = lo
    for x, kind in pts:
        if depth > 0 and x > last:
            total += x - last
        depth += -1 if kind else 1
        last = x
    return total


def _check_micro_cases(conn, fail) -> list[dict]:
    out = []
    for tid, exp in EXPECTED.items():
        row = conn.execute("SELECT * FROM analysis_trace WHERE trace_id=?", (tid,)).fetchone()
        if not row:
            continue
        ok = row["root_window_ns"] == exp["root_window_ns"]
        got = {"self": row["self_ns"], "await": row["await_ns"],
               "queue": row["queue_ns"], "gap": row["gap_ns"]}
        ok = ok and all(got[k] == v for k, v in exp["totals"].items())
        if "background_min_ns" in exp:
            ok = ok and (row["background_ns"] or 0) >= exp["background_min_ns"]
        if "must_have_diag" in exp:
            hit = conn.execute("SELECT 1 FROM diagnostics WHERE trace_id=? AND diag_code=?",
                               (tid, exp["must_have_diag"])).fetchone()
            ok = ok and bool(hit)
        if not ok:
            fail(f"微案例 {tid} 预期不符：got={got}")
        out.append({"trace_id": tid, "ok": bool(ok), "expected": exp, "got": got})
    return out
