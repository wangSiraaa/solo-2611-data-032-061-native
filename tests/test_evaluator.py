"""三值求值语义测试，覆盖需求点名的全部边界场景。"""

import unittest

from sql3v.domain import DomainColumn, Spec
from sql3v.analysis import run
from sql3v.evaluator import evaluate
from sql3v.parser import parse_predicate
from sql3v.trit import Trit

T, F, U = Trit.TRUE, Trit.FALSE, Trit.UNKNOWN


def ev(text: str, env: dict, cols: set | None = None):
    cols = cols if cols is not None else set(env)
    _, tree = parse_predicate(text, cols)
    return evaluate(tree, env)


class ComparisonNullTest(unittest.TestCase):
    def test_any_null_operand_is_unknown(self):
        for op in ("=", "<>", "<", "<=", ">", ">="):
            self.assertEqual(ev(f"x {op} y", {"x": None, "y": 1}), U)
            self.assertEqual(ev(f"x {op} y", {"x": 1, "y": None}), U)
            self.assertEqual(ev(f"x {op} y", {"x": None, "y": None}), U)

    def test_x_equals_x(self):
        """x=x：非 NULL 为 TRUE，NULL 自比较为 UNKNOWN，不是 TRUE。"""
        self.assertEqual(ev("x = x", {"x": 5}), T)
        self.assertEqual(ev("x = x", {"x": None}), U)
        self.assertEqual(ev("x <> x", {"x": 5}), F)
        self.assertEqual(ev("x <> x", {"x": None}), U)

    def test_null_literal_compare(self):
        self.assertEqual(ev("x = NULL", {"x": 1}), U)
        self.assertEqual(ev("NULL = NULL", {}), U)
        self.assertEqual(ev("NULL <> 1", {}), U)

    def test_integer_comparisons(self):
        self.assertEqual(ev("x < 10", {"x": 9}), T)
        self.assertEqual(ev("x <= 10", {"x": 10}), T)
        self.assertEqual(ev("x > 10", {"x": 10}), F)
        self.assertEqual(ev("x >= 10", {"x": 11}), T)
        self.assertEqual(ev("x <> 10", {"x": 10}), F)
        self.assertEqual(ev("-5 < x", {"x": -4}), T)


class IsNullTest(unittest.TestCase):
    def test_is_null_is_not_null(self):
        self.assertEqual(ev("x IS NULL", {"x": None}), T)
        self.assertEqual(ev("x IS NULL", {"x": 1}), F)
        self.assertEqual(ev("x IS NOT NULL", {"x": None}), F)
        self.assertEqual(ev("x IS NOT NULL", {"x": 1}), T)


class InListTest(unittest.TestCase):
    def test_in_basic(self):
        self.assertEqual(ev("x IN (1, 2, 3)", {"x": 2}), T)
        self.assertEqual(ev("x IN (1, 2, 3)", {"x": 9}), F)

    def test_in_with_null_needle_present(self):
        # 命中非 NULL 元素仍是 TRUE
        self.assertEqual(ev("x IN (1, NULL)", {"x": 1}), T)

    def test_in_with_null_miss_is_unknown_not_false(self):
        # 未命中但列表含 NULL -> UNKNOWN（不能当 FALSE）
        self.assertEqual(ev("x IN (1, NULL)", {"x": 2}), U)
        self.assertEqual(ev("x IN (NULL)", {"x": 2}), U)
        self.assertEqual(ev("x IN (NULL)", {"x": None}), U)

    def test_not_in_preserves_unknown(self):
        """NOT IN 含 NULL：UNKNOWN 取三值 NOT 仍是 UNKNOWN，绝不变成 TRUE。"""
        self.assertEqual(ev("x NOT IN (1, NULL)", {"x": 1}), F)
        self.assertEqual(ev("x NOT IN (1, NULL)", {"x": 2}), U)   # 关键：不是 TRUE
        self.assertEqual(ev("x NOT IN (1, NULL)", {"x": None}), U)
        # 不含 NULL 时 NOT IN 才有真正的 TRUE/FALSE
        self.assertEqual(ev("x NOT IN (1, 2)", {"x": 3}), T)
        self.assertEqual(ev("x NOT IN (1, 2)", {"x": 1}), F)
        self.assertEqual(ev("x NOT IN (1, 2)", {"x": None}), U)

    def test_not_in_is_three_valued_not_of_in(self):
        for needle in (None, 1, 2, 3):
            in_v = ev("x IN (1, NULL)", {"x": needle})
            not_in_v = ev("x NOT IN (1, NULL)", {"x": needle})
            self.assertEqual(not_in_v, {T: F, F: T, U: U}[in_v])


