"""End-to-end tests on the shipped fixtures.

Expected values in this file were computed by hand from fixtures/*.json
(manual arithmetic on the raw plan values), not copied from tool output.
"""

import json
import unittest
from pathlib import Path

from plancompare.analyze import analyze
from plancompare.manifest import load_capture

FIX = Path(__file__).resolve().parent.parent / "fixtures"
MISSING = object()


def resolve_pointer(doc, pointer):
    cur = doc
    for tok in pointer.strip("/").split("/"):
        tok = tok.replace("~1", "/").replace("~0", "~")
        if isinstance(cur, list):
            cur = cur[int(tok)]
        elif isinstance(cur, dict):
            if tok not in cur:
                return MISSING
            cur = cur[tok]
        else:
            return MISSING
    return cur


class EndToEndTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cap = load_capture(FIX / "capture.json")
        cls.result = analyze(str(FIX / "before.json"), str(FIX / "after.json"), cap, cap)
        cls.before_raw = json.loads((FIX / "before.json").read_text(encoding="utf-8"))
        cls.after_raw = json.loads((FIX / "after.json").read_text(encoding="utf-8"))

    def pairs(self, status):
        return [p for p in self.result["correspondence"] if p["status"] == status]

    def pair_by_before_label(self, label):
        for p in self.pairs("matched"):
            if p["before"]["label"] == label:
                return p
        self.fail(f"no matched pair with before label {label}")

    def test_correspondence_counts(self):
        self.assertEqual(len(self.pairs("matched")), 6)
        self.assertEqual(len(self.pairs("removed")), 3)
        self.assertEqual(len(self.pairs("added")), 2)
        self.assertEqual(len(self.pairs("ambiguous")), 0)

    def test_duplicate_operators_distinguished_by_relation_set(self):
        # before has two Hash nodes and two Hash Joins; identity comes from
        # participating tables, not from node type or position.
        removed_labels = sorted(p["before"]["label"] for p in self.pairs("removed"))
        self.assertEqual(removed_labels, ["Hash Join {c,o}", "Hash {c}", "Hash {p}"])

    def test_different_subtrees_not_cross_matched(self):
        # The inner joins have different relation sets ({c,o} vs {c,p}):
        # they must surface as removed/added, never as a matched pair.
        for p in self.pairs("matched"):
            self.assertNotEqual(p["before"]["label"], "Hash Join {c,o}")
            self.assertNotEqual(p["after"]["label"], "Nested Loop {c,p}")
        added_labels = sorted(p["after"]["label"] for p in self.pairs("added"))
        self.assertEqual(added_labels, ["Memoize (unsupported)", "Nested Loop {c,p}"])

    def test_sort_position_change_detected(self):
        p = self.pair_by_before_label("Sort key(c.region, p.region)")
        self.assertEqual(p["before"]["pointer"], "/0/Plan")
        self.assertEqual(p["after"]["pointer"], "/0/Plan/Plans/0")
        self.assertTrue(any("位置变化" in c for c in p["structural_changes"]))

    def test_aggregate_strategy_change_kept_as_change_not_mismatch(self):
        p = self.pair_by_before_label("Aggregate[Hashed] group(c.region, p.region)")
        self.assertEqual(p["after"]["node_type"], "Aggregate")
        self.assertTrue(any("Hashed" in c and "Sorted" in c
                            for c in p["structural_changes"]))

    def test_join_type_change(self):
        p = self.pair_by_before_label("Hash Join {c,o,p}")
        self.assertEqual(p["after"]["node_type"], "Nested Loop")
        self.assertTrue(any("节点类型变化" in c for c in p["structural_changes"]))

    def test_loops_greater_than_one(self):
        p = self.pair_by_before_label("Seq Scan orders o")
        m = p["metrics"]
        self.assertEqual(m["actual_loops"]["before"], 1)
        self.assertEqual(m["actual_loops"]["after"], 133)
        # per-loop rows stay 16, totals are the xloops figures
        self.assertEqual(m["actual_rows_per_loop"]["after"], 16)
        self.assertEqual(m["actual_rows_total_xloops"]["after"], 16 * 133)
        self.assertEqual(m["actual_total_time_xloops_ms"]["after"], 0.012 * 133)

    def test_unknown_boundary_memoize(self):
        memo = [p for p in self.pairs("added")
                if p["after"]["node_type"] == "Memoize"]
        self.assertEqual(len(memo), 1)
        self.assertFalse(memo[0]["after"]["supported"])
        self.assertIn("Memoize", "\n".join(self.result["limitations"]))

    def test_hand_verified_self_time(self):
        # Manual arithmetic from the raw fixture values (inclusive minus children).
        expected = {
            # before: 3.4 - 3.297
            ("Sort key(c.region, p.region)", "before"): 0.103,
            # before Hash Join {c,o,p}: 2.566 - 1.829 - 0.141
            ("Hash Join {c,o,p}", "before"): 0.596,
            # after Nested Loop {c,o,p}: 2.204 - 0.243 - 0.012*133
            ("Hash Join {c,o,p}", "after"): 0.365,
            # after Sort: 3.369 - 2.204
            ("Sort key(c.region, p.region)", "after"): 1.165,
            # after Index Scan orders o (leaf): 0.012*133
            ("Seq Scan orders o", "after"): 1.596,
        }
        for (label, side), want in expected.items():
            p = self.pair_by_before_label(label)
            got = p["metrics"]["self_time_xloops_ms"][side]
            self.assertAlmostEqual(got, want, places=6,
                                   msg=f"{label} {side} self time")
            self.assertEqual(p["metrics"]["self_time_xloops_ms"][f"{side}_status"], "ok")

    def test_hand_verified_exclusive_buffers(self):
        # before root Sort: 29 - 26 (Aggregate) = 3 shared hits of its own.
        p = self.pair_by_before_label("Sort key(c.region, p.region)")
        self.assertEqual(
            p["metrics"]["buffers_exclusive"]["Shared Hit Blocks"]["before"], 3)
        # after Nested Loop {c,o,p}: 3003 - 82 - 2921 = 0.
        q = self.pair_by_before_label("Hash Join {c,o,p}")
        self.assertEqual(
            q["metrics"]["buffers_exclusive"]["Shared Hit Blocks"]["after"], 0)
        # after Index Scan orders o is a leaf: exclusive == inclusive == 2921.
        r = self.pair_by_before_label("Seq Scan orders o")
        self.assertEqual(
            r["metrics"]["buffers_exclusive"]["Shared Hit Blocks"]["after"], 2921)

    def test_est_ratio_per_loop(self):
        p = self.pair_by_before_label("Seq Scan orders o")
        self.assertAlmostEqual(p["metrics"]["est_ratio_per_loop"]["before"], 1.0)
        self.assertAlmostEqual(p["metrics"]["est_ratio_per_loop"]["after"], 1.0)

    def test_pointers_resolve_to_source_values(self):
        # Every directly-read scalar must point back at the raw value it came
        # from; entries flagged derived=True are computed from those fields.
        def check(node):
            if isinstance(node, dict):
                if "before_pointer" in node and "delta" in node \
                        and not node.get("derived"):
                    for side, doc in (("before", self.before_raw),
                                      ("after", self.after_raw)):
                        ptr = node[f"{side}_pointer"]
                        val = resolve_pointer(doc, ptr)
                        if node[side] is None:
                            self.assertIn(val, (None, MISSING), ptr)
                        else:
                            self.assertAlmostEqual(float(val), float(node[side]),
                                                   places=6, msg=ptr)
                for v in node.values():
                    check(v)
            elif isinstance(node, list):
                for v in node:
                    check(v)

        check(self.result["plan_level"])
        for p in self.pairs("matched"):
            check(p["metrics"])

    def test_comparability_ok_but_not_attributable(self):
        comp = self.result["comparability"]
        self.assertEqual(comp["status"], "ok")
        self.assertTrue(comp["directly_comparable"])
        # settings differ between the two captures, so plan-blame is blocked
        self.assertFalse(comp["can_attribute_to_plan"])

    def test_observations_and_explanations_separated(self):
        obs_text = "\n".join(o["text"] for o in self.result["observations"])
        self.assertIn("Actual Loops 1 → 133", obs_text)
        expl = "\n".join(self.result["possible_explanations"])
        self.assertIn("设置", expl)
        self.assertIn("不判断因果", expl)


if __name__ == "__main__":
    unittest.main()
