"""SQLite 外排构建、随机访问与加载完整性核验。"""

import os
import sqlite3
import unittest

from merkle_dataset.errors import InputError, TreeIntegrityError
from merkle_dataset.hashing import (
    EMPTY_ROOT,
    PAD_LEAF,
    internal_digest,
    leaf_digest,
    next_power_of_two,
)
from merkle_dataset.manifest import write_manifest
from merkle_dataset.storage import build_tree, load_tree

from .helpers import WorkDir


def independent_root(records: list[tuple[str, str]]) -> bytes:
    """不依赖 storage 的独立根计算：Python 内存排序 + 逐层折叠。"""
    ordered = sorted((k.encode(), v.encode()) for k, v in records)
    if not ordered:
        return EMPTY_ROOT
    level = [leaf_digest(k, v) for k, v in ordered]
    leaves = next_power_of_two(len(level))
    level.extend([PAD_LEAF] * (leaves - len(level)))
    while len(level) > 1:
        level = [internal_digest(level[i], level[i + 1]) for i in range(0, len(level), 2)]
    return level[0]


class StorageBuildTest(unittest.TestCase):
    def setUp(self):
        self.wd = WorkDir()

    def _build(self, records, name="d"):
        jsonl = self.wd.write_jsonl(f"{name}.jsonl", records)
        db = self.wd.path(f"{name}.db")
        manifest_path = self.wd.path(f"{name}.manifest.json")
        manifest = build_tree(jsonl, db)
        write_manifest(manifest, manifest_path)
        return db, manifest_path, manifest

    def test_empty_dataset_root_and_reload(self):
        jsonl = self.wd.write_empty_jsonl("empty.jsonl")
        db = self.wd.path("empty.db")
        manifest = build_tree(jsonl, db)
        self.assertEqual(manifest.record_count, 0)
        self.assertEqual(manifest.root, EMPTY_ROOT)
        with load_tree(db, manifest) as tree:
            self.assertEqual(tree.record_count, 0)
            self.assertEqual(tree.height, 0)
            self.assertIsNone(tree.lookup(b"anything"))
            self.assertIsNone(tree.predecessor(b"anything"))
            self.assertIsNone(tree.successor(b"anything"))

    def test_single_leaf(self):
        db, _, manifest = self._build([("only", "v")], "one")
        self.assertEqual(manifest.root, leaf_digest(b"only", b"v"))
        with load_tree(db, manifest) as tree:
            self.assertEqual(tree.height, 0)
            self.assertEqual(tree.lookup(b"only"), (0, b"v"))
            self.assertIsNone(tree.lookup(b"nope"))

    def test_non_power_of_two_padding_and_independent_root(self):
        # 5 条记录补齐到 8 叶；根必须与独立实现完全一致。
        records = [
            ("banana", "黄"), ("apple", "红"), ("cherry", "c"),
            ("date", ""), ("eggplant", "e"),
        ]
        db, _, manifest = self._build(records, "five")
        self.assertEqual(manifest.root, independent_root(records))
        with load_tree(db, manifest) as tree:
            self.assertEqual(tree.record_count, 5)
            self.assertEqual(tree.leaf_count, 8)
            self.assertEqual(tree.height, 3)
            # 补齐叶存在于节点表但 ord 范围只到 4。
            pad = tree.node_digest(0, 7)
            self.assertEqual(pad, PAD_LEAF)
            with self.assertRaises(TreeIntegrityError):
                tree.node_digest(0, 8)

    def test_power_of_two_has_no_padding(self):
        records = [(f"k{i:02d}", f"v{i}") for i in range(4)]
        db, _, manifest = self._build(records, "four")
        self.assertEqual(manifest.root, independent_root(records))
        with load_tree(db, manifest) as tree:
            self.assertEqual(tree.leaf_count, 4)
            self.assertEqual(tree.leaf_count - tree.record_count, 0)

    def test_sizes_1_2_3_7_8_9(self):
        for n in (1, 2, 3, 7, 8, 9):
            records = [(f"key-{i:04d}", f"val-{i}") for i in range(n)]
            db, _, manifest = self._build(records, f"n{n}")
            self.assertEqual(manifest.root, independent_root(records), f"n={n}")
            with load_tree(db, manifest) as tree:
                self.assertEqual(tree.record_count, n)
                self.assertEqual(tree.leaf_count, next_power_of_two(n))

    def test_byte_order_utf8_no_normalization(self):
        # 中文键按 UTF-8 字节序排列；构造两个归一化场景：
        # "é"（NFC, 2 字节）与含组合符的 "é"（NFD, 3 字节）不得被归一化合并。
        nfc = "é"
        nfd = "\u0065\u0301"
        self.assertNotEqual(nfc.encode(), nfd.encode())
        records = [("中文", "z"), (nfc, "nfc"), (nfd, "nfd"), ("abc", "a"), ("🍎", "emoji")]
        db, _, manifest = self._build(records, "cjk")
        expected_order = sorted(k.encode() for k, _ in records)
        with load_tree(db, manifest) as tree:
            rows = tree.conn.execute("SELECT key FROM records ORDER BY ord").fetchall()
            self.assertEqual([r[0] for r in rows], expected_order)
            self.assertIsNotNone(tree.lookup(nfc.encode()))
            self.assertIsNotNone(tree.lookup(nfd.encode()))

    def test_random_access_neighbors(self):
        records = [(f"k{i:03d}", f"v{i}") for i in range(10)]
        db, _, manifest = self._build(records, "acc")
        with load_tree(db, manifest) as tree:
            self.assertEqual(tree.lookup(b"k000"), (0, b"v0"))
            self.assertEqual(tree.lookup(b"k009"), (9, b"v9"))
            self.assertIsNone(tree.predecessor(b"k000"))
            self.assertEqual(tree.predecessor(b"k005"), (4, b"k004", b"v4"))
            self.assertEqual(tree.successor(b"k005"), (6, b"k006", b"v6"))
            self.assertIsNone(tree.successor(b"k009"))
            # 不存在的键也能取到严格邻接。
            self.assertEqual(tree.predecessor(b"k0045"), (4, b"k004", b"v4"))
            self.assertEqual(tree.successor(b"k0045"), (5, b"k005", b"v5"))

    def test_duplicate_keys_fail_and_leave_no_artifact(self):
        jsonl = self.wd.write_jsonl("dup.jsonl", [("a", "1"), ("b", "2"), ("a", "3")])
        db = self.wd.path("dup.db")
        with self.assertRaises(InputError):
            build_tree(jsonl, db)
        self.assertFalse(os.path.exists(db))
        self.assertFalse(os.path.exists(db + ".tmp"))

    def test_illegal_inputs_fail(self):
        # 空 key
        path = self.wd.write_jsonl("emptykey.jsonl", [("", "x")])
        with self.assertRaises(InputError):
            build_tree(path, self.wd.path("e1.db"))
        # 超长 value（>64 KiB）
        path = self.wd.write_jsonl("big.jsonl", [("k", "x" * (64 * 1024 + 1))])
        with self.assertRaises(InputError):
            build_tree(path, self.wd.path("e2.db"))
        # 64 KiB 整恰好合法
        path = self.wd.write_jsonl("ok64.jsonl", [("k", "x" * 64 * 1024)])
        manifest = build_tree(path, self.wd.path("e3.db"))
        self.assertEqual(manifest.record_count, 1)
        # 多余字段
        with open(self.wd.path("extra.jsonl"), "w", encoding="utf-8") as f:
            f.write('{"key":"a","value":"b","extra":1}\n')
        with self.assertRaises(InputError):
            build_tree(self.wd.path("extra.jsonl"), self.wd.path("e4.db"))
        # 空行
        with open(self.wd.path("blank.jsonl"), "w", encoding="utf-8") as f:
            f.write('{"key":"a","value":"b"}\n\n')
        with self.assertRaises(InputError):
            build_tree(self.wd.path("blank.jsonl"), self.wd.path("e5.db"))

    def test_load_rejects_missing_file_and_bad_manifest(self):
        db, _, manifest = self._build([("a", "1"), ("b", "2")], "rel")
        with self.assertRaises(TreeIntegrityError):
            load_tree(self.wd.path("does-not-exist.db"))
        bad = type(manifest)(manifest.version, manifest.record_count + 1, manifest.root)
        with self.assertRaises(TreeIntegrityError):
            load_tree(db, bad)
        bad_root = type(manifest)(manifest.version, manifest.record_count, b"\x00" * 32)
        with self.assertRaises(TreeIntegrityError):
            load_tree(db, bad_root)


