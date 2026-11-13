"""prove/verify 端到端测试。

验证方的“正确答案”来自与实现完全独立的 Python 排序列表 + bisect，
而不是复用 storage 的查询逻辑，确保邻接/下标结论被独立交叉核对。
"""

import bisect
import copy
import unittest

from merkle_dataset.errors import InputError, ProofError
from merkle_dataset.manifest import write_manifest
from merkle_dataset.proving import (
    generate_proof,
    naive_path_digest_count,
    proof_byte_size,
    shared_node_count,
)
from merkle_dataset.storage import build_tree, load_tree
from merkle_dataset.verifier import verify_proof

from .helpers import WorkDir


class ProofFixture:
    def __init__(self, wd: WorkDir, records: list[tuple[str, str]], name: str):
        self.wd = wd
        self.records = records
        jsonl = wd.write_jsonl(f"{name}.jsonl", records)
        self.db = wd.path(f"{name}.db")
        self.manifest_path = wd.path(f"{name}.manifest.json")
        self.manifest = build_tree(jsonl, self.db)
        write_manifest(self.manifest, self.manifest_path)
        # 独立参照：纯 Python 排序键列表（字节序）。
        self.keys_sorted = sorted(k.encode() for k, _ in records)
        self.value_by_key = {k.encode(): v.encode() for k, v in records}

    def prove(self, queries: list[str]) -> dict:
        with load_tree(self.db, self.manifest) as tree:
            return generate_proof(tree, self.manifest, queries)

    def check_against_reference(self, proof: dict, queries: list[str]):
        """用 bisect 独立核对每个答案的存在性、邻接、下标。"""
        conclusions = verify_proof(self.manifest, queries, proof)
        assert len(conclusions) == len(queries)
        keys = self.keys_sorted
        for answer, conclusion, q in zip(proof["answers"], conclusions, queries):
            qb = q.encode()
            pos = bisect.bisect_left(keys, qb)
            if pos < len(keys) and keys[pos] == qb:
                assert answer["present"]
                assert answer["index"] == pos
                assert answer["key"].encode() == qb
                assert answer["value"].encode() == self.value_by_key[qb]
                assert conclusion.present
                assert conclusion.index == pos
            else:
                assert not answer["present"]
                pred_idx = pos - 1 if pos > 0 else None
                succ_idx = pos if pos < len(keys) else None
                if pred_idx is None:
                    assert answer["predecessor"] is None
                else:
                    assert answer["predecessor"]["index"] == pred_idx
                    assert answer["predecessor"]["key"].encode() == keys[pred_idx]
                if succ_idx is None:
                    assert answer["successor"] is None
                else:
                    assert answer["successor"]["index"] == succ_idx
                    assert answer["successor"]["key"].encode() == keys[succ_idx]
                # 邻接严格性：中间没有任何真实键。
                if pred_idx is not None and succ_idx is not None:
                    assert succ_idx == pred_idx + 1
                assert not conclusion.present
        return conclusions


