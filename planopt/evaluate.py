"""左深计划求值（题面公式的唯一权威实现）。

设当前顺序为 [t0, t1, ..., tk]：

* 初始中间基数 C0 = rows(t0)
* 加入 T = ti (i>=1) 时，令 S_i 为 T 与所有已加入表之间的条件集合，
  输出基数 C_i = C_{i-1} * rows(T) * Π sel(c), c ∈ S_i
  （没有条件即笛卡尔积，乘积为 1）
* 初始成本 cost_0 = rows(t0)
* 第 i 步成本 cost_i = C_{i-1} + rows(T) + C_i
* 总成本 = Σ cost_i

所有基数与成本均为 Fraction，绝不中间取整；零行、零选择率自然得到零。
"""

from dataclasses import dataclass
from fractions import Fraction

from .model import Problem


@dataclass(frozen=True)
class Step:
    position: int                       # 0 表示首表
    table_id: str
    applied_conditions: tuple           # tuple[Condition]，本步新应用的边
    input_cardinality: Fraction         # 首表为 0（没有输入关系）
    output_cardinality: Fraction
    step_cost: Fraction


def evaluate_order(problem: Problem, order: tuple[str, ...]) -> tuple[Step, ...]:
    """对一个完整排列求值，返回逐步记录。"""
    steps: list[Step] = []
    first = order[0]
    current = Fraction(problem.rows[first])
    steps.append(Step(0, first, (), Fraction(0), current, current))

    joined = frozenset({first})
    for position, table in enumerate(order[1:], start=1):
        conds = problem.selectivities_between(table, joined)
        selectivity_product = Fraction(1)
        for cond in conds:
            selectivity_product *= cond.selectivity
        output = current * problem.rows[table] * selectivity_product
        step_cost = current + problem.rows[table] + output
        steps.append(
            Step(position, table, conds, current, output, step_cost)
        )
        current = output
        joined |= frozenset({table})
    return tuple(steps)


def total_cost(steps: tuple[Step, ...]) -> Fraction:
    return sum((s.step_cost for s in steps), Fraction(0))


def final_cardinality(steps: tuple[Step, ...]) -> Fraction:
    return steps[-1].output_cardinality


def recompute_prefix_cardinality(problem: Problem, prefix: tuple[str, ...]) -> Fraction:
    """独立地按表集合前缀重新计算基数（供验证器交叉核对）。

    从头重放，不依赖任何缓存结果。
    """
    card = Fraction(problem.rows[prefix[0]])
    joined = {prefix[0]}
    for table in prefix[1:]:
        product = Fraction(1)
        for cond in problem.conditions:
            if (cond.left == table and cond.right in joined) or (
                cond.right == table and cond.left in joined
            ):
                product *= cond.selectivity
        card = card * problem.rows[table] * product
        joined.add(table)
    return card


def recompute_prefix_cost(problem: Problem, prefix: tuple[str, ...]) -> Fraction:
    """独立地重算某前缀的累计总成本（逐前缀重算，验证器使用）。"""
    card = Fraction(problem.rows[prefix[0]])
    cost = card
    joined = {prefix[0]}
    for table in prefix[1:]:
        product = Fraction(1)
        for cond in problem.conditions:
            if (cond.left == table and cond.right in joined) or (
                cond.right == table and cond.left in joined
            ):
                product *= cond.selectivity
        new_card = card * problem.rows[table] * product
        cost += card + problem.rows[table] + new_card
        card = new_card
        joined.add(table)
    return cost
