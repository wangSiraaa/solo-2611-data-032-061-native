"""核心数据模型、常量与区间几何。

时间约定
--------
输入 JSONL 中所有时刻为**同一可信时钟**的整数纳秒（合成环境保证，无需反推主机偏移）。
缺失的时间戳为 ``None`` —— 全程保持 None，绝不伪造开始时刻、绝不把未知时间填零。
区间统一表示为半开 ``[start, end)``，零长度区间合法（表示瞬时事件，不产生时间片）。

记录类型
--------
- span：``{"kind": "span", "trace_id", "span_id", "service", "operation",
  "start_ns", "end_ns", "parent_id", "span_type", "links": [[span_id, rel]...], ...}``
  span_type ∈ client / server / internal / producer / consumer
- event：``{"kind": "event", "event_id", "trace_id", "span_id", "name", "at_ns", ...}``
- await：``{"kind": "await", "await_id", "trace_id", "waiter_span_id",
  "start_ns", "end_ns"(可缺失=仍未解除), "result_ids": [...], "mode": "all"|"any",
  "resolved_by": [...]（实际解除等待的结果身份，可缺失）}``
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Optional

# ---- span 类型 ----
CLIENT = "client"
SERVER = "server"
INTERNAL = "internal"
PRODUCER = "producer"
CONSUMER = "consumer"
SPAN_TYPES = frozenset({CLIENT, SERVER, INTERNAL, PRODUCER, CONSUMER})

# ---- 事件名（独立的因果事件，任何一种都可能缺失）----
EVT_DISPATCH = "dispatch"          # 调用方派发
EVT_QUEUE_ENTER = "queue_enter"    # 进入下游队列
EVT_EXEC_START = "exec_start"      # 下游开始执行
EVT_ENQUEUE = "enqueue"            # 生产者入队（异步链路）
EVT_CANCEL = "cancel"              # 取消
EVT_TIMEOUT = "timeout"            # 超时

# ---- 覆盖账本时间片类别（四类互斥，穷尽根窗口）----
COV_SELF = "self"        # 本服务自身活动（内部计算 / 未被 await 解释的客户端发送等）
COV_AWAIT = "await"      # 显式 await 记录覆盖的窗口
COV_QUEUE = "queue"      # 有证据的排队等待（派发/入队 → 下游开始执行，且不在 await 内）
COV_GAP = "gap"          # 尚不能解释的空档

# gap 子标签
GAP_UNKNOWN = "unexplained"
GAP_TRANSMIT_MISSING = "missing_server_transmit"  # 有客户端无服务端：未知传输区间（带上下界）

# ---- 资源账本工作类型 ----
RES_EXEC = "execution"            # server/consumer/internal 的执行占用
RES_CLIENT = "client_span"        # 客户端 span 墙钟
RES_PRODUCER = "producer_span"
RES_QUEUE = "queue_wait"          # 队列驻留（资源侧观察）
RES_AWAIT = "explicit_await"      # 等待者自己承担的显式等待
RES_BACKGROUND = "background_after_root"  # 根结束后的消费者残留工作

# ---- 诊断代码 ----
DIAG_QUEUE_WAIT = "D1_queue_wait_before_exec"
DIAG_RESUME_LAG = "D2_result_ready_but_resume_lag"
DIAG_RETRY_AMPLIFICATION = "D3_retry_background_amplification"
DIAG_MISSING_SERVER = "D4_missing_server_unknown_transmit"
DIAG_NESTED_AWAIT_ROOT = "D5_nested_await_real_blocker"
DIAG_ANY_LOSER_WORK = "D6_any_loser_residual_work"
DIAG_TIMEOUT = "D7_timeout_cancel_residual"
DIAG_LONG_GAP = "D8_long_unexplained_gap"
DIAG_CAUSAL_ORDER = "D9_impossible_causal_order"
DIAG_CYCLE = "D10_cycle_or_conflicting_identity"

CERTAIN = "certain"     # 现有事件足以支持确定结论
BOUNDED = "bounded"     # 只能给上下界
UNKNOWN = "unknown"     # 证据缺失，结论退化为未知

# 扫描优先级：数字越小越优先（同一时刻的覆盖归属是确定的，不允许含糊）。
# queue 是 await 窗口内更具体的证据（有派发/入队/执行事件），优先于笼统的 await；
# root_fallback 是根 span 的兜底自身活动，只在没有任何更细证据时使用，
# 排在具体 self 之后 —— 无证据区域仍必须显式暴露为 gap。
PRIORITY = {COV_QUEUE: 0, COV_AWAIT: 1, COV_SELF: 2, "root_fallback": 3, COV_GAP: 4}


@dataclass(frozen=True)
class Interval:
    """半开时间区间 [start, end)，单位纳秒。end 为 None 表示开放区间（等待仍未解除）。"""
    start: int
    end: Optional[int]

    @property
    def duration(self) -> Optional[int]:
        return None if self.end is None else self.end - self.start

    def clip(self, lo: int, hi: int) -> Optional["Interval"]:
        """裁剪进 [lo, hi)；无交集或零长度返回 None。开放区间用 hi 收口（仅用于有界窗口展示）。"""
        s = max(self.start, lo)
        e = min(self.end if self.end is not None else hi, hi)
        return Interval(s, e) if e > s else None


@dataclass
class WorkItem:
    """参与覆盖扫描的一条带优先级的工作区间。"""
    interval: Interval
    category: str                 # COV_*
    sublabel: str = ""
    ref_kind: str = ""            # span / event / await，点击定位原始事件用
    ref_id: str = ""
    service: str = ""
    operation: str = ""

    def __post_init__(self):
        if self.category not in PRIORITY:
            raise ValueError(f"bad category {self.category!r}")

def merge_union(intervals: Iterable[Interval]) -> list[Interval]:
    """区间并集：重叠/相接的区间合并，返回不相交且有序的区间列表。开放区间排最后并保留。"""
    closed, open_start = [], None
    for iv in intervals:
        if iv.end is None:
            open_start = iv.start if open_start is None else min(open_start, iv.start)
        elif iv.end > iv.start:
            closed.append(iv)
    closed.sort(key=lambda x: x.start)
    out: list[Interval] = []
    for iv in closed:
        if out and iv.start <= out[-1].end:
            if iv.end > out[-1].end:
                out[-1] = Interval(out[-1].start, iv.end)
        else:
            out.append(Interval(iv.start, iv.end))
    if open_start is not None:
        s = min(open_start, out[-1].start if out else math.inf)
        # 与并集尾部相接则延长为开放
        if out and open_start <= out[-1].end:
            out[-1] = Interval(out[-1].start, None)
        else:
            out.append(Interval(open_start, None))
    return out


def union_duration(intervals: Iterable[Interval], horizon_end: Optional[int] = None) -> Optional[int]:
    """区间并集的总长度。含开放区间且未给 horizon_end 时返回 None（无法给出确定时长）。"""
    merged = merge_union(intervals)
    total = 0
    for iv in merged:
        if iv.end is None:
            if horizon_end is None:
                return None
            total += max(0, horizon_end - iv.start)
        else:
            total += iv.end - iv.start
    return total


def sweep_coverage(root_start: int, root_end: int, items: list[WorkItem]) -> list[dict]:
    """把 [root_start, root_end) 扫描成互不重叠的覆盖时间片。

    事件点排序：start 在同刻先于 end；同刻多个 start 按 PRIORITY 取最高优先级。
    活跃区间用**多集合**保存（同优先级可同时存在多个，如多个 self span），
    每段取当前最高优先级代表项；gap 由扫描器在无活跃项时自动补出。
    输出时间片字典：start_ns/end_ns/duration_ns/category/sublabel/ref_kind/ref_id/
    service/operation；相邻同归属片段合并。零长度区间不产生时间片。
    """
    events: list[tuple[int, int, int, WorkItem]] = []  # (时刻, 0起1止, 优先级, item)
    for it in items:
        iv = it.interval.clip(root_start, root_end)
        if iv is None:
            continue
        events.append((iv.start, 0, PRIORITY[it.category], it))
        events.append((iv.end, 1, PRIORITY[it.category], it))
    events.sort(key=lambda e: (e[0], e[1], e[2]))

    active: dict[int, list[WorkItem]] = {}   # 优先级 -> 活跃项多集合
    slices: list[dict] = []
    cursor = root_start

    def top() -> Optional[WorkItem]:
        for pri in sorted(active):
            if active[pri]:
                return active[pri][-1]
        return None

    def flush(t: int):
        nonlocal cursor
        if t <= cursor:
            return
        it = top()
        if it is not None:
            if it.category == "root_fallback":
                # 根 span 兜底但无更细证据：这是无法解释的空档，必须显式暴露为 gap，
                # 不能用根 span 的存在把未知区间伪装成自身活动。
                cat, sub = COV_GAP, GAP_UNKNOWN
            else:
                cat, sub = it.category, it.sublabel
        else:
            cat, sub = COV_GAP, GAP_UNKNOWN
        row = {
            "start_ns": cursor, "end_ns": t, "duration_ns": t - cursor,
            "category": cat, "sublabel": sub,
            "ref_kind": it.ref_kind if it else "",
            "ref_id": it.ref_id if it else "",
            "service": it.service if it else "",
            "operation": it.operation if it else "",
        }
        # 仅当两段确实相邻且归属（类别+子标签+证据）完全一致时合并；
        # 不能只按类别合并，否则会把 await 内嵌套的 queue 片段吞掉
        if (slices and slices[-1]["category"] == cat and slices[-1]["sublabel"] == sub
                and slices[-1]["ref_id"] == row["ref_id"]
                and slices[-1]["ref_kind"] == row["ref_kind"]
                and slices[-1]["end_ns"] == cursor):
            slices[-1]["end_ns"] = t
            slices[-1]["duration_ns"] = t - slices[-1]["start_ns"]
        else:
            slices.append(dict(row))
        cursor = t

    for t, kind, pri, it in events:
        flush(t)
        bucket = active.setdefault(pri, [])
        if kind == 0:
            bucket.append(it)
        else:
            # 移除该优先级桶中的一个对应项（同一 item 实例入/出配对）
            for i in range(len(bucket) - 1, -1, -1):
                if bucket[i] is it:
                    bucket.pop(i)
                    break
    flush(root_end)
    return slices


def fmt_ns(ns: Optional[int]) -> str:
    """人类可读时长（仅用于展示，计算一律用整数纳秒）。"""
    if ns is None:
        return "unknown"
    x = float(ns)
    for unit, div in (("ns", 1.0), ("us", 1e3), ("ms", 1e6), ("s", 1e9)):
        if x < 1000 * div or unit == "s":
            return f"{x / div:.3f}{unit}" if unit != "ns" else f"{int(ns)}ns"
    return f"{ns}ns"


def percentile(sorted_values: list[float], pct: float) -> Optional[float]:
    """最近秩百分位（线性插值）。**只对同一总体的观测值调用**——
    合并不同 trace 时严禁把各服务分位数相加冒充总体分位数（见 report.py）。"""
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = (pct / 100.0) * (len(sorted_values) - 1)
    lo = math.floor(pos)
    frac = pos - lo
    hi = min(lo + 1, len(sorted_values) - 1)
    return sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac
