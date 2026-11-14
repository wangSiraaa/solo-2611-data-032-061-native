"""Metric extraction and paired comparison.

Display conventions (also surfaced in the report):
* "Actual Rows" and "Actual Total Time" are per-loop values; fields named
  ``*_xloops`` multiply them by "Actual Loops".
* A parent's time and buffer counts include its children, so node times
  must never be summed into a total; self time is derived only when every
  child contributes reliable numbers, otherwise the limitation is kept.
* "Total Cost" is a planner estimate in arbitrary units, not milliseconds.
"""

from __future__ import annotations

from typing import Any

from .model import PlanNode

BUFFER_FIELDS = (
    "Shared Hit Blocks",
    "Shared Read Blocks",
    "Shared Dirtied Blocks",
    "Shared Written Blocks",
    "Local Hit Blocks",
    "Local Read Blocks",
    "Local Dirtied Blocks",
    "Local Written Blocks",
    "Temp Read Blocks",
    "Temp Written Blocks",
)

# Type-specific attributes surfaced as evidence when they differ.
ATTRIBUTE_FIELDS = {
    "Seq Scan": ("Filter", "Rows Removed by Filter", "Relation Name", "Alias"),
    "Index Scan": (
        "Index Name", "Index Cond", "Filter", "Rows Removed by Filter",
        "Rows Removed by Index Recheck", "Scan Direction", "Relation Name", "Alias",
    ),
    "Sort": ("Sort Key", "Sort Method", "Sort Space Used", "Sort Space Type"),
    "Aggregate": ("Strategy", "Group Key", "Partial Mode"),
    "Hash": ("Hash Buckets", "Hash Batches", "Peak Memory Usage"),
    "Nested Loop": ("Join Type", "Inner Unique", "Join Filter", "Rows Removed by Join Filter"),
    "Hash Join": (
        "Join Type", "Hash Cond", "Inner Unique", "Join Filter",
        "Rows Removed by Join Filter",
    ),
    "Merge Join": ("Join Type", "Merge Cond", "Inner Unique"),
}


def _num(node: PlanNode, key: str) -> float | None:
    v = node.raw.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v)


def entry(bv, av, bptr, aptr, note: str | None = None,
          derived: bool = False) -> dict[str, Any]:
    """One compared scalar with provenance back to both source files.

    ``derived`` marks values computed from the pointed-at fields (e.g.
    multiplied by loops) rather than read directly.
    """
    e: dict[str, Any] = {
        "before": bv,
        "after": av,
        "before_pointer": bptr,
        "after_pointer": aptr,
        "delta": None,
        "ratio": None,
    }
    if derived:
        e["derived"] = True
    if isinstance(bv, (int, float)) and isinstance(av, (int, float)):
        e["delta"] = round(av - bv, 6)
        if bv:
            e["ratio"] = round(av / bv, 6)
    if note:
        e["note"] = note
    return e


def self_time_ms(node: PlanNode) -> tuple[float | None, str]:
    """Inclusive-minus-children self time in ms (all loops), or (None, reason)."""
    if node.get("Actual Loops") == 0:
        return 0.0, "节点未执行（Actual Loops=0）"
    total, loops = _num(node, "Actual Total Time"), _num(node, "Actual Loops")
    if total is None or loops is None:
        return None, "缺少 Actual Total Time / Actual Loops"
    if node.get("Workers"):
        return None, "存在并行 Worker，无法可靠分摊，未推算"
    value = total * loops
    for child in node.children:
        if child.get("Actual Loops") == 0:
            continue
        ct, cl = _num(child, "Actual Total Time"), _num(child, "Actual Loops")
        if ct is None or cl is None:
            return None, f"子节点 {child.pointer} 缺少计时字段，未推算"
        value -= ct * cl
    if value < -0.0005:
        return round(value, 6), "为负（计时噪声/取整），原样保留"
    return round(max(value, 0.0), 6), "ok"


def exclusive_buffers(node: PlanNode) -> tuple[dict[str, float | None], str]:
    """Per-field buffer counts minus the node's own children."""
    if not any(f in node.raw for f in BUFFER_FIELDS):
        return {}, "未采集缓冲（缺少 BUFFERS 字段）"
    out: dict[str, float | None] = {}
    incomplete = False
    for f in BUFFER_FIELDS:
        v = _num(node, f)
        if v is None:
            out[f] = None
            incomplete = True
            continue
        value: float | None = v
        for child in node.children:
            cv = _num(child, f)
            if cv is None:
                value = None
                incomplete = True
                break
            value -= cv
        out[f] = None if value is None else round(value, 6)
    status = "部分字段或子节点缺字段，仅部分可推算" if incomplete else "ok"
    return out, status


