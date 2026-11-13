"""txaudit 测试套件（标准库 unittest，无第三方依赖）。

覆盖题目点名的全部情形：
- 正常串行、只读事务、读后写循环、事务读自身写入；
- 无读取盲写导致冲突图有环但仍存在视图等价排列；
- 两个不同 write_id 写相同值的正反两个反例（身份不能按字符串混同）；
- <=6 事务用独立递归枚举逐个排列核对来源身份与最终写入者；
- 8 事务上限的可复现运行时间；重复输入报告字节级稳定。
"""

from __future__ import annotations

import json
import time
import unittest
from itertools import permutations
from pathlib import Path

from txaudit.analysis import analyze, check_permutation
from txaudit.cli import run_audit
from txaudit.generate import FIXED_SUITES, generate_random_history
from txaudit.model import INITIAL, parse_and_validate
from txaudit.report import canonical_dumps, fingerprint
from txaudit.verify import (
    enumerate_legal_recursive,
    independent_conflict_edges,
    independent_replay,
    verify_analysis,
)


def _audit_suite(name: str) -> dict:
    data = FIXED_SUITES[name]
    return run_audit(data, canonical_dumps(data))


def _history_of(name: str):
    history, errors = parse_and_validate(FIXED_SUITES[name])
    assert not errors, [e.message for e in errors]
    return history


class FixedSuiteValidationTests(unittest.TestCase):
    """全部固定样例（除反例外）必须通过输入语义校验。"""

    def test_all_valid_suites_parse(self) -> None:
        for name in FIXED_SUITES:
            with self.subTest(name=name):
                history, errors = parse_and_validate(FIXED_SUITES[name])
                if name == "same_value_bad_source":
                    self.assertIsNone(history)
                    codes = {e.code for e in errors}
                    self.assertIn("BAD_READ_SOURCE", codes)
                else:
                    self.assertEqual(errors, [], msg=[e.message for e in errors])
                    self.assertIsNotNone(history)


class SerialTests(unittest.TestCase):
    def test_serial_history(self) -> None:
        report = _audit_suite("serial")
        self.assertEqual(report["status"], "ok")
        view = report["view_serializability"]
        self.assertEqual(view["legal_count"], 1)
        self.assertEqual(view["smallest_legal"], [1, 2])
        self.assertTrue(report["verdict"]["view_serializable"])
        self.assertTrue(report["verdict"]["conflict_serializable"])
        self.assertFalse(report["conflict_graph"]["has_cycle"])
        # 最终写入者：x 被 T2 的 w3 覆盖、y=w2(T1)
        fw = {e["key"]: e["final_write_id"] for e in report["final_writers"]}
        self.assertEqual(fw, {"x": "w3", "y": "w2"})


class ReadonlyTests(unittest.TestCase):
    def test_readonly_transaction(self) -> None:
        report = _audit_suite("readonly")
        view = report["view_serializability"]
        # T3 只读、要求 T1 先于自己；T1/T2 写不同键互不约束：
        # 唯一约束是 T1 在 T3 前，3! 个排列中一半合法 -> 3 个
        self.assertEqual(view["legal_count"], 3)
        self.assertEqual(view["smallest_legal"], [1, 2, 3])
        self.assertEqual(
            view["all_legal_permutations"],
            [[1, 2, 3], [1, 3, 2], [2, 1, 3]])
        self.assertTrue(report["verdict"]["conflict_serializable"])
        # 写事务之间无同键操作 -> 不存在 T1<->T2 边；T3 的 read 产生读写边
        edge_pairs = {(e["from_tid"], e["to_tid"]) for e in report["conflict_graph"]["edges"]}
        self.assertNotIn((1, 2), edge_pairs)
        self.assertNotIn((2, 1), edge_pairs)
        self.assertIn((1, 3), edge_pairs)  # w1 先于 T3 对 x 的 read
        # 独立枚举集合必须与主枚举完全一致（含身份，不只比字符串）
        history = _history_of("readonly")
        self.assertEqual(
            sorted(enumerate_legal_recursive(history)),
            view["all_legal_permutations"],
        )


