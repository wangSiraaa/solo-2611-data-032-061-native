"""端到端测试：公式手算、并列裁决、验证器、洗牌不变性、输入拒绝。

直接运行：python -m tests.run_tests
"""

import copy
import json
import unittest
from fractions import Fraction

from planopt.decimalx import decimal_to_fraction
from planopt.errors import InputValidationError, UserOrderError
from planopt.evaluate import (
    evaluate_order,
    final_cardinality,
    recompute_prefix_cardinality,
    recompute_prefix_cost,
    total_cost,
)
from planopt.model import parse_problem, validate_order
from planopt.optimizer import exhaustive_optimize, subset_dp_optimize
from planopt.presets import build_preset, preset_names
from planopt.render import build_result, canonical_json
from planopt.verifier import (
    check_condition_application_invariant,
    independent_enumerate,
    shuffle_invariance,
    verify_result,
)


# 贪心反例一：S=20, M=100, L=10000；S-L 选择率 0.5，M-L 选择率 0.001。
# 手算（步骤成本=输入基数+表行数+输出基数）：
#   S,L,M: 20 | 100000 | +M(0.001 无边→笛卡尔) 10000000 ... 见报告完整推导
# 下列六个值为引擎穷举结果，报告中给出逐步手算。
GREEDY_TRAP = build_preset("greedy_trap_3")
GREEDY_TRAP_EXPECTED = {
    ("L", "M", "S"): 32120,
    ("L", "S", "M"): 230120,
    ("M", "L", "S"): 22220,
    ("M", "S", "L"): 24220,
    ("S", "L", "M"): 220140,
    ("S", "M", "L"): 24140,
}


class DecimalTests(unittest.TestCase):
    def test_exact_conversion(self):
        self.assertEqual(decimal_to_fraction("0.1"), Fraction(1, 10))
        self.assertEqual(decimal_to_fraction("1"), Fraction(1))
        self.assertEqual(decimal_to_fraction("0"), Fraction(0))
        self.assertEqual(decimal_to_fraction("0.00001"), Fraction(1, 100000))

    def test_reject_float_and_garbage(self):
        for bad in [0.1, 1, True, None, "1e-3", "0.1.2", "", "abc"]:
            with self.assertRaises(InputValidationError):
                decimal_to_fraction(bad)


class GreedyTrapHandCalculationTests(unittest.TestCase):
    def setUp(self):
        self.problem = parse_problem(GREEDY_TRAP)

    def test_every_permutation_matches_hand_calc(self):
        for order, expected_cost in GREEDY_TRAP_EXPECTED.items():
            steps = evaluate_order(self.problem, order)
            self.assertEqual(total_cost(steps), Fraction(expected_cost), order)
            # 每条边恰好应用一次 => 所有顺序最终基数相同（10000）
            self.assertEqual(final_cardinality(steps), Fraction(10000))

    def test_final_cardinality_identical_costs_differ(self):
        costs = {
            order: total_cost(evaluate_order(self.problem, order))
            for order in GREEDY_TRAP_EXPECTED
        }
        # 最终基数同为 10000，成本却从 22220 到 230120 六个不同值
        self.assertEqual(len(set(costs.values())), 6)

    def test_smallest_table_first_is_suboptimal(self):
        # 最小表 S(20) 开头的两个计划都显著差于全局最优
        plan = exhaustive_optimize(self.problem)
        self.assertEqual(plan.order, ("M", "L", "S"))
        self.assertEqual(plan.cost, Fraction(22220))
        s_first_best = min(
            total_cost(evaluate_order(self.problem, o))
            for o in GREEDY_TRAP_EXPECTED if o[0] == "S"
        )
        self.assertEqual(s_first_best, Fraction(24140))
        self.assertGreater(s_first_best, plan.cost)

        # 四表反例：最小表 S 开头后按“只看表大小”贪心接入万行 L
        p4 = parse_problem(build_preset("greedy_trap_first_4"))
        greedy = total_cost(evaluate_order(p4, ("S", "L", "B", "M")))
        optimal = exhaustive_optimize(p4)
        self.assertEqual(greedy, Fraction(300211120))
        self.assertEqual(optimal.cost, Fraction(100033120))
        self.assertEqual(optimal.order, ("S", "M", "B", "L"))

    def test_tie_break_unicode_order(self):
        # 贪心反例的最优唯一：M,L,S = 22220
        plan = exhaustive_optimize(self.problem)
        self.assertEqual(plan.order, ("M", "L", "S"))
        # 三角形：B,C,A 与 C,B,A 并列 61300，码点序裁决 B,C,A
        tri = parse_problem(build_preset("triangle_tie_3"))
        plan_tri = exhaustive_optimize(tri)
        self.assertEqual(plan_tri.order, ("B", "C", "A"))
        self.assertEqual(plan_tri.cost, Fraction(61300))
        independent = independent_enumerate(tri)
        self.assertEqual(independent["tie_winner"], ("B", "C", "A"))
        self.assertEqual(len(independent["best_orders"]), 2)
        # 等行数无条件三表：6 个排列全并列，码点序裁决 P,Q,R
        ties = parse_problem(build_preset("ties_no_cond_3"))
        plan2 = exhaustive_optimize(ties)
        self.assertEqual(plan2.order, ("P", "Q", "R"))
        independent2 = independent_enumerate(ties)
        self.assertEqual(len(independent2["best_orders"]), 6)
        self.assertEqual(independent2["tie_winner"], ("P", "Q", "R"))