class LogicPrecedenceTest(unittest.TestCase):
    def test_and_or_precedence(self):
        # x=1 OR y=2 AND z=3 等价于 x=1 OR (y=2 AND z=3)
        text = "x = 1 OR y = 2 AND z = 3"
        self.assertEqual(ev(text, {"x": 1, "y": None, "z": None}), T)
        self.assertEqual(ev(text, {"x": 0, "y": 2, "z": 3}), T)
        self.assertEqual(ev(text, {"x": 0, "y": 2, "z": 9}), F)
        self.assertEqual(ev(text, {"x": 0, "y": 2, "z": None}), U)

    def test_not_precedence(self):
        # NOT x = 1 是 NOT (x = 1)，而不是 (NOT x) = 1
        self.assertEqual(ev("NOT x = 1", {"x": 2}), T)
        self.assertEqual(ev("NOT x = 1", {"x": 1}), F)
        self.assertEqual(ev("NOT x = 1", {"x": None}), U)

    def test_double_negation(self):
        for v in (None, 1, 2):
            self.assertEqual(
                ev("NOT NOT (x = 1 OR x IS NULL)", {"x": v}),
                ev("x = 1 OR x IS NULL", {"x": v}),
            )

    def test_de_morgan_on_finite_domain(self):
        """De Morgan：NOT (A OR B) 与 NOT A AND NOT B 在三值逻辑下逐项相同。"""
        spec = Spec(
            columns=(DomainColumn("x", (None, 1, 2)), DomainColumn("y", (None, 1, 2))),
            predicate_a="NOT (x = 1 OR y = 2)",
            predicate_b="x <> 1 AND y <> 2",
            raw={},
        )
        from sql3v.analysis import run
        result = run(spec)
        self.assertTrue(result.value_equivalent)
        self.assertTrue(result.filter_equivalent)

    def test_false_and_unknown_differs_only_at_value_level(self):
        """一假一未知：筛选层都不收录，值层面必须区分。"""
        # FALSE AND UNKNOWN = FALSE；TRUE AND UNKNOWN = UNKNOWN；WHERE 都不收
        self.assertEqual(ev("x = 1 AND y = 2", {"x": 9, "y": None}), F)
        self.assertEqual(ev("x = 1 AND y = 2", {"x": 1, "y": None}), U)

        # (1) 值等价对照：De Morgan 重写在三值逻辑下逐项 TRUE/FALSE/UNKNOWN 相同
        spec = Spec(
            columns=(DomainColumn("x", (None, 1)), DomainColumn("y", (None, 1))),
            predicate_a="x = 1 AND y = 1",
            predicate_b="NOT (x <> 1 OR y <> 1)",
            raw={},
        )
        result = run(spec)
        self.assertTrue(result.value_equivalent)
        self.assertTrue(result.filter_equivalent)
        self.assertEqual(result.report_a.counts, result.report_b.counts)

        # (2) 只比 WHERE 行数会误判的对：两边收录行数都为 0，
        #     但 FALSE/UNKNOWN 分布不同，值等价必须判“不成立”
        # 手工真值（域 x,y ∈ {NULL,1}，共 4 个赋值，规范序 NULL 在前）：
        #   A: y IS NULL AND x=1 AND x<>1  -> F F U F （T0 F3 U1，收录 0 行）
        #   B: x=1 AND x<>1                 -> F F U U （T0 F2 U2，收录 0 行）
        r_bc = run(Spec(
            columns=spec.columns,
            predicate_a="y IS NULL AND x = 1 AND x <> 1",
            predicate_b="x = 1 AND x <> 1",
            raw={},
        ))
        self.assertEqual(r_bc.report_a.counts, {"TRUE": 0, "FALSE": 3, "UNKNOWN": 1})
        self.assertEqual(r_bc.report_b.counts, {"TRUE": 0, "FALSE": 2, "UNKNOWN": 2})
        self.assertEqual(r_bc.report_a.where_count, r_bc.report_b.where_count)
        self.assertFalse(r_bc.value_equivalent)
        self.assertTrue(r_bc.filter_equivalent)
        self.assertEqual(r_bc.value_counterexample.result_a, F)
        self.assertEqual(r_bc.value_counterexample.result_b, U)


if __name__ == "__main__":
    unittest.main()
