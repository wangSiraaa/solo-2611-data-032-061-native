"""CLI smoke tests and missing-optional-field robustness end to end."""

import json
import tempfile
import unittest
from pathlib import Path

from plancompare.cli import main

FIX = Path(__file__).resolve().parent.parent / "fixtures"


class CliTest(unittest.TestCase):
    def test_compare_command_writes_both_outputs(self):
        with tempfile.TemporaryDirectory() as d:
            json_out = Path(d) / "r.json"
            md_out = Path(d) / "r.md"
            rc = main([
                "compare", str(FIX / "before.json"), str(FIX / "after.json"),
                "--capture", str(FIX / "capture.json"),
                "--json-out", str(json_out), "--md-out", str(md_out),
            ])
            self.assertEqual(rc, 0)
            data = json.loads(json_out.read_text(encoding="utf-8"))
            self.assertEqual(data["comparability"]["status"], "ok")
            self.assertEqual(len(data["correspondence"]), 11)
            md = md_out.read_text(encoding="utf-8")
            self.assertIn("可比性", md)
            self.assertIn("Memoize", md)
            self.assertIn("×Loops", md)

    def test_compare_without_manifest(self):
        with tempfile.TemporaryDirectory() as d:
            rc = main([
                "compare", str(FIX / "before.json"), str(FIX / "after.json"),
                "--json-out", str(Path(d) / "r.json"),
                "--md-out", str(Path(d) / "r.md"),
            ])
            self.assertEqual(rc, 0)
            data = json.loads((Path(d) / "r.json").read_text(encoding="utf-8"))
            self.assertEqual(data["comparability"]["status"], "no_manifest")

    def test_conflicting_capture_options_rejected(self):
        rc = main([
            "compare", "a.json", "b.json",
            "--capture", "c.json", "--before-capture", "d.json",
        ])
        self.assertEqual(rc, 1)


class MissingOptionalFieldsTest(unittest.TestCase):
    """Plans captured without BUFFERS, or with never-executed nodes that
    omit Actual fields, must still analyze without crashing."""

    def _strip_and_run(self, mutate):
        from plancompare.analyze import analyze

        before = json.loads((FIX / "before.json").read_text(encoding="utf-8"))
        after = json.loads((FIX / "after.json").read_text(encoding="utf-8"))
        mutate(after)
        with tempfile.TemporaryDirectory() as d:
            bp, ap = Path(d) / "b.json", Path(d) / "a.json"
            bp.write_text(json.dumps(before), encoding="utf-8")
            ap.write_text(json.dumps(after), encoding="utf-8")
            return analyze(str(bp), str(ap))

    def test_buffers_missing_everywhere(self):
        from plancompare.metrics import BUFFER_FIELDS

        def strip(doc):
            def walk(n):
                for f in BUFFER_FIELDS:
                    n.pop(f, None)
                for c in n.get("Plans", []):
                    walk(c)
            walk(doc[0]["Plan"])

        result = self._strip_and_run(strip)
        for p in result["correspondence"]:
            if p["status"] != "matched":
                continue
            m = p["metrics"]
            self.assertTrue(
                all(v["after"] is None for v in m["buffers_inclusive"].values()))
            self.assertNotEqual(m["buffers_exclusive_status"]["after"], "ok")
        # self time is still derivable without buffer fields
        joined = "\n".join(result["limitations"])
        self.assertIn("缓冲", joined)

    def test_never_executed_node(self):
        def mutate(doc):
            # make the inner index scan never execute: loops 0, actuals omitted
            idx = doc[0]["Plan"]["Plans"][0]["Plans"][0]["Plans"][0]["Plans"][1]["Plans"][0]
            for k in ("Actual Startup Time", "Actual Total Time", "Actual Rows"):
                idx.pop(k, None)
            idx["Actual Loops"] = 0

        result = self._strip_and_run(mutate)
        pair = [p for p in result["correspondence"]
                if p["status"] == "matched"
                and p["after"]["node_type"] == "Index Scan"
                and p["after"]["label"].endswith("customers p")]
        self.assertEqual(len(pair), 1)
        m = pair[0]["metrics"]
        self.assertIsNone(m["actual_rows_total_xloops"]["after"])
        self.assertEqual(m["self_time_xloops_ms"]["after"], 0.0)
        self.assertIn("未执行", m["self_time_xloops_ms"]["after_status"])


if __name__ == "__main__":
    unittest.main()
