"""Assemble the full comparison result: correspondence, metrics,
observations, possible explanations and limitations.

Observed changes and possible explanations are kept in separate lists;
every conclusion carries the JSON pointer and the original value it is
based on.
"""

from __future__ import annotations

from typing import Any

from . import __version__
from .manifest import Capture, compare_manifests
from .match import Correspondence, match_plans
from .metrics import diff_pair, entry, side_metrics
from .model import PlanDoc, PlanNode, load_plan

EST_RATIO_FLAG = 10.0  # per-loop actual/plan outside [1/10, 10] is flagged


def _fmtv(v) -> str:
    """Compact number for observation text: 6.0 -> '6', 3.537 -> '3.537'."""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _node_brief(n: PlanNode) -> dict[str, Any]:
    return {
        "pointer": n.pointer,
        "node_type": n.node_type,
        "label": n.label(),
        "context": n.context_path(),
        "supported": n.supported,
        "relation_set": sorted(n.relation_set),
    }


def _correspondence_entry(c: Correspondence) -> dict[str, Any]:
    e: dict[str, Any] = {
        "id": c.cid,
        "status": c.status,
        "evidence": c.evidence,
        "structural_changes": c.changes,
    }
    if c.status == "matched":
        e["before"] = _node_brief(c.before)
        e["after"] = _node_brief(c.after)
        e["metrics"] = diff_pair(c.before, c.after)
    elif c.status in ("added", "removed"):
        node = c.after or c.before
        e["before"] = _node_brief(c.before) if c.before else None
        e["after"] = _node_brief(c.after) if c.after else None
        e["metrics"] = side_metrics(node)
    else:  # ambiguous
        e["candidates_before"] = [_node_brief(n) for n in c.candidates_before]
        e["candidates_after"] = [_node_brief(n) for n in c.candidates_after]
    return e


def _plan_level(before: PlanDoc, after: PlanDoc) -> dict[str, Any]:
    rb = side_metrics(before.root)
    ra = side_metrics(after.root)
    return {
        "execution_time_ms": entry(
            before.execution_time, after.execution_time,
            before.execution_pointer, after.execution_pointer),
        "planning_time_ms": entry(
            before.planning_time, after.planning_time,
            before.planning_pointer, after.planning_pointer),
        "root_node_type": {
            "before": before.root.node_type,
            "after": after.root.node_type,
            "before_pointer": before.root.field_pointer("Node Type"),
            "after_pointer": after.root.field_pointer("Node Type"),
        },
        "result_rows_xloops": entry(
            rb["actual_rows_total_xloops"], ra["actual_rows_total_xloops"],
            before.root.field_pointer("Actual Rows"),
            after.root.field_pointer("Actual Rows"),
            "根节点 Actual Rows × Actual Loops", derived=True),
    }


def _observations(plan_level, pairs: list[dict]) -> list[dict]:
    """Facts only — no causal claims.  Each item lists its evidence."""
    obs: list[dict] = []

    def ev(pointer, value):
        return {"pointer": pointer, "value": value}

    rr = plan_level["result_rows_xloops"]
    obs.append({
        "text": (f"结果行数（根节点 ×Loops）：{_fmtv(rr['before'])} → {_fmtv(rr['after'])}"
                 + ("，结果集行数一致" if rr["before"] == rr["after"] else "，结果集行数不同")),
        "evidence": [ev(rr["before_pointer"], rr["before"]),
                     ev(rr["after_pointer"], rr["after"])],
    })
    et = plan_level["execution_time_ms"]
    if et["delta"] is not None:
        pct = f"（{(et['ratio'] - 1):+.1%}）" if et["ratio"] else ""
        obs.append({
            "text": f"Execution Time：{et['before']} ms → {et['after']} ms"
                    f"（{et['delta']:+.3f} ms{pct}）；单次运行，仅作观测",
            "evidence": [ev(et["before_pointer"], et["before"]),
                         ev(et["after_pointer"], et["after"])],
        })
    pt = plan_level["planning_time_ms"]
    if pt["delta"] is not None:
        obs.append({
            "text": f"Planning Time：{pt['before']} ms → {pt['after']} ms"
                    f"（{pt['delta']:+.3f} ms）",
            "evidence": [ev(pt["before_pointer"], pt["before"]),
                         ev(pt["after_pointer"], pt["after"])],
        })

    matched = [p for p in pairs if p["status"] == "matched"]
    added = [p for p in pairs if p["status"] == "added"]
    removed = [p for p in pairs if p["status"] == "removed"]
    ambiguous = [p for p in pairs if p["status"] == "ambiguous"]
    type_changed = [p for p in matched
                    if p["before"]["node_type"] != p["after"]["node_type"]]
    obs.append({
        "text": (f"节点对应：{len(matched)} 对对应（其中 {len(type_changed)} 对节点类型变化），"
                 f"{len(added)} 个新增，{len(removed)} 个消失，{len(ambiguous)} 组无法唯一对应"),
        "evidence": [],
    })
    for p in matched:
        m = p["metrics"]
        loops = m["actual_loops"]
        if loops["before"] != loops["after"]:
            obs.append({
                "text": (f"{p['before']['label']}：Actual Loops "
                         f"{_fmtv(loops['before'])} → {_fmtv(loops['after'])}"),
                "evidence": [ev(loops["before_pointer"], loops["before"]),
                             ev(loops["after_pointer"], loops["after"])],
            })
        for side, ratio in (("before", m["est_ratio_per_loop"]["before"]),
                            ("after", m["est_ratio_per_loop"]["after"])):
            if ratio is not None and (ratio >= EST_RATIO_FLAG or ratio <= 1 / EST_RATIO_FLAG):
                node = p[side]
                obs.append({
                    "text": (f"{side} 侧 {node['label']} 估计偏差 {ratio:.2f}×"
                             f"（每循环 实际/估计）"),
                    "evidence": [ev(m["est_ratio_per_loop"][f"{side}_pointer"], ratio)],
                })
        # Physical reads: use exclusive (self) buffers so the same blocks are
        # not reported again at every ancestor; fall back to inclusive only
        # when the exclusive value cannot be derived.
        read = m["buffers_exclusive"]["Shared Read Blocks"]
        scope = "仅自身，推算"
        if read["before"] is None or read["after"] is None:
            read = m["buffers_inclusive"]["Shared Read Blocks"]
            scope = "含子节点"
        if (read["before"], read["after"]) != (None, None) and read["before"] != read["after"]:
            obs.append({
                "text": (f"{p['before']['label']}：Shared Read Blocks（{scope}）"
                         f"{_fmtv(read['before'])} → {_fmtv(read['after'])}"),
                "evidence": [ev(read["before_pointer"], read["before"]),
                             ev(read["after_pointer"], read["after"])],
            })

    movers = []
    for p in matched:
        d = p["metrics"]["self_time_xloops_ms"]["delta"]
        if d is not None:
            movers.append((d, p))
    movers.sort(key=lambda x: -x[0])
    for d, p in movers[:3]:
        if d <= 0:
            continue
        s = p["metrics"]["self_time_xloops_ms"]
        note = "；该对节点存在树中位置变化，口径需谨慎" if any(
            "位置变化" in ch for ch in p["structural_changes"]) else ""
        obs.append({
            "text": (f"自耗时（推算）增加：{p['before']['label']} → {p['after']['label']} "
                     f"{s['before']} ms → {s['after']} ms（{d:+.3f} ms）{note}"),
            "evidence": [ev(s["before_pointer"], p["metrics"]["actual_total_time_xloops_ms"]["before"]),
                         ev(s["after_pointer"], p["metrics"]["actual_total_time_xloops_ms"]["after"])],
        })
    return obs


