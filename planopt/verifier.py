"""独立验证。

与主求解器不同的实现路径：

1. :func:`independent_enumerate` 用手写 DFS 枚举所有排列（不使用
   itertools.permutations，也不调用 optimizer），并且对每个前缀调用
   ``evaluate.py`` 中的 *重算* 函数重新计算基数与累计成本，
   独立给出最优顺序、最优成本和全部并列方案。
2. :func:`check_condition_application_invariant` 核对每条条件恰好只在
   其两端首次同时出现的那一步应用一次。
3. :func:`verify_result` 对 optimize 产出的规范结果做端到端复核；
4. :func:`shuffle_invariance` 打乱表与条件的输入顺序后重算，比较规范结果。
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from fractions import Fraction
from typing import TYPE_CHECKING

from .evaluate import (
    recompute_prefix_cardinality,
    recompute_prefix_cost,
    total_cost,
)
from .model import Problem, parse_problem
from .optimizer import exhaustive_optimize, subset_dp_optimize

if TYPE_CHECKING:
    from .optimizer import OptimalPlan


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str


def independent_enumerate(problem: Problem) -> dict:
    """独立穷举：手写 DFS + 逐前缀重算。返回最优与全部并列方案。"""
    ids = problem.table_ids
    best_cost: Fraction | None = None
    best_orders: list[tuple[str, ...]] = []
    explored = 0

    def dfs(prefix: list[str], used: int) -> None:
        nonlocal best_cost, best_orders, explored
        if used == (1 << problem.n) - 1:
            explored += 1
            # 每个完整排列都通过“按前缀重算”的路径取得成本
            cost = recompute_prefix_cost(problem, tuple(prefix))
            if best_cost is None or cost < best_cost:
                best_cost = cost
                best_orders = [tuple(prefix)]
            elif cost == best_cost:
                best_orders.append(tuple(prefix))
            return
        for tid in ids:  # 码点序遍历，天然确定
            bit = 1 << ids.index(tid)
            if not used & bit:
                prefix.append(tid)
                # 每个前缀的基数也独立重算一次，确保前缀公式可用
                recompute_prefix_cardinality(problem, tuple(prefix))
                dfs(prefix, used | bit)
                prefix.pop()

    dfs([], 0)
    tie_winner = min(best_orders)  # 完整顺序的码点序裁决
    return {
        "best_cost": best_cost,
        "best_orders": sorted(best_orders),
        "tie_winner": tie_winner,
        "explored_permutations": explored,
    }


def check_condition_application_invariant(problem: Problem, steps) -> CheckResult:
    """每条条件必须且只能在两端首次同时出现的步骤中出现一次。"""
    seen: set[tuple[str, str]] = set()
    joined: set[str] = set()
    for step in steps:
        for cond in step.applied_conditions:
            edge = cond.edge
            if edge in seen:
                return CheckResult("condition_once", False, f"条件 {edge} 被重复应用")
            if step.table_id not in edge or not (set(edge) <= joined | {step.table_id}):
                return CheckResult("condition_once", False, f"条件 {edge} 在非法步骤应用")
            seen.add(edge)
        joined.add(step.table_id)

    expected = {c.edge for c in problem.conditions}
    if seen != expected:
        missing = expected - seen
        extra = seen - expected
        return CheckResult(
            "condition_once", False,
            f"条件集合不一致，缺少 {sorted(missing)}，多余 {sorted(extra)}",
        )
    return CheckResult("condition_once", True, f"{len(seen)} 条条件均恰好在两端首次同时出现时应用一次")


def verify_result(problem: Problem, result: dict) -> list[CheckResult]:
    """对规范 JSON 结果做完整复核。"""
    checks: list[CheckResult] = []
    optimal: OptimalPlan = exhaustive_optimize(problem)
    dp = subset_dp_optimize(problem)

    # 1. 穷举搜索量
    import math
    reported_n = result["search"]["permutations"]
    expected_perms = math.factorial(problem.n)
    checks.append(CheckResult(
        "search_count", reported_n == expected_perms == optimal.evaluated_permutations,
        f"报告 {reported_n} / 期望 {expected_perms} 个排列",
    ))

    # 2. 报告的最优顺序与成本，重放后必须与穷举、DP 三方一致
    order = tuple(result["optimal_plan"]["order"])
    from .evaluate import evaluate_order
    steps = evaluate_order(problem, order)
    replay_cost = total_cost(steps)
    frac = Fraction
    reported_cost = frac(result["optimal_plan"]["total_cost"])
    ok = (
        order == optimal.order
        and replay_cost == optimal.cost == reported_cost
        and dp.cost == optimal.cost
        and dp.order == optimal.order
    )
    checks.append(CheckResult(
        "global_optimum", ok,
        f"穷举最优 {optimal.order} 成本 {optimal.cost}；DP 最优 {dp.order} 成本 {dp.cost}；"
        f"重放顺序 {order} 成本 {replay_cost}",
    ))

    # 3. 逐步数值与序列化值一致
    step_ok = True
    detail: list[str] = []
    for reported_step, live_step in zip(result["optimal_plan"]["steps"], steps):
        if (
            reported_step["table_id"] != live_step.table_id
            or frac(reported_step["output_cardinality"]) != live_step.output_cardinality
            or frac(reported_step["step_cost"]) != live_step.step_cost
            or frac(reported_step["input_cardinality"]) != live_step.input_cardinality
        ):
            step_ok = False
            detail.append(f"步骤 {live_step.position} 不一致")
        applied = tuple(tuple(sorted((c["left"], c["right"]))) for c in reported_step["applied_conditions"])
        live_applied = tuple(c.edge for c in live_step.applied_conditions)
        if applied != live_applied:
            step_ok = False
            detail.append(f"步骤 {live_step.position} 条件列表不一致")
    checks.append(CheckResult("step_values", step_ok, "；".join(detail) or "全部步骤的基数、成本、条件逐值一致"))

    # 4. 条件应用不变量
    checks.append(check_condition_application_invariant(problem, steps))

    # 5. n <= 6：独立穷举器复核全部最优候选与并列裁决
    if problem.n <= 6:
        ind = independent_enumerate(problem)
        tie_ok = (
            ind["tie_winner"] == optimal.order
            and ind["best_cost"] == optimal.cost
            and ind["explored_permutations"] == expected_perms
        )
        checks.append(CheckResult(
            "independent_enumeration", tie_ok,
            f"独立 DFS 穷举 {ind['explored_permutations']} 个排列，"
            f"并列方案 {len(ind['best_orders'])} 个，码点序裁决 {ind['tie_winner']}，成本 {ind['best_cost']}",
        ))
    else:
        checks.append(CheckResult(
            "independent_enumeration", True,
            f"n={problem.n} 超过 6，独立穷举改由子集 DP 精确核对（已在 global_optimum 中完成）",
        ))

    # 6. 用户顺序（若有）
    if "user_plan" in result:
        u_order = tuple(result["user_plan"]["order"])
        u_steps = evaluate_order(problem, u_order)
        u_cost = total_cost(u_steps)
        gap = u_cost - optimal.cost
        user_ok = (
            frac(result["user_plan"]["total_cost"]) == u_cost
            and frac(result["user_plan"]["gap_to_optimal"]) == gap
        )
        checks.append(CheckResult(
            "user_plan", user_ok,
            f"用户顺序 {u_order} 重放成本 {u_cost}，与最优差值 {gap}",
        ))

    return checks


def shuffle_invariance(problem: Problem, reference_result: dict, *, trials: int = 5,
                       seed: int = 20261113) -> CheckResult:
    """打乱表与条件（含端点左右方向）后重新求解，规范结果必须不变。"""
    rng = random.Random(seed)
    ref_opt = reference_result["optimal_plan"]
    for trial in range(trials):
        tables = [{"table_id": t, "rows": problem.rows[t]} for t in problem.table_ids]
        conds = [
            {"left": c.left, "right": c.right, "selectivity": _frac_to_decimal(c.selectivity)}
            for c in problem.conditions
        ]
        rng.shuffle(tables)
        rng.shuffle(conds)
        # 随机翻转端点方向，验证无向条件的规范化
        for c in conds:
            if rng.random() < 0.5:
                c["left"], c["right"] = c["right"], c["left"]
        shuffled = parse_problem({"tables": tables, "conditions": conds})
        plan = exhaustive_optimize(shuffled)
        if list(plan.order) != ref_opt["order"] or str(plan.cost) != ref_opt["total_cost"]:
            return CheckResult(
                "shuffle_invariance", False,
                f"第 {trial + 1} 次洗牌后结果改变: {plan.order} / {plan.cost}",
            )
    return CheckResult("shuffle_invariance", True, f"{trials} 次表/条件/端点方向洗牌后最优结果完全一致")


def _frac_to_decimal(value: Fraction) -> str:
    """把选择率分数无损还原为有限十进制字符串。

    输入选择率都是有限十进制，其最简分母只含质因子 2 和 5，
    因此总能扩成 10 的幂（如 1/2 -> 5/10 -> "0.5"）。
    """
    denominator = value.denominator
    twos = fives = 0
    d = denominator
    while d % 2 == 0:
        d //= 2
        twos += 1
    while d % 5 == 0:
        d //= 5
        fives += 1
    if d != 1:
        # 理论不可达：输入限定为十进制；稳妥起见退回分数形式
        return str(value)
    power = max(twos, fives)
    scaled = value.numerator * (10 ** power // denominator)
    digits = str(abs(scaled)).rjust(power + 1, "0")
    sign = "-" if scaled < 0 else ""
    if power == 0:
        return sign + digits
    return f"{sign}{digits[:-power]}.{digits[-power:]}"
