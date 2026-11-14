"""Comparability verdicts from capture manifests."""

import unittest
from pathlib import Path

from plancompare.manifest import Capture, compare_manifests, load_capture

FIX = Path(__file__).resolve().parent.parent / "fixtures"


class ManifestTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cap = load_capture(FIX / "capture.json")

    def test_shared_manifest_ok(self):
        r = compare_manifests(self.cap, self.cap)
        self.assertEqual(r["status"], "ok")
        self.assertTrue(r["directly_comparable"])
        # settings differ between before/after captures → cannot blame the plan
        self.assertFalse(r["can_attribute_to_plan"])
        settings = [c for c in r["checks"] if c["check"] == "capture_settings"]
        self.assertEqual(settings[0]["result"], "differ")

    def test_identity_mismatch_blocks_comparison(self):
        other = Capture("other.json", dict(self.cap.raw, sql_sha256="0" * 64))
        r = compare_manifests(self.cap, other)
        self.assertEqual(r["status"], "identity_mismatch")
        self.assertFalse(r["directly_comparable"])
        self.assertFalse(r["can_attribute_to_plan"])

    def test_parameters_mismatch_blocks_comparison(self):
        other = Capture("other.json", dict(self.cap.raw, parameters={"x": 1}))
        r = compare_manifests(self.cap, other)
        self.assertEqual(r["status"], "identity_mismatch")
        self.assertFalse(r["directly_comparable"])

    def test_snapshot_mismatch_shows_but_limits(self):
        other = Capture("other.json", dict(self.cap.raw, data_snapshot="f" * 64))
        r = compare_manifests(self.cap, other)
        self.assertEqual(r["status"], "snapshot_mismatch")
        self.assertTrue(r["directly_comparable"])
        self.assertFalse(r["can_attribute_to_plan"])
        self.assertTrue(any("快照" in c for c in r["caveats"]))

    def test_no_manifest(self):
        r = compare_manifests(None, None)
        self.assertEqual(r["status"], "no_manifest")
        self.assertFalse(r["directly_comparable"])

    def test_identical_manifests_allow_attribution(self):
        raw = dict(self.cap.raw)
        same_settings = "SET jit=off;"
        raw["before"] = {"file": "a.json", "settings_sql": same_settings}
        raw["after"] = {"file": "b.json", "settings_sql": same_settings}
        raw["timing_note"] = None
        cap = Capture("m.json", raw)
        r = compare_manifests(cap, cap)
        self.assertEqual(r["status"], "ok")
        self.assertTrue(r["can_attribute_to_plan"])


if __name__ == "__main__":
    unittest.main()