def _explanations(comparability, pairs: list[dict]) -> list[str]:
    """Hypotheses only — separated from observations, never asserted."""
    expl: list[str] = []
    for check in comparability.get("checks", []):
        if check.get("check") == "capture_settings" and check.get("result") == "differ":
            expl.append(
                "两次采集的执行设置不同（before: "
                f"{check.get('before')!r}；after: {check.get('after')!r}）："
                "计划结构差异可能由设置驱动，需人工确认")
        if check.get("check") == "data_snapshot" and check.get("result") == "mismatch":
            expl.append("数据快照不同：行数与缓冲差异可能来自数据本身，而非计划")
    for p in pairs:
        if p["status"] != "matched":
            continue
        est = p["metrics"]["est_ratio_per_loop"]
        for side in ("before", "after"):
            r = est[side]
            if r is not None and (r >= EST_RATIO_FLAG or r <= 1 / EST_RATIO_FLAG):
                expl.append(
                    f"{side} 侧 {p[side]['label']} 估计偏差 {r:.2f}×："
                    "统计信息可能过时（需人工核实，本工具不执行 ANALYZE）")
                break
    expl.append("本工具不判断因果；以上仅为待人工核实的假设")
    return expl


def _limitations(before: PlanDoc, after: PlanDoc, pairs: list[dict]) -> list[str]:
    lim = [
        "Actual Rows / Actual Total Time 为每次循环值；报告以 ×Loops 标注合计",
        "父节点时间与缓冲包含子节点；总耗时只取根节点 / Execution Time，未将各节点时间相加",
        "自耗时为推算值（自身×Loops − 子节点×Loops）；字段缺失、节点未执行或存在并行 Worker 时不推算并保留原因",
        "Total Cost 为优化器估计单位，不能当作毫秒",
        "本工具离线分析两份计划文件，不连接数据库、不推荐索引、不改写 SQL",
    ]
    for doc, side in ((before, "before"), (after, "after")):
        unknown = [n for n in doc.nodes if not n.supported]
        if unknown:
            lim.append(
                f"{side} 含未知边界节点（不做类型特定解释）："
                + "、".join(f"{n.node_type}({n.pointer})" for n in unknown))
    for p in pairs:
        if p["status"] != "matched":
            continue
        st = p["metrics"]["self_time_xloops_ms"]
        for side in ("before", "after"):
            if st[f"{side}_status"] != "ok":
                lim.append(f"{p['id']} {side} 自耗时不可用：{st[f'{side}_status']}")
        bs = p["metrics"]["buffers_exclusive_status"]
        for side in ("before", "after"):
            if bs[side] != "ok":
                lim.append(f"{p['id']} {side} 仅自身缓冲：{bs[side]}")
    return lim


def analyze(before_path: str, after_path: str,
            before_cap: Capture | None = None,
            after_cap: Capture | None = None) -> dict[str, Any]:
    before = load_plan(before_path)
    after = load_plan(after_path)
    correspondences = match_plans(before, after)
    comparability = compare_manifests(before_cap, after_cap)
    pairs = [_correspondence_entry(c) for c in correspondences]
    plan_level = _plan_level(before, after)

    return {
        "tool": "plancompare",
        "version": __version__,
        "inputs": {
            "before": before_path,
            "after": after_path,
            "before_capture": before_cap.path if before_cap else None,
            "after_capture": after_cap.path if after_cap else None,
        },
        "comparability": comparability,
        "plan_level": plan_level,
        "correspondence": pairs,
        "observations": _observations(plan_level, pairs),
        "possible_explanations": _explanations(comparability, pairs),
        "limitations": _limitations(before, after, pairs),
    }