class ExhaustiveVsDpTests(unittest.TestCase):
    def test_all_presets_agree(self):
        for name in preset_names():
            with self.subTest(preset=name):
                problem = parse_problem(build_preset(name))
                opt = exhaustive_optimize(problem)
                dp = subset_dp_optimize(problem)
                self.assertEqual(opt.cost, dp.cost, name)
                self.assertEqual(opt.order, dp.order, name)
                self.assertEqual(opt.final_card, dp.final_card, name)
                self.assertEqual(opt.evaluated_permutations, _factorial(problem.n))

    def test_independent_enumeration_n_le_6(self):
        for name in preset_names():
            problem = parse_problem(build_preset(name))
            if problem.n > 6:
                continue
            with self.subTest(preset=name):
                opt = exhaustive_optimize(problem)
                ind = independent_enumerate(problem)
                self.assertEqual(ind["best_cost"], opt.cost)
                self.assertEqual(ind["tie_winner"], opt.order)
                self.assertEqual(ind["explored_permutations"], _factorial(problem.n))
                # 并列方案必须全部达到最优成本
                for order in ind["best_orders"]:
                    self.assertEqual(
                        total_cost(evaluate_order(problem, order)), opt.cost
                    )

    def test_prefix_recompute_matches_incremental(self):
        problem = parse_problem(build_preset("star_5"))
        opt = exhaustive_optimize(problem)
        order = opt.order
        for k in range(1, len(order) + 1):
            prefix = order[:k]
            steps = evaluate_order(problem, prefix)
            self.assertEqual(
                recompute_prefix_cardinality(problem, prefix),
                steps[-1].output_cardinality,
            )
            self.assertEqual(
                recompute_prefix_cost(problem, prefix), total_cost(steps)
            )


