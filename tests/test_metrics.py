"""Metric derivation: self time, exclusive buffers, missing fields, loops."""

import unittest

from plancompare.metrics import (
    BUFFER_FIELDS,
    exclusive_buffers,
    self_time_ms,
    side_metrics,
)
from plancompare.model import PlanNode


def node(node_type="Sort", children=(), **fields):
    raw = {"Node Type": node_type}
    raw.update(fields)
    n = PlanNode(pointer="/0/Plan", node_type=node_type, raw=raw)
    for i, c in enumerate(children):
        c.parent = n
        c.pointer = f"/0/Plan/Plans/{i}"
        n.children.append(c)
    return n


class SelfTimeTest(unittest.TestCase):
    def test_simple_subtraction(self):
        child = node("Seq Scan", **{"Actual Total Time": 0.25, "Actual Loops": 2})
        parent = node("Aggregate", [child],
                      **{"Actual Total Time": 1.0, "Actual Loops": 1})
        got, status = self_time_ms(parent)
        self.assertEqual(status, "ok")
        self.assertAlmostEqual(got, 1.0 - 0.25 * 2)

    def test_child_with_more_loops_than_parent(self):
        # e.g. Memoize(133 loops) over Index Scan(40 loops)
        child = node("Index Scan", **{"Actual Total Time": 0.001, "Actual Loops": 40})
        parent = node("Memoize", [child],
                      **{"Actual Total Time": 0.001, "Actual Loops": 133})
        got, status = self_time_ms(parent)
        self.assertEqual(status, "ok")
        self.assertAlmostEqual(got, 0.001 * 133 - 0.001 * 40)

    def test_never_executed_node(self):
        n = node("Index Scan", **{"Actual Loops": 0})
        got, status = self_time_ms(n)
        self.assertEqual(got, 0.0)
        self.assertIn("未执行", status)

    def test_missing_child_time_blocks_derivation(self):
        child = node("Seq Scan", **{"Actual Loops": 1})  # no Actual Total Time
        parent = node("Sort", [child],
                      **{"Actual Total Time": 1.0, "Actual Loops": 1})
        got, status = self_time_ms(parent)
        self.assertIsNone(got)
        self.assertIn("子节点", status)

    def test_missing_own_fields(self):
        n = node("Sort")
        got, status = self_time_ms(n)
        self.assertIsNone(got)
        self.assertIn("缺少", status)

    def test_parallel_workers_block_derivation(self):
        n = node("Gather", **{"Actual Total Time": 1.0, "Actual Loops": 1,
                              "Workers": [{"Worker Number": 0}]})
        got, status = self_time_ms(n)
        self.assertIsNone(got)
        self.assertIn("Worker", status)


class ExclusiveBuffersTest(unittest.TestCase):
    def test_subtraction(self):
        child_fields = {f: 0 for f in BUFFER_FIELDS}
        child_fields.update({"Shared Hit Blocks": 4, "Shared Read Blocks": 1})
        parent_fields = {f: 0 for f in BUFFER_FIELDS}
        parent_fields.update({"Shared Hit Blocks": 10, "Shared Read Blocks": 1})
        child = node("Seq Scan", **child_fields)
        parent = node("Sort", [child], **parent_fields)
        excl, status = exclusive_buffers(parent)
        self.assertEqual(status, "ok")
        self.assertEqual(excl["Shared Hit Blocks"], 6)
        self.assertEqual(excl["Shared Read Blocks"], 0)

    def test_buffers_not_captured(self):
        n = node("Seq Scan", **{"Actual Rows": 1})
        excl, status = exclusive_buffers(n)
        self.assertEqual(excl, {})
        self.assertIn("未采集", status)

    def test_missing_child_field_marks_partial(self):
        child = node("Seq Scan", **{"Shared Hit Blocks": 4})  # other fields absent
        parent = node("Sort", [child], **{f: 10 for f in BUFFER_FIELDS})
        excl, status = exclusive_buffers(parent)
        self.assertNotEqual(status, "ok")
        self.assertEqual(excl["Shared Hit Blocks"], 6)
        self.assertIsNone(excl["Shared Read Blocks"])


class SideMetricsTest(unittest.TestCase):
    def test_xloops_totals(self):
        n = node("Index Scan", **{
            "Plan Rows": 16, "Actual Rows": 16, "Actual Loops": 133,
            "Actual Total Time": 0.012,
        })
        m = side_metrics(n)
        self.assertEqual(m["actual_rows_total_xloops"], 16 * 133)
        self.assertEqual(m["actual_total_time_xloops_ms"], 0.012 * 133)
        self.assertAlmostEqual(m["est_ratio_per_loop"], 1.0)

    def test_never_executed_has_no_est_ratio(self):
        n = node("Index Scan", **{"Plan Rows": 5, "Actual Loops": 0})
        m = side_metrics(n)
        self.assertFalse(m["executed"])
        self.assertIsNone(m["est_ratio_per_loop"])

    def test_zero_plan_rows_gives_no_ratio(self):
        n = node("Aggregate", **{
            "Plan Rows": 0, "Actual Rows": 3, "Actual Loops": 1,
            "Actual Total Time": 0.1,
        })
        self.assertIsNone(side_metrics(n)["est_ratio_per_loop"])

    def test_missing_buffers_do_not_crash(self):
        n = node("Seq Scan", **{"Actual Rows": 1, "Actual Loops": 1,
                                "Actual Total Time": 0.1})
        m = side_metrics(n)
        self.assertTrue(all(v is None for v in m["buffers_inclusive"].values()))
        self.assertEqual(m["buffers_exclusive"], {})


if __name__ == "__main__":
    unittest.main()
