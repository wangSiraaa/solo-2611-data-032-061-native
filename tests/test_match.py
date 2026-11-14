"""Matching invariants: order/noise independence and explicit ambiguity."""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from plancompare.match import match_plans
from plancompare.model import load_plan

FIX = Path(__file__).resolve().parent.parent / "fixtures"


def _write_tmp(raw):
    f = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8")
    json.dump(raw, f)
    f.close()
    return f.name


def _sig(node):
    """Position-independent identity signature of a node."""
    return (
        node.node_type,
        node.relation,
        node.alias,
        tuple(sorted(node.relation_set)),
        tuple(node.get("Sort Key") or ()),
        tuple(node.get("Group Key") or ()),
    )


class OrderAndNoiseTest(unittest.TestCase):
    """Swapping child order or adding irrelevant fields must not change
    relation identity or the resulting correspondence."""

    @classmethod
    def setUpClass(cls):
        cls.orig = load_plan(FIX / "before.json")
        raw = copy.deepcopy(cls.orig.raw)

        def walk(n):
            n["X-Noise-Field"] = {"arbitrary": [1, 2, 3]}
            n["Comment"] = "无关字段"
            plans = n.get("Plans")
            if plans:
                plans.reverse()
                for c in plans:
                    walk(c)

        walk(raw[0]["Plan"])
        cls.modified = load_plan(_write_tmp(raw))

    def test_all_nodes_matched_after_shuffle(self):
        pairs = match_plans(self.orig, self.modified)
        by_status = {}
        for p in pairs:
            by_status[p.status] = by_status.get(p.status, 0) + 1
        self.assertEqual(by_status, {"matched": 9})

    def test_pair_signatures_identical(self):
        pairs = match_plans(self.orig, self.modified)
        for p in pairs:
            self.assertEqual(p.status, "matched")
            self.assertEqual(_sig(p.before), _sig(p.after))

    def test_same_pairs_as_unshuffled_self_match(self):
        base = match_plans(self.orig, load_plan(FIX / "before.json"))
        shuffled = match_plans(self.orig, self.modified)
        base_set = {(_sig(p.before), _sig(p.after)) for p in base}
        shuffled_set = {(_sig(p.before), _sig(p.after)) for p in shuffled}
        self.assertEqual(base_set, shuffled_set)


class AmbiguityTest(unittest.TestCase):
    """Duplicate operators with identical signatures must be reported as
    ambiguous, never force-paired."""

    def _plan(self):
        return [{
            "Plan": {
                "Node Type": "Aggregate",
                "Strategy": "Hashed",
                "Group Key": ["x"],
                "Plan Rows": 1,
                "Actual Rows": 1,
                "Actual Loops": 1,
                "Actual Total Time": 1.0,
                "Plans": [
                    {"Node Type": "Seq Scan", "Relation Name": "t", "Alias": "t",
                     "Plan Rows": 5, "Actual Rows": 5, "Actual Loops": 1,
                     "Actual Total Time": 0.4},
                    {"Node Type": "Seq Scan", "Relation Name": "t", "Alias": "t",
                     "Plan Rows": 5, "Actual Rows": 5, "Actual Loops": 1,
                     "Actual Total Time": 0.4},
                ],
            },
            "Execution Time": 1.0,
            "Planning Time": 0.1,
        }]

    def test_identical_duplicate_scans_are_ambiguous(self):
        a = load_plan(_write_tmp(self._plan()))
        b = load_plan(_write_tmp(self._plan()))
        pairs = match_plans(a, b)
        ambiguous = [p for p in pairs if p.status == "ambiguous"]
        matched = [p for p in pairs if p.status == "matched"]
        # the Aggregate still matches 1:1 …
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0].before.node_type, "Aggregate")
        # … but the two identical scans are an explicit ambiguity
        self.assertEqual(len(ambiguous), 1)
        self.assertEqual(len(ambiguous[0].candidates_before), 2)
        self.assertEqual(len(ambiguous[0].candidates_after), 2)
        self.assertIn("不强行配对", ambiguous[0].evidence[0])

    def test_ambiguity_is_stable_under_child_order_swap(self):
        raw_a = self._plan()
        raw_b = self._plan()
        raw_b[0]["Plan"]["Plans"].reverse()
        a = load_plan(_write_tmp(raw_a))
        b = load_plan(_write_tmp(raw_b))
        pairs = match_plans(a, b)
        self.assertEqual(len([p for p in pairs if p.status == "ambiguous"]), 1)
        self.assertEqual(len([p for p in pairs if p.status == "matched"]), 1)


if __name__ == "__main__":
    unittest.main()