class RWCycleBadTests(unittest.TestCase):
    def test_read_write_cycle_is_not_serializable(self) -> None:
        report = _audit_suite("rw_cycle_bad")
        view = report["view_serializability"]
        # 两个排列都必须被枚举且都失败，合法数明确为零而非“一次失败就放弃”
        self.assertEqual(view["total_permutations_enumerated"], 2)
        self.assertEqual(view["legal_count"], 0)
        self.assertIsNone(view["smallest_legal"])
        self.assertFalse(report["verdict"]["view_serializable"])
        self.assertFalse(report["verdict"]["conflict_serializable"])
        graph = report["conflict_graph"]
        self.assertTrue(graph["has_cycle"])
        # 环上每条边都必须真实存在
        edge_set = {(e["from_tid"], e["to_tid"]) for e in graph["edges"]}
        for ce in graph["cycle_edges"]:
            self.assertIn((ce["from_tid"], ce["to_tid"]), edge_set)
        # 环首尾闭合
        first = graph["cycle_edges"][0]
        last = graph["cycle_edges"][-1]
        self.assertEqual(first["from_tid"], last["to_tid"])


class SelfReadTests(unittest.TestCase):
    def test_transaction_reads_own_writes(self) -> None:
        report = _audit_suite("self_read")
        view = report["view_serializability"]
        self.assertEqual(view["legal_count"], 1)
        self.assertEqual(view["smallest_legal"], [1])
        # read 来源必须精确到具体 write_id，w1 与 w2 不可混同
        sources = [(r["seq"], r["read_from"]) for r in report["read_from"]]
        self.assertEqual(sources, [(1, INITIAL), (3, "w1"), (5, "w2")])
        self.assertTrue(report["verdict"]["conflict_serializable"])
        # 单事务图不可能有环、也没有跨事务边
        self.assertEqual(report["conflict_graph"]["edges"], [])


class BlindWriteCycleTests(unittest.TestCase):
    """题目核心反例：盲写冲突图有环 ≠ 视图不可串行化（需三事务）。"""

    def test_three_txn_blind_write_cycle_view_serializable(self) -> None:
        report = _audit_suite("blind_write_cycle_view_ok")
        graph = report["conflict_graph"]
        self.assertTrue(graph["has_cycle"], "盲写交叉应使冲突图成环")
        self.assertFalse(report["verdict"]["conflict_serializable"])
        self.assertTrue(report["verdict"]["view_serializable"],
                        "T3 是全部键最终写入者，T3 收尾的排列都视图等价")
        view = report["view_serializability"]
        self.assertEqual(view["legal_count"], 2)
        self.assertEqual(view["smallest_legal"], [1, 2, 3])
        self.assertEqual(
            view["all_legal_permutations"], [[1, 2, 3], [2, 1, 3]])
        # 历史中没有任何 read
        self.assertEqual(report["read_from"], [])
        # 最终写入者：x=w5、y=w6，均属 T3
        fw = {e["key"]: e["final_write_id"] for e in report["final_writers"]}
        self.assertEqual(fw, {"x": "w5", "y": "w6"})
        # 成环的两个方向边都要有可核验证据
        edge_evidence = {
            (e["from_tid"], e["to_tid"]): e["evidence"] for e in graph["edges"]
        }
        self.assertIn((1, 2), edge_evidence)
        self.assertIn((2, 1), edge_evidence)
        for ev_list in edge_evidence.values():
            self.assertGreaterEqual(len(ev_list), 1)

    def test_two_txn_blind_write_cycle_has_zero_legal_permutations(self) -> None:
        # 两事务纯盲写环：最终写入者要求互相矛盾，视图等价排列数必须为 0
        report = _audit_suite("blind_write_cycle_two_txn")
        self.assertTrue(report["conflict_graph"]["has_cycle"])
        self.assertFalse(report["verdict"]["conflict_serializable"])
        self.assertFalse(report["verdict"]["view_serializable"])
        self.assertEqual(report["view_serializability"]["legal_count"], 0)
        self.assertIsNone(report["view_serializability"]["smallest_legal"])
        self.assertEqual(
            report["view_serializability"]["total_permutations_enumerated"], 2)

    def test_each_permutation_independently_simulated(self) -> None:
        # 逐事务排列独立模拟：T3 收尾的排列合法，T3 不收尾则最终写入者不符
        history = _history_of("blind_write_cycle_view_ok")
        ok_123, fail_123 = check_permutation(history, (1, 2, 3))
        ok_213, fail_213 = check_permutation(history, (2, 1, 3))
        ok_321, fail_321 = check_permutation(history, (3, 2, 1))
        self.assertTrue(ok_123)
        self.assertEqual(fail_123, [])
        self.assertTrue(ok_213)
        self.assertFalse(ok_321)
        self.assertTrue(
            all(f["reason"] == "FINAL_WRITER_MISMATCH" for f in fail_321))
        self.assertEqual(len(fail_321), 2)