class ProofCorrectnessTest(unittest.TestCase):
    def setUp(self):
        self.wd = WorkDir()

    def test_empty_dataset_all_missing(self):
        fx = ProofFixture(self.wd, [], "empty")
        queries = ["a", "中", "zzz"]
        proof = fx.prove(queries)
        fx.check_against_reference(proof, queries)
        self.assertEqual(proof["nodes"], [])
        self.assertEqual(proof_byte_size(proof), proof_byte_size(proof))

    def test_single_leaf_present_and_both_boundary_missing(self):
        fx = ProofFixture(self.wd, [("only", "v")], "one")
        for queries in (["only"], ["before"], ["after"], ["only", "before", "after"]):
            proof = fx.prove(queries)
            fx.check_against_reference(proof, queries)
        # 单叶树高 0：任何证明都不需要节点。
        proof = fx.prove(["only", "before", "after"])
        self.assertEqual(shared_node_count(proof), 0)

    def test_non_power_of_two_missing_between_adjacent(self):
        records = [(f"k{i:02d}", f"v{i}") for i in range(5)]  # 补齐到 8
        fx = ProofFixture(self.wd, records, "five")
        queries = ["k00", "k04", "k015", "k025", "k035", "kk", "aa"]
        proof = fx.prove(queries)
        fx.check_against_reference(proof, queries)

    def test_before_min_and_after_max_boundaries(self):
        records = [("m", "1"), ("n", "2"), ("o", "3")]
        fx = ProofFixture(self.wd, records, "bnd")
        proof = fx.prove(["a", "z"])
        fx.check_against_reference(proof, ["a", "z"])
        missing = {a["query"]: a for a in proof["answers"]}
        self.assertIsNone(missing["a"]["predecessor"])
        self.assertEqual(missing["a"]["successor"]["index"], 0)
        self.assertIsNone(missing["z"]["successor"])
        self.assertEqual(missing["z"]["predecessor"]["index"], 2)

    def test_missing_between_every_adjacent_pair(self):
        records = [(f"key-{i:03d}", str(i)) for i in range(8)]
        fx = ProofFixture(self.wd, records, "gap")
        queries = [f"key-{i:03d}-mid" for i in range(7)]
        proof = fx.prove(queries)
        fx.check_against_reference(proof, queries)
        # 每个缺失答案的前驱/后继都必须是相邻下标。
        for answer in proof["answers"]:
            self.assertEqual(
                answer["successor"]["index"], answer["predecessor"]["index"] + 1
            )

    def test_chinese_keys_byte_order(self):
        nfc = "é"          # U+00E9，UTF-8 两字节
        nfd = "\u0065\u0301"  # e + U+0301，UTF-8 三字节，不做归一化
        records = [
            ("中文", "1"), ("苹果", "2"), ("apple", "3"),
            ("香蕉", "4"), (nfc, "5"), (nfd, "6"),
        ]
        fx = ProofFixture(self.wd, records, "cjk")
        queries = ["中", "苹果", "苹", nfc + "x", nfc, "🍰", "apple"]
        proof = fx.prove(queries)
        fx.check_against_reference(proof, queries)

    def test_random_queries_match_reference(self):
        import random

        rng = random.Random(42)
        records = [(f"id-{rng.randrange(100000):06d}", f"v{i}") for i in range(50)]
        # 去重后重建（夹具允许重复会在 build 阶段失败，这里先保证唯一）。
        seen = {}
        for k, v in records:
            seen[k] = v
        records = list(seen.items())
        fx = ProofFixture(self.wd, records, "rnd")
        keys = [k for k, _ in records]
        queries = list(rng.sample(keys, 15))
        queries += [f"id-{rng.randrange(100000):06d}" for _ in range(15)]
        queries = list(dict.fromkeys(queries))
        proof = fx.prove(queries)
        fx.check_against_reference(proof, queries)

    def test_duplicate_query_rejected(self):
        fx = ProofFixture(self.wd, [("a", "1"), ("b", "2")], "dupq")
        with self.assertRaises(InputError):
            fx.prove(["a", "a"])
        with self.assertRaises(InputError):
            fx.prove([])