class StorageTamperTest(unittest.TestCase):
    """加载时不能仅凭文件存在接受：直接篡改 SQLite 内容必须被重算核验发现。"""

    def setUp(self):
        self.wd = WorkDir()
        self.jsonl = self.wd.write_jsonl(
            "t.jsonl", [(f"k{i:02d}", f"v{i}") for i in range(6)]
        )
        self.db = self.wd.path("t.db")
        self.manifest = build_tree(self.jsonl, self.db)

    def _tamper(self, sql: str):
        conn = sqlite3.connect(self.db)
        conn.execute(sql)
        conn.commit()
        conn.close()

    def test_tampered_value_detected(self):
        self._tamper("UPDATE records SET value = 'forged' WHERE ord = 2")
        with self.assertRaises(TreeIntegrityError):
            load_tree(self.db, self.manifest)

    def test_tampered_order_detected(self):
        # ord 有 PRIMARY KEY 约束无法直接重复，改为破坏键与下标的对应关系：
        # 交换两行的键，使 ord 顺序不再等于键字节序。
        self._tamper("UPDATE records SET key = x'ff' WHERE ord = 0")
        with self.assertRaises(TreeIntegrityError):
            load_tree(self.db, self.manifest)

    def test_tampered_node_digest_detected(self):
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE nodes SET digest = ? WHERE level = 1 AND idx = 0", (b"\x11" * 32,))
        conn.commit()
        conn.close()
        with self.assertRaises(TreeIntegrityError):
            load_tree(self.db, self.manifest)

    def test_tampered_meta_count_detected(self):
        self._tamper("UPDATE meta SET value = '7' WHERE key = 'record_count'")
        with self.assertRaises(TreeIntegrityError):
            load_tree(self.db)

    def test_manifest_tamper_rejected_at_load(self):
        forged = type(self.manifest)(
            self.manifest.version, self.manifest.record_count, b"\x09" * 32
        )
        with self.assertRaises(TreeIntegrityError):
            load_tree(self.db, forged)


if __name__ == "__main__":
    unittest.main()