class SameValueIdentityTests(unittest.TestCase):
    def test_same_value_distinct_write_ids_ok(self) -> None:
        report = _audit_suite("same_value_identity_ok")
        self.assertEqual(report["status"], "ok")
        # seq3 的 read 必须来源 w2 而不是值相同的 w1
        self.assertEqual(report["read_from"][0]["read_from"], "w2")
        self.assertEqual(report["final_writers"][0]["final_write_id"], "w2")

    def test_same_value_wrong_source_is_input_error(self) -> None:
        report = _audit_suite("same_value_bad_source")
        self.assertEqual(report["status"], "input_semantic_error")
        self.assertIsNone(report["view_serializability"])
        self.assertIsNone(report["conflict_graph"])
        bad = [e for e in report["errors"] if e["code"] == "BAD_READ_SOURCE"]
        self.assertEqual(len(bad), 1)
        self.assertEqual(bad[0]["seq"], 3)
        # 实际值相同 → 不应报值错误，只报来源身份错误
        self.assertNotIn("BAD_READ_VALUE", {e["code"] for e in report["errors"]})


class ViewButNotConflictTests(unittest.TestCase):
    def test_view_serializable_but_conflict_not(self) -> None:
        report = _audit_suite("view_but_not_conflict")
        self.assertTrue(report["verdict"]["view_serializable"])
        self.assertFalse(report["verdict"]["conflict_serializable"])
        graph = report["conflict_graph"]
        self.assertTrue(graph["has_cycle"])
        self.assertEqual(report["view_serializability"]["smallest_legal"], [1, 2, 3, 4])
        # 含 read 的环边与盲写环边都应出现
        edge_set = {(e["from_tid"], e["to_tid"]) for e in graph["edges"]}
        self.assertIn((3, 4), edge_set)  # w4 与 w3 在 x 上反向冲突
        self.assertIn((4, 3), edge_set)


class UntouchedKeyTests(unittest.TestCase):
    def test_untouched_key_stays_initial(self) -> None:
        report = _audit_suite("untouched_key")
        fw = {e["key"]: e["final_write_id"] for e in report["final_writers"]}
        self.assertEqual(fw["y"], INITIAL)
        self.assertEqual(fw["x"], "w1")
        self.assertTrue(report["verdict"]["view_serializable"])


