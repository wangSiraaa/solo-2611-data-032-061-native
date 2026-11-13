"""全局最优求解。

主求解器 :func:`exhaustive_optimize` 枚举全部 n! 个左深排列，逐个按
:mod:`planopt.evaluate` 的公式完整求值，绝不使用“最小表优先”之类启发式。
并列时按完整顺序的 table_id Unicode 码点序（Python 字符串默认序即码点序）取最小。

另外提供 :func:`subset_dp_optimize`：基数只依赖已加入的表集合而与加入顺序
无关，因此可以用子集动态规划独立求最优，作为交叉校验引擎。
"""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass
from fractions import Fraction

from .evaluate import Step, evaluate_order
from .model import Problem


@dataclass(frozen=True)
class OptimalPlan:
    order: tuple[str, ...]
    steps: tuple[Step, ...]
    cost: Fraction
    final_card: Fraction
    evaluated_permutations: int
    elapsed_seconds: float
    engine: str


def exhaustive_optimize(problem: Problem) -> OptimalPlan:
    """枚举全部 n! 个排列求全局最优；这是报告中使用的主结果。

    这是*字面意义*的全排列穷举（``itertools.permutations``），每个排列都
    独立完整求值并计入 ``evaluated_permutations``，不做任何剪枝或记忆化，
    因而搜索量统计是真实的 n!，也与子集 DP 求解器保持实现异构、可互相证伪。
    热路径用预计算邻接表做增量求值，公式与 :mod:`planopt.evaluate` 完全
    相同；最优计划最终仍交给 evaluate_order 生成权威 Step 记录。
    """
    start = time.perf_counter()
    ids = problem.table_ids
    rows = {t: Fraction(problem.rows[t]) for t in ids}
    index = {t: i for i, t in enumerate(ids)}
    # 邻接表（位掩码 + 选择率）：adj[t] = [(neighbor_bit, selectivity), ...]
    adj: dict[str, list[tuple[int, Fraction]]] = {t: [] for t in ids}
    for cond in problem.conditions:
        adj[cond.left].append((1 << index[cond.right], cond.selectivity))
        adj[cond.right].append((1 << index[cond.left], cond.selectivity))
    frac = Fraction
    perms = itertools.permutations(ids)

    best_order: tuple[str, ...] | None = None
    best_cost: frac | None = None
    count = 0

    for order in perms:  # ids 已按码点序，枚举确定
        count += 1
        first = order[0]
        card = rows[first]
        cost = card
        joined_mask = 1 << index[first]
        for table in order[1:]:
            product = frac(1)
            for neighbor_bit, sel in adj[table]:
                if joined_mask & neighbor_bit:  # 边仅在两端首次同时出现时计入
                    product *= sel
            new_card = card * rows[table] * product
            cost += card + rows[table] + new_card
            card = new_card
            joined_mask |= 1 << index[table]
        if best_cost is None or cost < best_cost or (cost == best_cost and order < best_order):
            best_cost = cost
            best_order = order

    elapsed = time.perf_counter() - start
    best_steps = evaluate_order(problem, best_order)  # 权威逐步记录
    return OptimalPlan(
        order=best_order,
        steps=best_steps,
        cost=best_cost,
        final_card=best_steps[-1].output_cardinality,
        evaluated_permutations=count,
        elapsed_seconds=elapsed,
        engine="exhaustive_permutations",
    )


def subset_dp_optimize(problem: Problem) -> OptimalPlan:
    """子集 DP 独立求解 O(2^n n^2)，用于与穷举结果互相核对。

    状态 dp[S] = 恰好加入集合 S 时的最小累计成本；同时记录达到该成本的
    码点序最小完整顺序，用于并列裁决核对。
    """
    start = time.perf_counter()
    ids = problem.table_ids
    index = {t: i for i, t in enumerate(ids)}
    full = (1 << problem.n) - 1

    # 基数只与集合有关，按集合递推（与顺序无关）
    card: dict[int, Fraction] = {}
    for i, tid in enumerate(ids):
        card[1 << i] = Fraction(problem.rows[tid])
    for mask in range(1, full + 1):
        if mask in card:
            continue
        # 任选集合中一个表作为“最后加入”的表来算基数；结果与选择无关
        bit = mask & -mask
        i = bit.bit_length() - 1
        prev = mask ^ bit
        tid = ids[i]
        product = Fraction(1)
        m = prev
        while m:
            b = m & -m
            j = b.bit_length() - 1
            cond = problem.cond_map().get(tuple(sorted((tid, ids[j]))))
            if cond is not None:
                product *= cond.selectivity
            m ^= b
        card[mask] = card[prev] * problem.rows[tid] * product

    INF = None
    dp_cost: dict[int, Fraction] = {}
    dp_order: dict[int, tuple[str, ...]] = {}
    for i, tid in enumerate(ids):
        dp_cost[1 << i] = Fraction(problem.rows[tid])
        dp_order[1 << i] = (tid,)

    for mask in range(1, full + 1):
        if mask & (mask - 1) == 0:
            continue  # 单表已初始化
        best_cost = INF
        best_order: tuple[str, ...] | None = None
        m = mask
        while m:
            bit = m & -m
            i = bit.bit_length() - 1
            prev = mask ^ bit
            tid = ids[i]
            candidate = dp_cost[prev] + card[prev] + problem.rows[tid] + card[mask]
            candidate_order = dp_order[prev] + (tid,)
            if best_cost is None or candidate < best_cost or (
                candidate == best_cost and candidate_order < best_order
            ):
                best_cost = candidate
                best_order = candidate_order
            m ^= bit
        dp_cost[mask] = best_cost  # type: ignore[assignment]
        dp_order[mask] = best_order  # type: ignore[assignment]

    elapsed = time.perf_counter() - start
    order = dp_order[full]
    steps = evaluate_order(problem, order)
    return OptimalPlan(
        order=order,
        steps=steps,
        cost=dp_cost[full],
        final_card=card[full],
        evaluated_permutations=0,
        elapsed_seconds=elapsed,
        engine="subset_dp",
    )