class NodeSharingTest(unittest.TestCase):
    def setUp(self):
        self.wd = WorkDir()

    def test_shared_nodes_stored_once(self):
        # 16 叶满树；查询同一父节点下的两片叶，level>=1 路径完全相同。
        records = [(f"k{i:02d}", "v") for i in range(16)]
        fx = ProofFixture(self.wd, records, "share16")
        proof = fx.prove(["k00", "k01"])
        coords = [(n["level"], n["index"]) for n in proof["nodes"]]
        self.assertEqual(len(coords), len(set(coords)))  # 无重复节点
        # 逐条路径：每条 height=4 个摘要，共 8；共享后只需 5 个坐标
        # （level0 两个兄弟 + level1..3 各一个）。
        self.assertEqual(naive_path_digest_count(proof), 8)
        self.assertEqual(shared_node_count(proof), 5)

    def test_large_batch_smaller_than_naive_paths(self):
        records = [(f"k{i:04d}", "v") for i in range(64)]
        fx = ProofFixture(self.wd, records, "share64")
        queries = [k for k, _ in records[:32]]  # 整棵左半子树
        proof = fx.prove(queries)
        verify_proof(fx.manifest, queries, proof)
        self.assertLess(shared_node_count(proof), naive_path_digest_count(proof))
        # 32 条路径 × 6 层 = 192 个摘要位；共享后只需 63 个
        # （level 0 仍有 32 个兄弟，level 1..5 共 31 个上层坐标）。
        self.assertEqual(naive_path_digest_count(proof), 32 * 6)
        self.assertEqual(shared_node_count(proof), 63)
        self.assertLess(shared_node_count(proof), naive_path_digest_count(proof))

    def test_single_query_has_no_redundant_nodes(self):
        records = [(f"k{i:02d}", "v") for i in range(8)]
        fx = ProofFixture(self.wd, records, "single")
        proof = fx.prove(["k03"])
        # 单查询 height=3，恰好 3 个节点。
        self.assertEqual(shared_node_count(proof), 3)
        verify_proof(fx.manifest, ["k03"], proof)