class IndependentCrossCheckTests(unittest.TestCase):
    """<=6 事务：独立枚举与主枚举对每个排列核对来源身份与最终写入者。"""

    def test_all_small_suites_cross_check(self) -> None:
        for name in FIXED_SUITES:
            if name == "same_value_bad_source":
                continue
            with self.subTest(name=name):
                history = _history_of(name)
                self.assertLessEqual(len(history.tids), 6)
                # 主枚举（itertools）与独立递归枚举的合法集合必须逐排列相同
                main_set = [list(p) for p in permutations(history.tids)
                            if check_permutation(history, p)[0]]
                indep_set = enumerate_legal_recursive(history)
                self.assertEqual(sorted(main_set), sorted(indep_set))
                # 独立回放核对来源身份与最终写入者
                source, _value, problems = independent_replay(history)
                self.assertEqual(problems, [])
                for k in history.keys:
                    self.assertEqual(source[k], history.final_writer[k])
                # 冲突边集合双实现一致
                report = run_audit(FIXED_SUITES[name], canonical_dumps(FIXED_SUITES[name]))
                reported = {(e["from_tid"], e["to_tid"])
                            for e in report["conflict_graph"]["edges"]}
                self.assertEqual(independent_conflict_edges(history), reported)
                # verify_analysis 整体必须通过
                vr = verify_analysis(history, report)
                self.assertTrue(vr.passed, msg=vr.discrepancies)

    def test_identity_not_string_comparison(self) -> None:
        """字符串全程相同，但排列改变了 read 的来源 write 身份，必须判失败。"""
        history = _history_of("same_value_permutation_identity")
        # 合法排列：T3 必须在 T2 之后（其 read 来源是 w2）
        ok_123, fail_123 = check_permutation(history, (1, 2, 3))
        self.assertTrue(ok_123)
        self.assertEqual(fail_123, [])
        # T3 排到最前：它 read 时 x 没有任何 write，来源身份是 INITIAL 而非 w2；
        # 但 INITIAL/w1/w2 的字符串全是 SAME，读值完全相同——只可能靠身份抓到
        ok_312, fail_312 = check_permutation(history, (3, 1, 2))
        self.assertFalse(ok_312)
        source_failures = [f for f in fail_312 if f["reason"] == "READ_SOURCE_MISMATCH"]
        self.assertEqual(len(source_failures), 1)
        self.assertEqual(source_failures[0]["expected_source"], "w2")
        self.assertEqual(source_failures[0]["actual_source_in_permutation"], INITIAL)
        self.assertNotIn("READ_VALUE_MISMATCH",
                         {f["reason"] for f in fail_312})
        # 该历史唯一合法排列 [1,2,3]：T3 的 read 依赖 w2（T2 在 T3 前），
        # 且 w2 是 x 的最终写入者（T2 必须最后一个写 x，即 T2 在 T1 后）
        report = _audit_suite("same_value_permutation_identity")
        self.assertEqual(report["view_serializability"]["legal_count"], 1)
        self.assertEqual(report["view_serializability"]["smallest_legal"], [1, 2, 3])


