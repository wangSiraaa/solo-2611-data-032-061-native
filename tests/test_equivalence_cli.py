"""等价判定、规范反例与 SQLite 交叉核对测试。"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from sql3v.analysis import run
from sql3v.domain import DomainColumn, Spec
from sql3v.sqlite_check import sqlite_probe
from sql3v.parser import parse_predicate
from sql3v.trit import Trit

ROOT = Path(__file__).resolve().parents[1]


def make_spec(columns, a, b):
    return Spec(
        columns=tuple(DomainColumn.normalized(name, values) for name, values in columns),
        predicate_a=a, predicate_b=b, raw={},
    )


class EquivalenceTest(unittest.TestCase):
    def test_value_and_filter_equivalence_can_diverge(self):
        # NOT IN 含 NULL：值不等价（UNKNOWN vs FALSE），但都不被 WHERE 收录
        spec = make_spec([("x", (None, 1, 2))],
                         "x NOT IN (1, NULL)", "x NOT IN (1, 2) AND x IS NOT NULL")
        r = run(spec)
        self.assertFalse(r.value_equivalent)
        self.assertTrue(r.filter_equivalent)
        self.assertIsNone(r.filter_counterexample)
        self.assertEqual(r.value_counterexample.vector(), ("NULL",))

    def test_filter_counterexample_picks_true_divergence(self):
        spec = make_spec(
            [("a", (None, 1, 2)), ("b", (None, 1, 2))],
            "a = 1", "a = 1 OR (a = 2 AND b = 1)",
        )
        r = run(spec)
        self.assertFalse(r.value_equivalent)
        self.assertFalse(r.filter_equivalent)
        # 值反例是首个三值分叉 (a=2,b=NULL)；筛选反例是首个 TRUE 分叉 (a=2,b=1)
        self.assertEqual(r.value_counterexample.vector(), ("2", "NULL"))
        self.assertEqual(r.filter_counterexample.vector(), ("2", "1"))
        self.assertEqual(r.filter_counterexample.result_a, Trit.FALSE)
        self.assertEqual(r.filter_counterexample.result_b, Trit.TRUE)

    def test_canonical_counterexample_independent_of_input_order(self):
        text_a, text_b = "a = 1", "b = 1"
        # 两个规格的域内容完全相同（a、b 均为 {NULL,1,2}），仅列声明顺序
        # 与值书写顺序不同；canonical_columns/枚举器负责归一化
        s1 = make_spec([("b", (2, None, 1)), ("a", (2, 1, None))], text_a, text_b)
        s2 = make_spec([("a", (None, 1, 2)), ("b", (2, 1, None))], text_a, text_b)
        r1, r2 = run(s1), run(s2)
        # 规范枚举：列名字典序 a 先 b 后；每列 null 在前、整数升序
        self.assertEqual(r1.value_counterexample.ordered_columns, ("a", "b"))
        # 手工枚举（A: a=1，B: b=1；域均为 {NULL,1,2}）：
        #   (NULL,NULL): U/U 不分叉
        #   (NULL,1):    U/T  <- 首个值分叉，也是首个 TRUE(筛选)分叉
        #   其余分叉均在其后，故两类反例相同
        self.assertEqual(r1.value_counterexample.vector(), ("NULL", "1"))
        self.assertEqual(r1.value_counterexample.result_a, Trit.UNKNOWN)
        self.assertEqual(r1.value_counterexample.result_b, Trit.TRUE)
        self.assertEqual(r1.value_counterexample.vector(),
                         r2.value_counterexample.vector())
        self.assertEqual(r1.filter_counterexample.vector(),
                         r2.filter_counterexample.vector())
        self.assertEqual(r1.filter_counterexample.vector(), ("NULL", "1"))

    def test_counts_reported_explicitly(self):
        spec = make_spec([("x", (None, 1, 2))], "x = 1", "x IS NULL OR x = 1")
        r = run(spec)
        self.assertEqual(r.traversed, 3)
        self.assertEqual(r.report_a.counts,
                         {"TRUE": 1, "FALSE": 1, "UNKNOWN": 1})
        self.assertEqual(r.report_b.counts,
                         {"TRUE": 2, "FALSE": 1, "UNKNOWN": 0})
        self.assertEqual(r.report_a.where_count, 1)
        self.assertEqual(r.report_b.where_count, 2)

    def test_counterexample_carries_subexpression_steps(self):
        spec = make_spec([("x", (None, 1, 2))],
                         "x NOT IN (1, NULL)", "x NOT IN (1, 2)")
        r = run(spec)
        ce = r.value_counterexample
        self.assertTrue(all("result" in s and "expr" in s for s in ce.steps_a))
        self.assertEqual(ce.steps_a[-1]["result"], Trit.UNKNOWN)
        self.assertEqual(ce.steps_b[-1]["result"], Trit.FALSE)


class SqliteCrossCheckTest(unittest.TestCase):
    def test_probe_matches_python_evaluator_all_cases(self):
        specs = [
            make_spec([("x", (None, 1, 2)), ("y", (None, 1, 2))],
                      "NOT (x = 1 OR y = 2)", "x <> 1 AND y <> 2"),
            make_spec([("x", (None, 1, 2))],
                      "x NOT IN (1, NULL)", "x NOT IN (1, 2)"),
            make_spec([("a", (None, 1, 2)), ("b", (None, 1, 2))],
                      "a = 1 OR (a = 2 AND b IS NOT NULL)",
                      "NOT a IS NULL AND (a < 2 OR b >= 1)"),
        ]
        for spec in specs:
            r = run(spec, cross_check=True)
            self.assertTrue(r.sqlite_verified)
            # 再逐赋值手工比对一次 sqlite_probe
            pa, pb = None, None
            from sql3v.analysis import parse_both
            pa, pb = parse_both(spec)
            for env in spec.enumerate_assignments():
                ta, wa = sqlite_probe(pa.tree, env)
                tb, wb = sqlite_probe(pb.tree, env)
                self.assertEqual(ta, _py(pa.tree, env))
                self.assertEqual(tb, _py(pb.tree, env))
                self.assertEqual(wa, ta is Trit.TRUE)
                self.assertEqual(wb, tb is Trit.TRUE)

    def test_where_acceptance_uses_true_only(self):
        _, tree = parse_predicate("x = 1", {"x"})
        for env, expected in (({"x": 1}, Trit.TRUE), ({"x": 2}, Trit.FALSE),
                              ({"x": None}, Trit.UNKNOWN)):
            trit, accepted = sqlite_probe(tree, env)
            self.assertEqual(trit, expected)
            self.assertEqual(accepted, expected is Trit.TRUE)


def _py(tree, env):
    from sql3v.evaluator import evaluate
    return evaluate(tree, env)


class CliEndToEndTest(unittest.TestCase):
    def _run(self, *args):
        proc = subprocess.run(
            [sys.executable, "-m", "sql3v", *args],
            cwd=ROOT, capture_output=True, text=True, check=False,
        )
        return proc

    def _write_spec(self, data: dict) -> str:
        fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8")
        json.dump(data, fh, ensure_ascii=False)
        fh.close()
        return fh.name

    def setUp(self):
        self.spec_path = self._write_spec({
            "columns": [{"name": "x", "values": [None, 1, 2]}],
            "predicate_a": "x NOT IN (1, NULL)",
            "predicate_b": "x NOT IN (1, 2)",
        })

    def test_compare_text_and_json(self):
        proc = self._run("compare", self.spec_path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("值等价", proc.stdout)
        self.assertIn("筛选等价", proc.stdout)
        self.assertIn("UNKNOWN", proc.stdout)
        proc2 = self._run("compare", self.spec_path, "--json")
        data = json.loads(proc2.stdout)
        self.assertEqual(data["traversed_assignments"], 3)
        self.assertFalse(data["value_equivalence"]["equivalent"])
        self.assertTrue(data["filter_equivalence"]["equivalent"])

    def test_compare_with_sqlite_flag(self):
        proc = self._run("compare", self.spec_path, "--sqlite")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("SQLite 交叉核对", proc.stdout)

    def test_generate_does_not_execute(self):
        proc = self._run("generate", self.spec_path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("AST", proc.stdout)
        self.assertIn("?", proc.stdout)
        data = json.loads(self._run("generate", self.spec_path, "--json").stdout)
        self.assertIn("sqlite_expression", data["predicate_a"])
        self.assertLessEqual(data["predicate_a"]["token_count"], 200)

    def test_explain_assignment(self):
        proc = self._run("explain", self.spec_path, "--assign", "x=2", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout)
        self.assertEqual(data["predicates"]["A"]["final"], "UNKNOWN")
        self.assertEqual(data["predicates"]["B"]["final"], "FALSE")
        # 越界赋值（不在域内）应拒绝
        bad = self._run("explain", self.spec_path, "--assign", "x=99")
        self.assertEqual(bad.returncode, 2)

    def test_verify_command(self):
        proc = self._run("verify", self.spec_path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("真值表", proc.stdout)
        self.assertIn("NOT 3 组", proc.stdout)
        self.assertIn("AND 9 组", proc.stdout)
        self.assertIn("OR 9 组", proc.stdout)

    def test_rejected_inputs_exit_nonzero(self):
        bad_specs = [
            {"columns": [{"name": "x", "values": [1]}],
             "predicate_a": "w = 1", "predicate_b": "x = 1"},                # 未知列
            {"columns": [{"name": "x", "values": [1]}, {"name": "x", "values": [2]}],
             "predicate_a": "x = 1", "predicate_b": "x = 1"},                # 重复列
            {"columns": [{"name": "x", "values": [1, 1]}],
             "predicate_a": "x = 1", "predicate_b": "x = 1"},                # 重复值
            {"columns": [{"name": "x", "values": [1]}],
             "predicate_a": "x + 1", "predicate_b": "x = 1"},                # 算术
            {"columns": [{"name": "x", "values": [1]}],
             "predicate_a": "x = 1; DROP TABLE t", "predicate_b": "x = 1"},  # 多语句
        ]
        for data in bad_specs:
            path = self._write_spec(data)
            proc = self._run("compare", path)
            self.assertEqual(proc.returncode, 2, f"应拒绝: {data}\n{proc.stdout}")
            self.assertIn("错误", proc.stderr)

    def test_stdin_spec(self):
        proc = subprocess.run(
            [sys.executable, "-m", "sql3v", "compare", "-"],
            input=json.dumps({
                "columns": [{"name": "x", "values": [1, None]}],
                "predicate_a": "x IS NULL", "predicate_b": "NOT (x IS NOT NULL)",
            }),
            cwd=ROOT, capture_output=True, text=True, check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("值等价（TRUE/FALSE/UNKNOWN 逐项相同）: 成立", proc.stdout)


if __name__ == "__main__":
    unittest.main()
