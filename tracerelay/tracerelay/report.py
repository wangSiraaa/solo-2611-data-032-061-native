"""批量报告：按服务/操作与请求类型汇总，并提供可复算的池化分位数。

分位数口径（重要）
------------------
合并不同 trace 时，分位数只对**同一总体内的逐 trace 观测值**计算（池化后再求 p50/p95）。
绝不把“服务 A 的 p95 + 服务 B 的 p95”冒充总体 p95 —— 报告中同时输出每服务观测与
总体池化观测，并保留 ``quantile_method`` 字段说明方法，供复核。
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict

from .model import percentile


def _pooled(values: list[int]) -> dict:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return {"n": 0}
    return {
        "n": len(vals), "sum_ns": sum(vals), "min_ns": vals[0],
        "max_ns": vals[-1], "p50_ns": percentile(vals, 50),
        "p95_ns": percentile(vals, 95), "p99_ns": percentile(vals, 99),
        "quantile_method": "pooled_per_trace_observations_linear_interp",
        "forbidden": "do_not_sum_per_service_quantiles",
    }


def build_report(conn: sqlite3.Connection) -> dict:
    # ---- trace 总览 ----
    traces = [dict(r) for r in conn.execute("SELECT * FROM analysis_trace ORDER BY trace_id")]
    complete = [t for t in traces if t["complete_flag"]]
    gappy = [t for t in traces if t["has_gap"] or t["has_unknown_time"]
             or not t["complete_flag"]]
    overview = {
        "n_traces": len(traces),
        "n_complete": len(complete),
        "n_with_gaps": len(gappy),
        "incomplete_rate": (len(gappy) / len(traces)) if traces else None,
        "conservation_violations": [t["trace_id"] for t in traces
                                    if not t["conservation_ok"]],
        "coverage_pooled_ns": {
            "root_window": _pooled([t["root_window_ns"] for t in traces]),
            "self": _pooled([t["self_ns"] for t in traces]),
            "await": _pooled([t["await_ns"] for t in traces]),
            "queue": _pooled([t["queue_ns"] for t in traces]),
            "gap": _pooled([t["gap_ns"] for t in traces]),
            "background_after_root": _pooled([t["background_ns"] for t in traces
                                              if t["background_ns"] is not None]),
        },
    }

    # ---- 服务 / 操作维度（逐 trace 观测池化）----
    svc_obs: dict[tuple, dict[str, list]] = defaultdict(
        lambda: {"exec": [], "queue": [], "await": [], "background": [],
                 "client": [], "producer": [], "open_traces": set()})
    for r in conn.execute("SELECT * FROM resource_ledger"):
        key = (r["service"], r["operation"])
        svc_obs[key]["exec"].append(r["exec_ns"])
        svc_obs[key]["queue"].append(r["queue_ns"])
        svc_obs[key]["await"].append(r["await_ns"])
        svc_obs[key]["background"].append(r["background_ns"])
        # resource_ledger 表没有 client 列，客户端墙钟从 spans 表按服务/操作补采
        if r["open_flag"]:
            svc_obs[key]["open_traces"].add(r["trace_id"])
    # 客户端/生产者墙钟按服务操作补采（区间并集已在分析阶段按 trace 算好，这里逐 trace 池化）
    for r in conn.execute(
            "SELECT service, operation, trace_id, "
            "SUM(COALESCE(end_ns, start_ns) - start_ns) AS wall_ns, span_type "
            "FROM spans WHERE span_type IN ('client','producer') "
            "GROUP BY service, operation, trace_id, span_type"):
        key = (r["service"], r["operation"])
        svc_obs[key]["client" if r["span_type"] == "client" else "producer"].append(r["wall_ns"])
    service_rows = []
    for (svc, op), obs in sorted(svc_obs.items()):
        service_rows.append({
            "service": svc, "operation": op,
            "execution": _pooled(obs["exec"]),
            "client_span": _pooled(obs["client"]),
            "producer_span": _pooled(obs["producer"]),
            "queue_wait": _pooled(obs["queue"]),
            "explicit_await": _pooled(obs["await"]),
            "background": _pooled([x for x in obs["background"] if x is not None]),
            "open_ended_trace_count": len(obs["open_traces"]),
            "note": ("含开放区间，仅给下界" if obs["open_traces"]
                     else "墙钟占用并集，非 CPU 时间"),
        })

    # ---- 请求类型对比 ----
    type_obs: dict[str, list] = defaultdict(list)
    for t in traces:
        type_obs[t["request_type"]].append(t)
    request_type_rows = []
    for rtype, rows in sorted(type_obs.items()):
        request_type_rows.append({
            "request_type": rtype, "n": len(rows),
            "root_window": _pooled([x["root_window_ns"] for x in rows]),
            "await_share_pct": _share(rows, "await_ns"),
            "queue_share_pct": _share(rows, "queue_ns"),
            "gap_share_pct": _share(rows, "gap_ns"),
            "incomplete_rate": sum(1 for x in rows if not x["complete_flag"]) / len(rows),
        })

    # ---- 诊断汇总 ----
    diag_rows = []
    for r in conn.execute(
            "SELECT diag_code, certainty, COUNT(*) AS n FROM diagnostics "
            "GROUP BY diag_code, certainty ORDER BY n DESC"):
        diag_rows.append({"diag_code": r["diag_code"], "certainty": r["certainty"],
                          "count": r["n"]})

    # ---- 重试与不完整率 ----
    retry_rows = [dict(r) for r in conn.execute(
        "SELECT trace_id, diag_code, certainty, detail FROM diagnostics "
        "WHERE diag_code='D3_retry_background_amplification' ORDER BY trace_id")]

    return {
        "generated_note": "仅基于合成追踪中的墙钟记录；无 CPU 观测，不能推断 CPU 使用率",
        "overview": overview,
        "by_service_operation": service_rows,
        "by_request_type": request_type_rows,
        "diagnostics": diag_rows,
        "retry_evidence": retry_rows,
        "filters": {"complete_only": "/api/traces?complete=1",
                    "gaps_only": "/api/traces?gaps=1"},
    }


def _share(rows: list[dict], key: str) -> float | None:
    tot_w = sum((r["root_window_ns"] or 0) for r in rows)
    tot_v = sum((r[key] or 0) for r in rows)
    return (tot_v / tot_w) if tot_w else None


def render_text(report: dict) -> str:
    o = report["overview"]
    lines = [
        "TraceRelay 批量报告（合成数据；墙钟口径，非 CPU）",
        "=" * 68,
        f"trace 总数 {o['n_traces']}  完整 {o['n_complete']}  "
        f"有缺口 {o['n_with_gaps']}  不完整率 "
        f"{(o['incomplete_rate'] or 0):.2%}",
        f"守恒校验失败 trace: {o['conservation_violations'] or '无'}",
        "",
        "根窗口覆盖池化分布（ns；分位数按逐 trace 池化观测计算，禁止分位数相加）：",
    ]
    for k, v in o["coverage_pooled_ns"].items():
        if v.get("n"):
            lines.append(f"  {k:22s} n={v['n']:5d} p50={v['p50_ns']:>10.0f} "
                         f"p95={v['p95_ns']:>10.0f} sum={v['sum_ns']}")
    lines += ["", "按服务/操作（墙钟并集 p50/p95，client/exec 分列，开放区间仅下界）："]
    for r in report["by_service_operation"]:
        e = r["execution"]
        c = r["client_span"] if "client_span" in r else {"n": 0}
        lines.append(
            f"  {r['service']}.{r['operation']:24s} exec n={e.get('n', 0):4d} "
            f"p50={e.get('p50_ns', 0) or 0:>9.0f} p95={e.get('p95_ns', 0) or 0:>9.0f} "
            f"client p95={c.get('p95_ns', 0) or 0:>8.0f} "
            f"queue p95={r['queue_wait'].get('p95_ns', 0) or 0:>8.0f} "
            f"open={r['open_ended_trace_count']} {r['note']}")
    lines += ["", "按请求类型："]
    for r in report["by_request_type"]:
        lines.append(
            f"  {r['request_type']:32s} n={r['n']:4d} "
            f"await={(r['await_share_pct'] or 0):.1%} "
            f"queue={(r['queue_share_pct'] or 0):.1%} "
            f"gap={(r['gap_share_pct'] or 0):.1%} "
            f"incomplete={r['incomplete_rate']:.1%}")
    lines += ["", "诊断计数："]
    for d in report["diagnostics"]:
        lines.append(f"  {d['diag_code']:42s} {d['certainty']:8s} {d['count']}")
    lines.append("")
    lines.append("注：资源账本允许跨并行分支累加，其总和不等于也不应等于根响应窗口；"
                 "两套口径分别守恒。")
    return "\n".join(lines)