class InputValidationTests(unittest.TestCase):
    def _base(self) -> dict:
        return json.loads(json.dumps(FIXED_SUITES["serial"]))

    def test_missing_commit(self) -> None:
        data = self._base()
        data["operations"] = [o for o in data["operations"] if o["op"] != "commit"]
        report = run_audit(data, canonical_dumps(data))
        self.assertEqual(report["status"], "input_semantic_error")
        self.assertIn("MISSING_COMMIT", {e["code"] for e in report["errors"]})

    def test_op_after_commit(self) -> None:
        data = self._base()
        # T1 在 seq4 已 commit，追加一条提交后的 write
        data["operations"].append(
            {"seq": 100, "transaction": 1, "op": "write",
             "write_id": "w9", "key": "x", "value": "z"})
        report = run_audit(data, canonical_dumps(data))
        codes = {e["code"] for e in report["errors"]}
        self.assertIn("OP_AFTER_COMMIT", codes)
        self.assertNotIn("MULTI_COMMIT", codes)

    def test_double_commit(self) -> None:
        data = self._base()
        # 第二次 commit 必须单独定性为 MULTI_COMMIT
        data["operations"].insert(4, {"seq": 99, "transaction": 1, "op": "commit"})
        report = run_audit(data, canonical_dumps(data))
        codes = {e["code"] for e in report["errors"]}
        self.assertIn("MULTI_COMMIT", codes)
        self.assertNotIn("OP_AFTER_COMMIT", codes)

    def test_unknown_key_rejected(self) -> None:
        data = self._base()
        data["operations"].append(
            {"seq": 100, "transaction": 2, "op": "write",
             "write_id": "wx", "key": "NEW", "value": "z"})
        report = run_audit(data, canonical_dumps(data))
        self.assertIn("UNKNOWN_KEY", {e["code"] for e in report["errors"]})

    def test_dup_seq_and_write_id(self) -> None:
        data = self._base()
        # 用唯一 seq 避免 DUP_SEQ 短路，专门验证重复 write_id
        data["operations"].append(
            {"seq": 100, "transaction": 2, "op": "write",
             "write_id": "w1", "key": "x", "value": "z"})
        report = run_audit(data, canonical_dumps(data))
        self.assertIn("DUP_WRITE_ID", {e["code"] for e in report["errors"]})
        # 同一条操作再制造 seq 重复，验证两类错误可同时报出
        data["operations"].append(
            {"seq": 1, "transaction": 2, "op": "write",
             "write_id": "w2dup", "key": "y", "value": "z"})
        report = run_audit(data, canonical_dumps(data))
        codes = {e["code"] for e in report["errors"]}
        self.assertIn("DUP_SEQ", codes)
        self.assertIn("DUP_WRITE_ID", codes)

    def test_multiple_errors_collected_together(self) -> None:
        """未知键、坏 read_from 引用、缺少 commit 必须一次性全部报出。"""
        data = {
            "history_id": "multi_error",
            "keys": {"x": "x0"},
            "transactions": [1],
            "operations": [
                {"seq": 1, "transaction": 1, "op": "write",
                 "write_id": "w1", "key": "z", "value": "a"},
                {"seq": 2, "transaction": 1, "op": "read",
                 "key": "x", "value": "x0", "read_from": "nope"},
            ],
        }
        report = run_audit(data, canonical_dumps(data))
        codes = {e["code"] for e in report["errors"]}
        self.assertIn("UNKNOWN_KEY", codes)
        self.assertIn("UNKNOWN_READ_FROM", codes)
        self.assertIn("MISSING_COMMIT", codes)
        self.assertEqual(report["status"], "input_semantic_error")

    def test_uncommitted_read_sees_uncommitted_write(self) -> None:        # 未提交写入也必须能被读到：w1(T1 未提交) 后 T2 读到 w1 是合法的
        data = {
            "history_id": "dirty_read_allowed",
            "keys": {"x": "x0"},
            "transactions": [1, 2],
            "operations": [
                {"seq": 1, "transaction": 1, "op": "write", "write_id": "w1", "key": "x", "value": "a"},
                {"seq": 2, "transaction": 2, "op": "read", "key": "x", "value": "a", "read_from": "w1"},
                {"seq": 3, "transaction": 1, "op": "commit"},
                {"seq": 4, "transaction": 2, "op": "commit"},
            ],
        }
        history, errors = parse_and_validate(data)
        self.assertEqual(errors, [])
        self.assertIsNotNone(history)


