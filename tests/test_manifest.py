"""根清单的读写与损坏拒绝。"""

import json
import unittest

from merkle_dataset.errors import ManifestError
from merkle_dataset.hashing import EMPTY_ROOT
from merkle_dataset.manifest import build_manifest, read_manifest, write_manifest

from .helpers import WorkDir


class ManifestTest(unittest.TestCase):
    def setUp(self):
        self.wd = WorkDir()

    def test_roundtrip(self):
        m = build_manifest(3, b"\xab" * 32)
        path = self.wd.path("m.json")
        write_manifest(m, path)
        loaded = read_manifest(path)
        self.assertEqual(loaded, m)

    def test_empty_dataset_requires_fixed_root(self):
        with self.assertRaises(ManifestError):
            build_manifest(0, b"\x00" * 32)
        m = build_manifest(0, EMPTY_ROOT)
        path = self.wd.path("empty.json")
        write_manifest(m, path)
        self.assertEqual(read_manifest(path).root, EMPTY_ROOT)

    def test_reject_bad_files(self):
        cases = [
            {"type": "wrong", "format_version": 1, "record_count": 1, "root_hex": ("ab" * 32)},
            {"type": "ordered-merkle-manifest", "format_version": 99,
             "record_count": 1, "root_hex": ("ab" * 32)},
            {"type": "ordered-merkle-manifest", "format_version": 1,
             "record_count": -1, "root_hex": ("ab" * 32)},
            {"type": "ordered-merkle-manifest", "format_version": 1,
             "record_count": 1, "root_hex": "abcd"},
            {"type": "ordered-merkle-manifest", "format_version": 1,
             "record_count": 0, "root_hex": ("ab" * 32)},
        ]
        for i, data in enumerate(cases):
            path = self.wd.path(f"bad{i}.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f)
            with self.assertRaises(ManifestError):
                read_manifest(path)

    def test_missing_file(self):
        with self.assertRaises(ManifestError):
            read_manifest(self.wd.path("nope.json"))


if __name__ == "__main__":
    unittest.main()