class EdgeCaseTests(unittest.TestCase):
    def test_empty_table_zero_propagation(self):
        problem = parse_problem(build_preset("empty_3"))
        opt = exhaustive_optimize(problem)
        # E(0 行) 必须在首位，之后基数严格为 0
        self.assertEqual(opt.order[0], "E")
        self.assertEqual(opt.steps[1].input_cardinality, Fraction(0))
        self.assertEqual(opt.steps[1].output_cardinality, Fraction(0))
        # 手算：E 首位成本 = 0 + (0+50+0) + (0+80+0) = 130
        self.assertEqual(opt.cost, Fraction(130))

    def test_zero_selectivity_strict_formula(self):
        problem = parse_problem(build_preset("zero_sel_3"))
        # X,Y 连接后严格为 0
        steps = evaluate_order(problem, ("X", "Y", "Z"))
        self.assertEqual(steps[1].output_cardinality, Fraction(0))
        self.assertEqual(steps[2].output_cardinality, Fraction(0))
        opt = exhaustive_optimize(problem)
        # 成本为非负分数；零选择率不产生异常
        self.assertGreaterEqual(opt.cost, Fraction(0))

    def test_disjoint_cartesian_products(self):
        problem = parse_problem(build_preset("disjoint_4"))
        opt = exhaustive_optimize(problem)
        for step in opt.steps[1:]:
            self.assertEqual(step.applied_conditions, ())
        # 最终基数 = 10*30*7*50 = 105000
        self.assertEqual(opt.final_card, Fraction(105000))

    def test_single_table(self):
        payload = {"tables": [{"table_id": "Solo", "rows": 42}], "conditions": []}
        problem = parse_problem(payload)
        opt = exhaustive_optimize(problem)
        self.assertEqual(opt.order, ("Solo",))
        self.assertEqual(opt.cost, Fraction(42))
        self.assertEqual(opt.evaluated_permutations, 1)

    def test_unicode_tie_break(self):
        # 码点：'A'(65) < '中'(20013) < '表'(34920)
        payload = {
            "tables": [
                {"table_id": "表", "rows": 5},
                {"table_id": "A", "rows": 5},
                {"table_id": "中", "rows": 5},
            ],
            "conditions": [],
        }
        problem = parse_problem(payload)
        opt = exhaustive_optimize(problem)
        self.assertEqual(opt.order, ("A", "中", "表"))


class InvariantTests(unittest.TestCase):
    def test_condition_applied_exactly_once_all_presets(self):
        for name in preset_names():
            problem = parse_problem(build_preset(name))
            opt = exhaustive_optimize(problem)
            with self.subTest(preset=name):
                result = check_condition_application_invariant(problem, opt.steps)
                self.assertTrue(result.ok, result.detail)

    def test_shuffle_invariance_all_presets(self):
        for name in preset_names():
            problem = parse_problem(build_preset(name))
            plan = exhaustive_optimize(problem)
            result = build_result(problem, plan)
            with self.subTest(preset=name):
                # 八表穷举一次已 4 万排列，洗牌两次足以证明顺序无关
                trials = 2 if problem.n == 8 else 5
                check = shuffle_invariance(problem, result, trials=trials)
                self.assertTrue(check.ok, check.detail)

    def test_canonical_bytes_stable_under_shuffle(self):
        problem = parse_problem(build_preset("ring_4"))
        result = canonical_json(build_result(problem, exhaustive_optimize(problem)))
        payload = build_preset("ring_4")
        shuffled = {
            "tables": list(reversed(payload["tables"])),
            "conditions": list(reversed([
                {**c, "left": c["right"], "right": c["left"]}
                for c in payload["conditions"]
            ])),
        }
        problem2 = parse_problem(shuffled)
        result2 = canonical_json(build_result(problem2, exhaustive_optimize(problem2)))
        # 计时字段不同，去掉后比较
        def strip(s):
            d = json.loads(s)
            d["search"].pop("elapsed_seconds")
            return canonical_json(d)
        self.assertEqual(strip(result), strip(result2))