class DeterminismAndBenchmarkTests(unittest.TestCase):
    def test_repeat_input_byte_identical_report(self) -> None:
        data = generate_random_history(seed=42, n_transactions=8, n_keys=10, n_ops=80)
        raw = canonical_dumps(data)
        r1 = canonical_dumps(run_audit(data, raw))
        r2 = canonical_dumps(run_audit(data, raw))
        self.assertEqual(r1, r2)
        self.assertEqual(fingerprint(r1), fingerprint(r2))

    def test_random_generation_is_valid_and_reproducible(self) -> None:
        a = generate_random_history(seed=7, n_transactions=5, n_keys=6, n_ops=40)
        b = generate_random_history(seed=7, n_transactions=5, n_keys=6, n_ops=40)
        self.assertEqual(a, b)
        history, errors = parse_and_validate(a)
        self.assertEqual(errors, [])
        self.assertIsNotNone(history)
        # 每个事务恰好一次 commit 且 commit 在最后
        last_kind = {}
        commit_cnt: dict[int, int] = {}
        for op in history.ops:
            last_kind[op.tid] = op.kind
            if op.kind == "commit":
                commit_cnt[op.tid] = commit_cnt.get(op.tid, 0) + 1
        self.assertTrue(all(c == 1 for c in commit_cnt.values()))
        self.assertTrue(all(k == "commit" for k in last_kind.values()))

    def test_eight_transaction_runtime_reproducible(self) -> None:
        """8 事务上限：40320 个排列全枚举，记录可复现运行时间（宽松上界）。"""
        data = generate_random_history(
            seed=20261113, n_transactions=8, n_keys=12, n_ops=64)
        raw = canonical_dumps(data)
        t0 = time.perf_counter()
        report = run_audit(data, raw)
        elapsed = time.perf_counter() - t0
        self.assertEqual(
            report["view_serializability"]["total_permutations_enumerated"], 40320)
        # 两次运行耗时分别记录；正确性输出必须完全一致
        report2 = run_audit(data, raw)
        self.assertEqual(canonical_dumps(report), canonical_dumps(report2))
        # 全枚举在普通机器上应为亚秒级；CI 环境给宽松上界 30 秒
        self.assertLess(elapsed, 30.0, f"8 事务枚举耗时 {elapsed:.2f}s 超出上界")
        print(f"\n8 事务 40320 排列全枚举（含独立复核）耗时 {elapsed*1000:.1f} ms；"
              f"合法排列 {report['view_serializability']['legal_count']} 个")

    def test_eight_transaction_limit_constants(self) -> None:
        # 规模上限边界：恰好 8 事务/20 键/200 操作可被接受
        data = generate_random_history(
            seed=1, n_transactions=8, n_keys=20, n_ops=200)
        history, errors = parse_and_validate(data)
        self.assertEqual(errors, [])
        self.assertEqual(len(history.ops), 200)
        self.assertEqual(len(history.keys), 20)


class StorageAndTamperTests(unittest.TestCase):
    def test_sqlite_roundtrip_and_tamper_detection(self) -> None:
        import tempfile
        from txaudit.storage import AuditStore

        data = FIXED_SUITES["blind_write_cycle_view_ok"]
        raw = canonical_dumps(data)
        report = run_audit(data, raw)
        report_text = canonical_dumps(report)

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "audit.db"
            with AuditStore(db_path) as store:
                store.save(report["history_id"], report["input_fingerprint_sha256"],
                           report["status"], raw, report_text)
                row = store.load(report["history_id"])
            self.assertIsNotNone(row)
            self.assertEqual(row["report_json"], report_text)
            self.assertEqual(row["input_json"], raw)
            self.assertEqual(row["status"], "ok")

            # 篡改报告后独立复核必须失败（退出语义由 verify_analysis 的 passed 体现）
            history, errors = parse_and_validate(data)
            self.assertEqual(errors, [])
            tampered = json.loads(report_text)
            tampered["view_serializability"]["legal_count"] = 999
            from txaudit.verify import verify_analysis
            vr = verify_analysis(history, tampered)
            self.assertFalse(vr.passed)
            self.assertTrue(
                any("legal_permutation_count" in d for d in vr.discrepancies))

            # 同一 history_id 重新保存应覆盖而不是报错
            with AuditStore(db_path) as store:
                store.save(report["history_id"], report["input_fingerprint_sha256"],
                           report["status"], raw, report_text)
                rows = store.list_runs()
            self.assertEqual(len(rows), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