def side_metrics(node: PlanNode) -> dict[str, Any]:
    """Metrics of a single node (no comparison)."""
    loops = _num(node, "Actual Loops")
    rows = _num(node, "Actual Rows")
    time_ms = _num(node, "Actual Total Time")
    plan_rows = _num(node, "Plan Rows")
    executed = node.get("Actual Loops") != 0 and loops is not None
    total_rows = rows * loops if rows is not None and loops is not None else None
    total_time = time_ms * loops if time_ms is not None and loops is not None else None
    est = None
    if executed and plan_rows and rows is not None:
        est = round(rows / plan_rows, 6)
    self_ms, self_status = self_time_ms(node)
    excl, excl_status = exclusive_buffers(node)
    return {
        "executed": executed,
        "plan_rows": plan_rows,
        "actual_rows_per_loop": rows,
        "actual_loops": loops,
        "actual_rows_total_xloops": total_rows,
        "est_ratio_per_loop": est,
        "actual_total_time_per_loop_ms": time_ms,
        "actual_total_time_xloops_ms": total_time,
        "self_time_xloops_ms": self_ms,
        "self_time_status": self_status,
        "startup_time_ms": _num(node, "Actual Startup Time"),
        "total_cost": _num(node, "Total Cost"),
        "buffers_inclusive": {f: _num(node, f) for f in BUFFER_FIELDS},
        "buffers_exclusive": excl,
        "buffers_exclusive_status": excl_status,
    }


def _attribute_diffs(b: PlanNode, a: PlanNode) -> list[dict[str, Any]]:
    keys: list[str] = []
    for t in (b.node_type, a.node_type):
        for k in ATTRIBUTE_FIELDS.get(t, ()):
            if k not in keys:
                keys.append(k)
    out = []
    for k in keys:
        vb, va = b.get(k), a.get(k)
        if vb != va:
            out.append({
                "field": k,
                "before": vb,
                "after": va,
                "before_pointer": b.field_pointer(k),
                "after_pointer": a.field_pointer(k),
            })
    return out


def diff_pair(b: PlanNode, a: PlanNode) -> dict[str, Any]:
    """Full metric comparison of one matched pair."""
    mb, ma = side_metrics(b), side_metrics(a)

    def ent(key: str, field_name: str, note: str | None = None,
            derived: bool = False) -> dict[str, Any]:
        return entry(mb[key], ma[key],
                     b.field_pointer(field_name), a.field_pointer(field_name),
                     note, derived=derived)

    self_b, self_a = mb["self_time_xloops_ms"], ma["self_time_xloops_ms"]
    metrics: dict[str, Any] = {
        "convention": (
            "Actual Rows/Actual Total Time 为每次循环值；*_xloops 为乘 Actual Loops "
            "后的合计；父节点数值包含子节点"
        ),
        "plan_rows": ent("plan_rows", "Plan Rows"),
        "actual_rows_per_loop": ent("actual_rows_per_loop", "Actual Rows"),
        "actual_loops": ent("actual_loops", "Actual Loops"),
        "actual_rows_total_xloops": ent(
            "actual_rows_total_xloops", "Actual Rows",
            "由 Actual Rows × Actual Loops 推算", derived=True),
        "est_ratio_per_loop": ent(
            "est_ratio_per_loop", "Plan Rows",
            "每循环 Actual Rows / Plan Rows；>1 表示低估", derived=True),
        "actual_total_time_per_loop_ms": ent(
            "actual_total_time_per_loop_ms", "Actual Total Time", "含子节点"),
        "actual_total_time_xloops_ms": ent(
            "actual_total_time_xloops_ms", "Actual Total Time",
            "含子节点；由 Actual Total Time × Actual Loops 推算", derived=True),
        "self_time_xloops_ms": {
            "before": self_b,
            "after": self_a,
            "before_pointer": b.field_pointer("Actual Total Time"),
            "after_pointer": a.field_pointer("Actual Total Time"),
            "before_status": mb["self_time_status"],
            "after_status": ma["self_time_status"],
            "delta": (round(self_a - self_b, 6)
                      if self_b is not None and self_a is not None else None),
            "derived": True,
            "note": "自耗时=自身×Loops总时间−子节点×Loops总时间；不可推算时保留原因",
        },
        "total_cost": ent("total_cost", "Total Cost", "优化器估计单位，不是毫秒"),
        "buffers_inclusive": {
            f: entry(mb["buffers_inclusive"][f], ma["buffers_inclusive"][f],
                     b.field_pointer(f), a.field_pointer(f), "含子节点")
            for f in BUFFER_FIELDS
        },
        "buffers_exclusive": {
            f: entry(mb["buffers_exclusive"].get(f), ma["buffers_exclusive"].get(f),
                     b.field_pointer(f), a.field_pointer(f), "推算：自身−子节点",
                     derived=True)
            for f in BUFFER_FIELDS
        },
        "buffers_exclusive_status": {
            "before": mb["buffers_exclusive_status"],
            "after": ma["buffers_exclusive_status"],
        },
        "attributes": _attribute_diffs(b, a),
    }
    return metrics