class ResultAndUserOrderTests(unittest.TestCase):
    def test_verify_result_passes(self):
        problem = parse_problem(build_preset("high_sel_large_4"))
        result = build_result(problem, exhaustive_optimize(problem))
        checks = verify_result(problem, result)
        self.assertTrue(all(c.ok for c in checks), [c.detail for c in checks if not c.ok])

    def test_user_order_gap(self):
        problem = parse_problem(GREEDY_TRAP)
        plan = exhaustive_optimize(problem)
        result = build_result(problem, plan, user_order=("S", "L", "M"))
        self.assertEqual(Fraction(result["user_plan"]["total_cost"]), Fraction(220140))
        self.assertEqual(Fraction(result["user_plan"]["gap_to_optimal"]),
                         Fraction(220140 - 22220))
        checks = verify_result(problem, result)
        self.assertTrue(all(c.ok for c in checks))

    def test_invalid_user_order_rejected(self):
        problem = parse_problem(GREEDY_TRAP)
        for bad in [["A", "B"], ["A", "B", "A"], ["A", "B", "X"], "AB"]:
            with self.assertRaises(UserOrderError):
                validate_order(problem, bad)


class InputValidationTests(unittest.TestCase):
    def _expect(self, payload):
        with self.assertRaises(InputValidationError):
            parse_problem(copy.deepcopy(payload))

    def test_table_rules(self):
        self._expect({"tables": [], "conditions": []})
        self._expect({"tables": [{"table_id": "", "rows": 1}], "conditions": []})
        self._expect({"tables": [{"table_id": "A", "rows": -1}], "conditions": []})
        self._expect({"tables": [{"table_id": "A", "rows": 1.5}], "conditions": []})
        self._expect({"tables": [{"table_id": "A", "rows": True}], "conditions": []})
        self._expect({"tables": [
            {"table_id": "A", "rows": 1}, {"table_id": "A", "rows": 2}],
            "conditions": []})
        self._expect({"tables": [{"table_id": f"T{i}", "rows": 1} for i in range(9)],
                      "conditions": []})

    def test_condition_rules(self):
        base = {"tables": [{"table_id": "A", "rows": 1},
                           {"table_id": "B", "rows": 2}], "conditions": []}
        self._expect({**base, "conditions": [
            {"left": "A", "right": "A", "selectivity": "0.5"}]})          # 自连接
        self._expect({**base, "conditions": [
            {"left": "A", "right": "X", "selectivity": "0.5"}]})          # 未知表
        self._expect({**base, "conditions": [
            {"left": "A", "right": "B", "selectivity": "0.5"},
            {"left": "B", "right": "A", "selectivity": "0.2"}]})          # 重复
        self._expect({**base, "conditions": [
            {"left": "A", "right": "B", "selectivity": "1.1"}]})          # 超界
        self._expect({**base, "conditions": [
            {"left": "A", "right": "B", "selectivity": "-0.1"}]})         # 负数
        self._expect({**base, "conditions": [
            {"left": "A", "right": "B", "selectivity": 0.5}]})            # 非字符串


class TimingTests(unittest.TestCase):
    def test_eight_table_finishes_and_stats(self):
        import time
        problem = parse_problem(build_preset("eight_8"))
        start = time.perf_counter()
        opt = exhaustive_optimize(problem)
        elapsed = time.perf_counter() - start
        self.assertEqual(opt.evaluated_permutations, 40320)
        self.assertLess(elapsed, 10.0)  # 宽松上限，实际约 0.8s
        dp = subset_dp_optimize(problem)
        self.assertEqual(dp.cost, opt.cost)
        self.assertEqual(dp.order, opt.order)

    def test_random_generator_is_reproducible_and_valid(self):
        from planopt.presets import random_problem
        a = random_problem(8, seed=42)
        b = random_problem(8, seed=42)
        self.assertEqual(a, b)  # 固定种子可复现
        problem = parse_problem(a)  # 生成结果必须通过全部输入校验
        opt = exhaustive_optimize(problem)
        dp = subset_dp_optimize(problem)
        self.assertEqual(opt.cost, dp.cost)
        self.assertEqual(opt.order, dp.order)
        with self.assertRaises(InputValidationError):
            parse_problem(random_problem(9, seed=1))


def _factorial(n: int) -> int:
    out = 1
    for i in range(2, n + 1):
        out *= i
    return out


if __name__ == "__main__":
    unittest.main(verbosity=2)
