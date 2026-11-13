"""规范结果渲染。

数值约定：

* 行数、表数量等整数 -> JSON number 整数；
* 基数/成本/选择率等分数 -> 归一化分数字符串 ``"分子/分母"``
  （整除时为 ``"整数"``），保证精确、可往返解析、跨平台字节一致；
* 顶层 JSON 使用 sort_keys + ensure_ascii=False，配合规范化的输入回显，
  同一份问题无论输入顺序如何都产生字节稳定的规范结果。

注意：这些是独立选择率模型下的*估计*基数，不宣称等于真实数据库行数。
"""

import hashlib
import json
from fractions import Fraction

from .evaluate import Step, evaluate_order, final_cardinality, total_cost
from .model import Condition, Problem
from .optimizer import OptimalPlan

SCHEMA_VERSION = "1.0"


def frac_to_str(value: Fraction) -> str:
    if value.denominator == 1:
        return str(value.numerator)
    return f"{value.numerator}/{value.denominator}"


def _condition_json(cond: Condition) -> dict:
    return {
        "left": cond.left,
        "right": cond.right,
        "selectivity": frac_to_str(cond.selectivity),
    }


def _step_json(step: Step) -> dict:
    return {
        "position": step.position,
        "table_id": step.table_id,
        "applied_conditions": [_condition_json(c) for c in step.applied_conditions],
        "input_cardinality": frac_to_str(step.input_cardinality),
        "output_cardinality": frac_to_str(step.output_cardinality),
        "step_cost": frac_to_str(step.step_cost),
    }


def canonical_input(problem: Problem) -> dict:
    """规范化输入回显：表按码点序、条件按边键码点序、选择率为分数字符串。"""
    return {
        "conditions": [_condition_json(c) for c in problem.conditions],
        "tables": [
            {"rows": problem.rows[t], "table_id": t} for t in problem.table_ids
        ],
    }


def build_result(problem: Problem, plan: OptimalPlan, *, user_order: tuple[str, ...] | None = None,
                 raw_input: dict | None = None) -> dict:
    """构造规范结果字典。"""
    result: dict = {
        "schema_version": SCHEMA_VERSION,
        "model_notes": (
            "独立选择率模型下的估计值，非真实数据库行数；"
            "基数与成本均为精确分数，不做中间取整"
        ),
        "input": canonical_input(problem),
        "input_sha256": _input_hash(problem),
        "search": {
            "engine": plan.engine,
            "permutations": plan.evaluated_permutations,
            "table_count": problem.n,
            "elapsed_seconds": round(plan.elapsed_seconds, 6),
        },
        "optimal_plan": {
            "order": list(plan.order),
            "steps": [_step_json(s) for s in plan.steps],
            "final_cardinality": frac_to_str(final_cardinality(plan.steps)),
            "total_cost": frac_to_str(plan.cost),
            "tie_break_rule": "成本并列时按完整连接顺序的 table_id Unicode 码点序取最小",
        },
    }
    if user_order is not None:
        u_steps = evaluate_order(problem, user_order)
        u_cost = total_cost(u_steps)
        result["user_plan"] = {
            "order": list(user_order),
            "steps": [_step_json(s) for s in u_steps],
            "final_cardinality": frac_to_str(final_cardinality(u_steps)),
            "total_cost": frac_to_str(u_cost),
            "gap_to_optimal": frac_to_str(u_cost - plan.cost),
        }
    return result


def canonical_json(result: dict) -> str:
    """字节稳定的规范 JSON 文本。"""
    return json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _input_hash(problem: Problem) -> str:
    blob = canonical_json(canonical_input(problem)).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


# --------------------------------------------------------------------------- #
# 人类可读的 explain 文本
# --------------------------------------------------------------------------- #

def explain_text(problem: Problem, result: dict) -> str:
    lines: list[str] = []
    opt = result["optimal_plan"]
    lines.append("连接顺序解释（左深计划，独立选择率估计模型，非真实行数）")
    lines.append("=" * 64)
    lines.append(f"表数量: {problem.n}；搜索方式: 全排列全局穷举，"
                 f"共评估 {result['search']['permutations']} 个排列"
                 f"（耗时 {result['search']['elapsed_seconds']:.6f}s）")
    lines.append(f"最优顺序: {' -> '.join(opt['order'])}")
    lines.append(f"最终估计基数: {opt['final_cardinality']}；总成本: {opt['total_cost']}")
    lines.append(f"并列裁决: {opt['tie_break_rule']}")
    lines.append("-" * 64)
    for step in opt["steps"]:
        pos = step["position"]
        if pos == 0:
            lines.append(
                f"[步骤 0] 首表 {step['table_id']}（行数 {problem.rows[step['table_id']]}）"
            )
            lines.append(
                f"         初始基数 = {step['output_cardinality']}；初始成本 = {step['step_cost']}"
            )
            continue
        conds = step["applied_conditions"]
        if conds:
            cond_text = "; ".join(
                f"{c['left']}={c['right']} 选择率 {c['selectivity']}" for c in conds
            )
        else:
            cond_text = "无连接条件（合法笛卡尔积，选择率 1）"
        lines.append(f"[步骤 {pos}] 加入 {step['table_id']}（行数 {problem.rows[step['table_id']]}）")
        lines.append(f"         新应用条件: {cond_text}")
        lines.append(
            f"         基数: {step['input_cardinality']} -> {step['output_cardinality']}"
        )
        lines.append(
            f"         步骤成本 = 输入基数 {step['input_cardinality']} "
            f"+ 表行数 {problem.rows[step['table_id']]} "
            f"+ 输出基数 {step['output_cardinality']} = {step['step_cost']}"
        )
    lines.append("-" * 64)
    lines.append(f"总成本 = 各步骤成本之和 = {opt['total_cost']}")

    if "user_plan" in result:
        up = result["user_plan"]
        lines.append("")
        lines.append("用户给定顺序评估")
        lines.append("-" * 64)
        lines.append(f"用户顺序: {' -> '.join(up['order'])}")
        lines.append(f"用户总成本: {up['total_cost']}；最优总成本: {opt['total_cost']}")
        lines.append(f"与最优的差值（用户 - 最优）: {up['gap_to_optimal']}")
        for step in up["steps"]:
            conds = ", ".join(f"{c['left']}-{c['right']}" for c in step["applied_conditions"]) or "笛卡尔积"
            lines.append(
                f"  步骤{step['position']} {step['table_id']}: "
                f"{step['input_cardinality']} -> {step['output_cardinality']}，"
                f"成本 {step['step_cost']}（{conds}）"
            )
    return "\n".join(lines) + "\n"
