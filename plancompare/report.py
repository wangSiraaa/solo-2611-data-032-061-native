"""Render the analysis result as a short Markdown report (Chinese)."""

from __future__ import annotations

from typing import Any

from .metrics import BUFFER_FIELDS

_STATUS_LABEL = {
    "matched": "对应",
    "added": "新增",
    "removed": "消失",
    "ambiguous": "无法唯一对应",
}

_COMPARABILITY_LABEL = {
    "ok": "可直接比较（SQL 身份、参数、数据快照一致）",
    "identity_mismatch": "不可直接比较：SQL 身份或参数不一致",
    "snapshot_mismatch": "可比较但有保留：数据快照不同，不能断言计划本身造成回归",
    "no_manifest": "无采集清单：仅做结构对比，无法核对身份与快照",
}


def _ms(v) -> str:
    return "n/a" if v is None else f"{v:.3f}"


def _num(v) -> str:
    if v is None:
        return "n/a"
    f = float(v)
    return f"{f:.0f}" if f.is_integer() else f"{f:.2f}"


def _ratio(v) -> str:
    return "n/a" if v is None else f"{v:.2f}"


def _change(e: dict, unit: str = "") -> str:
    if e.get("delta") is None:
        return "—"
    if e["delta"] == 0:
        return "不变"
    s = f"{e['delta']:+.3f}{unit}"
    if e.get("ratio"):
        s += f"（{(e['ratio'] - 1):+.1%}）"
    return s


def _val(e: dict, side: str, fmt=_num) -> str:
    return fmt(e.get(side))