class VerifyRejectionTest(unittest.TestCase):
    """主动篡改：值、下标、记录数、前后驱次序、内部摘要、冗余节点。"""

    def setUp(self):
        self.wd = WorkDir()
        records = [(f"k{i:02d}", f"value-{i}") for i in range(6)]
        self.fx = ProofFixture(self.wd, records, "tamper")
        self.queries = ["k01", "k04", "k025", "aaa", "zzz"]
        self.proof = self.fx.prove(self.queries)

    def _assert_rejected(self, proof, queries=None, manifest=None):
        with self.assertRaises(ProofError):
            verify_proof(
                manifest or self.fx.manifest,
                queries if queries is not None else self.queries,
                proof,
            )

    def test_valid_proof_passes_first(self):
        conclusions = verify_proof(self.fx.manifest, self.queries, self.proof)
        self.assertEqual(len(conclusions), len(self.queries))

    def test_tamper_value_rejected(self):
        proof = copy.deepcopy(self.proof)
        proof["answers"][0]["value"] = "forged-value"
        self._assert_rejected(proof)

    def test_tamper_leaf_index_rejected(self):
        proof = copy.deepcopy(self.proof)
        for answer in proof["answers"]:
            if answer["present"]:
                answer["index"] = (answer["index"] + 1) % self.fx.manifest.record_count
                break
        self._assert_rejected(proof)

    def test_index_into_padding_range_rejected(self):
        proof = copy.deepcopy(self.proof)
        proof["answers"][0]["index"] = 7  # 6 条记录补齐到 8，下标 7 是补齐叶
        self._assert_rejected(proof)

    def test_tamper_record_count_in_proof_rejected(self):
        proof = copy.deepcopy(self.proof)
        proof["record_count"] += 1
        self._assert_rejected(proof)

    def test_tamper_manifest_record_count_rejected(self):
        # 清单被改：高度/范围检查或边界邻接检查必须拦住。
        from merkle_dataset.manifest import Manifest

        forged = Manifest(
            self.fx.manifest.version,
            self.fx.manifest.record_count + 2,
            self.fx.manifest.root,
        )
        proof = copy.deepcopy(self.proof)
        proof["height"] = 3  # 与被篡改后的记录数推导高度保持一致也没用
        self._assert_rejected(proof, manifest=forged)

    def test_tamper_predecessor_successor_order_rejected(self):
        proof = copy.deepcopy(self.proof)
        for answer in proof["answers"]:
            if not answer["present"] and answer["predecessor"] and answer["successor"]:
                answer["predecessor"], answer["successor"] = (
                    answer["successor"],
                    answer["predecessor"],
                )
                break
        self._assert_rejected(proof)

    def test_tamper_neighbor_index_gap_rejected(self):
        proof = copy.deepcopy(self.proof)
        for answer in proof["answers"]:
            if not answer["present"] and answer["predecessor"] and answer["successor"]:
                answer["predecessor"]["index"] -= 1  # 制造“中间有遗漏叶”
                break
        self._assert_rejected(proof)

    def test_tamper_internal_digest_rejected(self):
        proof = copy.deepcopy(self.proof)
        target = max(proof["nodes"], key=lambda n: n["level"])
        target["digest_hex"] = "00" * 32
        self._assert_rejected(proof)

    def test_extra_unused_node_rejected(self):
        proof = copy.deepcopy(self.proof)
        proof["nodes"].append(
            {"level": 1, "index": 99, "digest_hex": "11" * 32}
        )
        self._assert_rejected(proof)

    def test_extra_node_at_valid_coordinate_rejected(self):
        # 即使坐标合法，只要不在所需集合里（本批查询用不到）也必须拒绝。
        records = [(f"k{i:02d}", "v") for i in range(16)]
        fx = ProofFixture(self.wd, records, "extra-valid")
        proof = fx.prove(["k00"])
        verify_proof(fx.manifest, ["k00"], proof)
        proof["nodes"].append({"level": 2, "index": 3, "digest_hex": "22" * 32})
        with self.assertRaises(ProofError):
            verify_proof(fx.manifest, ["k00"], proof)

    def test_duplicate_node_rejected(self):
        proof = copy.deepcopy(self.proof)
        proof["nodes"].append(dict(proof["nodes"][0]))
        self._assert_rejected(proof)

    def test_root_node_in_proof_rejected(self):
        proof = copy.deepcopy(self.proof)
        proof["nodes"].append(
            {"level": proof["height"], "index": 0, "digest_hex": "33" * 32}
        )
        self._assert_rejected(proof)

    def test_missing_node_rejected(self):
        proof = copy.deepcopy(self.proof)
        proof["nodes"].pop()
        self._assert_rejected(proof)

    def test_query_set_mismatch_rejected(self):
        self._assert_rejected(self.proof, queries=self.queries + ["k00"])
        self._assert_rejected(self.proof, queries=self.queries[:-1])
        other = list(self.queries)
        other[0] = "k00" if other[0] != "k00" else "k01"
        self._assert_rejected(self.proof, queries=other)

    def test_duplicate_queries_rejected_by_verifier(self):
        dup = list(self.queries)
        dup[1] = dup[0]
        self._assert_rejected(self.proof, queries=dup)

    def test_answer_duplication_rejected(self):
        proof = copy.deepcopy(self.proof)
        proof["answers"].append(dict(proof["answers"][0]))
        self._assert_rejected(proof)

    def test_wrong_root_manifest_rejected(self):
        from merkle_dataset.manifest import Manifest

        forged = Manifest(
            self.fx.manifest.version,
            self.fx.manifest.record_count,
            b"\x44" * 32,
        )
        self._assert_rejected(self.proof, manifest=forged)

    def test_empty_proof_nodes_on_nonempty_tree_rejected(self):
        proof = copy.deepcopy(self.proof)
        proof["nodes"] = []
        self._assert_rejected(proof)

    def test_nonempty_nodes_on_empty_tree_rejected(self):
        fx = ProofFixture(self.wd, [], "empty-reject")
        proof = fx.prove(["q"])
        proof["nodes"] = [{"level": 0, "index": 0, "digest_hex": "55" * 32}]
        with self.assertRaises(ProofError):
            verify_proof(fx.manifest, ["q"], proof)

    def test_boundary_missing_wrong_side_rejected(self):
        records = [("a", "1"), ("b", "2"), ("c", "3")]
        fx = ProofFixture(self.wd, records, "side")
        proof = fx.prove(["z"])
        verify_proof(fx.manifest, ["z"], proof)
        # 把末位叶下标改掉，边界规则（前驱必须是 count-1）应触发。
        proof["answers"][0]["predecessor"]["index"] = 0
        with self.assertRaises(ProofError):
            verify_proof(fx.manifest, ["z"], proof)


if __name__ == "__main__":
    unittest.main()