def render_markdown(result: dict) -> str:
    out: list[str] = []
    w = out.append
    comp = result["comparability"]
    pl = result["plan_level"]

    w("# 计划对比报告（离线分析）")
    w("")
    w(f"- before：`{result['inputs']['before']}`")
    w(f"- after：`{result['inputs']['after']}`")
    cap = result["inputs"]["before_capture"] or result["inputs"]["after_capture"]
    w(f"- 采集清单：`{cap or '未提供'}`")
    w("")

    # -- 1. comparability -------------------------------------------------
    w("## 1. 可比性结论")
    w("")
    w(f"**{_COMPARABILITY_LABEL.get(comp['status'], comp['status'])}**")
    w("")
    if comp["checks"]:
        w("| 检查项 | 结果 | 依据 |")
        w("| --- | --- | --- |")
        for c in comp["checks"]:
            evidence = c.get("detail") or ""
            if c.get("check") == "capture_settings" and c.get("result") == "differ":
                evidence = f"before: `{c.get('before')}`<br>after: `{c.get('after')}`"
            elif c.get("check") == "data_snapshot" and c.get("result") == "mismatch":
                evidence = f"before: `{c.get('before')}`<br>after: `{c.get('after')}`"
            elif c.get("check") == "sql_identity" and c.get("result") == "mismatch":
                evidence = f"before: `{c.get('before')}`<br>after: `{c.get('after')}`"
            elif c.get("value") is not None:
                evidence = f"`{c.get('value')}`"
            w(f"| {c['check']} | {c['result']} | {evidence} |")
        w("")
    if not comp["can_attribute_to_plan"]:
        w("> 注意：当前条件下不能把差异断言为“计划本身造成回归”（见下方保留项）。")
        w("")
    for cav in comp["caveats"]:
        w(f"- 保留项：{cav}")
    if comp["caveats"]:
        w("")

    # -- 2. conventions ----------------------------------------------------
    w("## 2. 口径与边界（先读）")
    w("")
    w("- `Actual Rows` / `Actual Total Time` 为**每次循环**值；列名含 `×Loops` 的为乘上 `Actual Loops` 的合计。")
    w("- 父节点时间/缓冲**包含**子节点；总耗时以根节点或 `Execution Time` 为准，**不能**把全部节点时间相加。")
    w("- 自耗时 = 自身×Loops 总时间 − 子节点×Loops 总时间；字段缺失或存在并行 Worker 时不推算，标记原因。")
    w("- `Total Cost` 是优化器估计单位，**不是毫秒**。")
    w("- 本工具只做离线对比：不连接数据库、不推荐索引、不改写 SQL。")
    w("")

    # -- 3. plan-level observations ----------------------------------------
    w("## 3. 总体观测")
    w("")
    w("| 指标 | before | after | 变化 | 依据 |")
    w("| --- | --- | --- | --- | --- |")
    et = pl["execution_time_ms"]
    w(f"| Execution Time (ms) | {_ms(et['before'])} | {_ms(et['after'])} "
      f"| {_change(et, ' ms')} | `{et['before_pointer']}` |")
    pt = pl["planning_time_ms"]
    w(f"| Planning Time (ms) | {_ms(pt['before'])} | {_ms(pt['after'])} "
      f"| {_change(pt, ' ms')} | `{pt['before_pointer']}` |")
    rn = pl["root_node_type"]
    w(f"| 根节点类型 | {rn['before']} | {rn['after']} | "
      f"{'不变' if rn['before'] == rn['after'] else '变化'} | `{rn['before_pointer']}` |")
    rr = pl["result_rows_xloops"]
    w(f"| 结果行数（根 ×Loops） | {_num(rr['before'])} | {_num(rr['after'])} "
      f"| {_change(rr)} | `{rr['before_pointer']}` |")
    w("")

    # -- 4. structure correspondence ----------------------------------------
    w("## 4. 结构对应关系")
    w("")
    w("配对依据：关系/别名、子树参与表集合、Sort/Aggregate 键与父上下文；不使用数组下标，不强行配对。")
    w("")
    w("| ID | 状态 | before | after | 依据 / 结构变化 |")
    w("| --- | --- | --- | --- | --- |")
    for p in result["correspondence"]:
        status = _STATUS_LABEL[p["status"]]
        if p["status"] == "ambiguous":
            cb = "<br>".join(f"`{c['pointer']}` {c['label']}" for c in p["candidates_before"])
            ca = "<br>".join(f"`{c['pointer']}` {c['label']}" for c in p["candidates_after"])
            w(f"| {p['id']} | {status} | {cb} | {ca} | {'；'.join(p['evidence'])} |")
            continue
        b = p.get("before")
        a = p.get("after")
        bcell = f"{b['label']}<br>`{b['pointer']}`" if b else "—"
        acell = f"{a['label']}<br>`{a['pointer']}`" if a else "—"
        why = "；".join(p["evidence"] + p.get("structural_changes", []))
        if p["status"] in ("added", "removed"):
            m = p["metrics"]
            why += f"（行数×Loops={_num(m['actual_rows_total_xloops'])}，" \
                   f"时间×Loops={_ms(m['actual_total_time_xloops_ms'])} ms）"
        w(f"| {p['id']} | {status} | {bcell} | {acell} | {why} |")
    w("")

    # -- 5. per-pair metrics --------------------------------------------------
    matched = [p for p in result["correspondence"] if p["status"] == "matched"]
    if matched:
        w("## 5. 对应节点指标对比")
        w("")
        for p in matched:
            w(f"### {p['id']} {p['before']['label']} ↔ {p['after']['label']}")
            w("")
            w(f"- before：`{p['before']['pointer']}` · after：`{p['after']['pointer']}`")
            for ch in p["structural_changes"]:
                w(f"- 结构变化：{ch}")
            if not (p["before"]["supported"] and p["after"]["supported"]):
                w("- 注意：含未知边界节点类型，仅展示通用字段")
            w("")
            m = p["metrics"]
            w("| 指标 | before | after | 变化 |")
            w("| --- | --- | --- | --- |")
            rows = [
                ("Plan Rows（估计）", m["plan_rows"], _num),
                ("Actual Rows（每循环）", m["actual_rows_per_loop"], _num),
                ("Actual Loops", m["actual_loops"], _num),
                ("Actual Rows ×Loops", m["actual_rows_total_xloops"], _num),
                ("估计偏差（每循环 实际/估计）", m["est_ratio_per_loop"], _ratio),
                ("时间 ×Loops ms（含子节点）", m["actual_total_time_xloops_ms"], _ms),
                ("Total Cost（估计单位，非毫秒）", m["total_cost"], _num),
            ]
            for label, e, fmt in rows:
                w(f"| {label} | {_val(e, 'before', fmt)} | {_val(e, 'after', fmt)} | {_change(e)} |")
            st = m["self_time_xloops_ms"]
            if st["delta"] is None:
                st_delta = "—"
            elif st["delta"] == 0:
                st_delta = "不变"
            else:
                st_delta = f"{st['delta']:+.3f}"
            w(f"| 自耗时 ×Loops ms（推算） | {_ms(st['before'])} | {_ms(st['after'])} "
              f"| {st_delta} |")
            if st["before_status"] != "ok" or st["after_status"] != "ok":
                w(f"| 自耗时限制 | {st['before_status']} | {st['after_status']} | |")
            for f in BUFFER_FIELDS:
                inc = m["buffers_inclusive"][f]
                exc = m["buffers_exclusive"][f]
                vals = [inc["before"], inc["after"], exc["before"], exc["after"]]
                interesting = f in ("Shared Hit Blocks", "Shared Read Blocks")
                if not interesting and not any(v for v in vals):
                    continue
                w(f"| {f}（含子节点） | {_val(inc, 'before')} | {_val(inc, 'after')} | {_change(inc)} |")
                w(f"| {f}（仅自身，推算） | {_val(exc, 'before')} | {_val(exc, 'after')} | {_change(exc)} |")
            w("")
            if m["attributes"]:
                w("属性变化：")
                w("")
                for at in m["attributes"]:
                    w(f"- `{at['field']}`：`{at['before']}` → `{at['after']}`"
                      f"（`{at['before_pointer']}`）")
                w("")

    # -- 6. observations -------------------------------------------------------
    w("## 6. 观测到的变化（事实）")
    w("")
    for o in result["observations"]:
        ev = "；".join(f"`{e['pointer']}`={e['value']}" for e in o["evidence"])
        w(f"- {o['text']}" + (f"（依据：{ev}）" if ev else ""))
    w("")

    # -- 7. possible explanations ----------------------------------------------
    w("## 7. 可能的解释（假设，需人工确认）")
    w("")
    for x in result["possible_explanations"]:
        w(f"- {x}")
    w("")

    # -- 8. limitations ----------------------------------------------------------
    w("## 8. 限制")
    w("")
    for lim in result["limitations"]:
        w(f"- {lim}")
    w("")
    return "\n".join(out)
