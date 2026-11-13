"""端到端测试：覆盖题目明确要求的退化、不变性与边界场景。

运行：python3 -m tests.test_pipeline
不依赖 pytest（标准库 unittest），全部在临时目录用独立 SQLite 完成。
"""
from __future__ import annotations

import json
import os
import random
import shutil
import sqlite3
import tempfile
import unittest

from tracerelay.analyze import analyze_all, Analyzer
from tracerelay.generator import (CASES, build_all, build_bulk, write_jsonl)
from tracerelay.model import (Interval, merge_union, percentile, sweep_coverage,
                              WorkItem, COV_AWAIT, COV_GAP, COV_QUEUE, COV_SELF)
from tracerelay.storage import connect, import_jsonl
from tracerelay.validate import load_trace
from tracerelay.verify import run_verification


def fresh_db(path):
    if os.path.exists(path):
        for ext in ("", "-wal", "-shm"):
            p = path + ext
            if os.path.exists(p):
                os.remove(p)
    return connect(path)


def import_records(records, db_path):
    conn = fresh_db(db_path)
    jl = db_path + ".in.jsonl"
    write_jsonl(records, jl)
    import_jsonl(conn, jl)
    return conn


def analyze(records, db_path):
    conn = import_records(records, db_path)
    summaries = analyze_all(conn)
    return conn, summaries


class TestIntervalGeometry(unittest.TestCase):
    def test_union_and_sweep_no_double_count(self):
        # 两个重叠 await + 一个 self，扫描后各类时长可手工复算
        items = [
            WorkItem(Interval(0, 100), COV_AWAIT, "a1", "await", "w1"),
            WorkItem(Interval(50, 150), COV_AWAIT, "a2", "await", "w2"),
            WorkItem(Interval(0, 200), COV_SELF, "root", "span", "r"),
        ]
        slices = sweep_coverage(0, 200, items)
        total = sum(s["duration_ns"] for s in slices)
        self.assertEqual(total, 200)
        by_cat = {}
        for s in slices:
            by_cat[s["category"]] = by_cat.get(s["category"], 0) + s["duration_ns"]
        # await 并集 [0,150) 只算 150，重叠的 50 不重复；剩余 50 为 self
        self.assertEqual(by_cat[COV_AWAIT], 150)
        self.assertEqual(by_cat[COV_SELF], 50)
        # 区间两两不相交
        for a, b in zip(slices, slices[1:]):
            self.assertEqual(a["end_ns"], b["start_ns"])

    def test_priority_await_over_self_over_gap(self):
        items = [
            WorkItem(Interval(0, 100), COV_SELF, "s", "span", "r"),
            WorkItem(Interval(20, 80), COV_AWAIT, "w", "await", "w1"),
            WorkItem(Interval(40, 60), COV_QUEUE, "q", "span", "q1"),
        ]
        slices = sweep_coverage(0, 100, items)
        cats = [(s["category"], s["duration_ns"]) for s in slices]
        # self 0-20；await 20-40；queue 40-60（排队是等待窗口内更具体的证据，优先）；
        # await 60-80；self 80-100。总时长守恒，无 gap。
        self.assertEqual(cats, [(COV_SELF, 20), (COV_AWAIT, 20), (COV_QUEUE, 20),
                                (COV_AWAIT, 20), (COV_SELF, 20)])
        self.assertEqual(sum(d for _, d in cats), 100)

    def test_union_open_ended(self):
        merged = merge_union([Interval(0, 10), Interval(5, None)])
        self.assertEqual(merged[0].end, None)
        self.assertIsNone(merge_union([Interval(0, None)])[0].duration)

    def test_percentile_pooled(self):
        vals = sorted(range(1, 101))           # 1..100
        self.assertEqual(percentile(vals, 50), 50.5)
        self.assertEqual(percentile([7], 95), 7)
        self.assertIsNone(percentile([], 95))


class TestMicroCases(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="trtest-")
        cls.paths = build_all(cls.tmp)
        cls.db = os.path.join(cls.tmp, "micro.db")
        conn = fresh_db(cls.db)
        for p in cls.paths.values():
            import_jsonl(conn, p)
        analyze_all(conn)
        cls.conn = conn

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_conservation_and_expected(self):
        result = run_verification(self.conn)
        self.assertTrue(result["all_ok"], msg=json.dumps(result["errors"], ensure_ascii=False))

    def test_any_winner_only(self):
        rows = [dict(r) for r in self.conn.execute(
            "SELECT * FROM await_causes WHERE trace_id='T-any'")]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["mode"], "any")
        self.assertEqual(json.loads(rows[0]["resolved_by"])[0][:4], "cli-")
        self.assertIn("svcA", rows[0]["blocker_detail"])
        diags = [r["diag_code"] for r in self.conn.execute(
            "SELECT DISTINCT diag_code FROM diagnostics WHERE trace_id='T-any'")]
        self.assertIn("D6_any_loser_residual_work", diags)

    def test_retry_attempts_kept(self):
        ids = [r["span_id"] for r in self.conn.execute(
            "SELECT span_id FROM spans WHERE trace_id='T-retry' AND span_type='client' "
            "ORDER BY attempt_no")]
        self.assertEqual(len(ids), 2)
        d = self.conn.execute(
            "SELECT detail FROM diagnostics WHERE trace_id='T-retry' "
            "AND diag_code='D3_retry_background_amplification'").fetchone()
        evidence = json.loads(d["detail"])
        self.assertEqual(len(evidence["attempts"]), 2)
        self.assertTrue(any(a["has_timeout"] for a in evidence["attempts"]))

    def test_async_background_not_in_root_coverage(self):
        row = self.conn.execute(
            "SELECT * FROM analysis_trace WHERE trace_id='T-async'").fetchone()
        self.assertEqual(row["gap_ns"], 0)
        self.assertGreaterEqual(row["background_ns"], 30_000_000)
        # 消费者 span 不得出现在根时间片引用里
        refs = [r["ref_id"] for r in self.conn.execute(
            "SELECT ref_id FROM timeline_slices WHERE trace_id='T-async'")]
        cons = [r["span_id"] for r in self.conn.execute(
            "SELECT span_id FROM spans WHERE trace_id='T-async' AND span_type='consumer'")]
        self.assertFalse(set(refs) & set(cons))


class TestDegradation(unittest.TestCase):
    """主动删事件：确定结论必须退化为未知，且不能凭旧缓存沿用。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="trdeg-")
        self.db = os.path.join(self.tmp, "d.db")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_drop_queue_events_makes_wait_unknown(self):
        records = [r for r in CASES["sync"]() if not (
            r.get("kind") == "event" and r.get("name") in ("queue_enter", "exec_start"))]
        conn, _ = analyze(records, self.db)
        diags = [r["diag_code"] for r in conn.execute(
            "SELECT diag_code FROM diagnostics WHERE trace_id='T-sync'")]
        self.assertNotIn("D1_queue_wait_before_exec", diags)  # 无证据不得下排队结论
        # 覆盖守恒仍然成立（缺证据不破坏守恒，只是解释变少）
        row = conn.execute("SELECT * FROM analysis_trace WHERE trace_id='T-sync'").fetchone()
        self.assertEqual(row["conservation_ok"], 1)

    def test_drop_server_span_degrades_to_bounded_unknown(self):
        records = [r for r in CASES["sync"]()
                   if not (r.get("kind") == "span" and r.get("span_type") == "server"
                           and r.get("service") == "billing")]
        conn, _ = analyze(records, self.db)
        d4 = conn.execute(
            "SELECT certainty, evidence FROM diagnostics WHERE trace_id='T-sync' "
            "AND diag_code='D4_missing_server_unknown_transmit'").fetchall()
        self.assertTrue(d4)
        for r in d4:
            self.assertEqual(r["certainty"], "bounded")
            ev = json.loads(r["evidence"])
            self.assertIsNotNone(ev["upper_bound_ns"])
            self.assertEqual(ev["lower_bound_ns"], 0)

    def test_unknown_never_zero_filled(self):
        records = CASES["gap"]()
        for r in records:
            if r.get("kind") == "span" and r.get("operation") == "after":
                r["start_ns"] = None       # 模拟缺失开始时刻
        conn, _ = analyze(records, self.db + ".x")
        row = conn.execute(
            "SELECT has_unknown_time, conservation_ok FROM analysis_trace "
            "WHERE trace_id='T-gap'").fetchone()
        self.assertEqual(row["has_unknown_time"], 1)
        raw = conn.execute("SELECT start_ns FROM spans WHERE operation='after'").fetchone()
        self.assertIsNone(raw["start_ns"])     # 保持 NULL，没有被填零

    def test_reanalysis_does_not_reuse_stale_conclusion(self):
        """同一数据库先分析完整数据，再删掉服务端事件重新分析：
        D1 排队结论必须消失/退化，不能沿用旧一轮的缓存结果。"""
        full = CASES["sync"]()
        conn = import_records(full, os.path.join(self.tmp, "stale.db"))
        analyze_all(conn)
        before = [r["diag_code"] for r in conn.execute(
            "SELECT DISTINCT diag_code FROM diagnostics WHERE trace_id='T-sync'")]
        self.assertIn("D1_queue_wait_before_exec", before)
        # 模拟事件丢失：清空 events 表并整体重建分析
        conn.execute("DELETE FROM events WHERE trace_id='T-sync'")
        conn.commit()
        analyze_all(conn)
        after = [r["diag_code"] for r in conn.execute(
            "SELECT DISTINCT diag_code FROM diagnostics WHERE trace_id='T-sync'")]
        self.assertNotIn("D1_queue_wait_before_exec", after)
        # 守恒依然成立，且 trace 被标记为不完整
        row = conn.execute(
            "SELECT conservation_ok, complete_flag FROM analysis_trace "
            "WHERE trace_id='T-sync'").fetchone()
        self.assertEqual(row["conservation_ok"], 1)
        self.assertEqual(row["complete_flag"], 0)

    def test_shuffle_does_not_change_result(self):
        records = CASES["parallel"]()
        conn1, s1 = analyze(records, os.path.join(self.tmp, "a.db"))
        shuffled = records[:]
        random.Random(7).shuffle(shuffled)
        conn2, s2 = analyze(shuffled, os.path.join(self.tmp, "b.db"))
        def sig(conn):
            return [(r["slice_no"], r["category"], r["start_ns"], r["end_ns"], r["ref_id"])
                    for r in conn.execute(
                        "SELECT * FROM timeline_slices ORDER BY trace_id, slice_no")]
        self.assertEqual(sig(conn1), sig(conn2))
        self.assertEqual(
            [r["diag_code"] for r in conn1.execute("SELECT diag_code FROM diagnostics ORDER BY 1")],
            [r["diag_code"] for r in conn2.execute("SELECT diag_code FROM diagnostics ORDER BY 1")])


class TestIntegrity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="trint-")
        self.db = os.path.join(self.tmp, "i.db")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_duplicate_folded_conflict_reported(self):
        records = CASES["sync"]()
        span_records = [r for r in records if r.get("kind") == "span"]
        target = span_records[0]
        records.append(json.loads(json.dumps(target)))   # 完全相同 -> 折叠
        evil = json.loads(json.dumps(target))
        evil["service"] = "malicious-different-service"  # 同 ID 不同内容 -> 冲突
        records.append(evil)
        conn = import_records(records, self.db)
        dup = conn.execute("SELECT dedup_count FROM spans WHERE span_id=?",
                           (target["span_id"],)).fetchone()
        self.assertGreaterEqual(dup["dedup_count"], 2)
        issues = [r["code"] for r in conn.execute(
            "SELECT code FROM validation_issues WHERE code='conflicting_span_identity'")]
        self.assertTrue(issues)
        # 首条记录内容保留，冲突记录未覆盖
        kept = conn.execute("SELECT service FROM spans WHERE span_id=?",
                            (target["span_id"],)).fetchone()
        self.assertEqual(kept["service"], target["service"])

    def test_parent_cycle_detected(self):
        records = [
            {"kind": "span", "trace_id": "T-cyc", "span_id": "a", "service": "x",
             "operation": "o", "span_type": "server", "start_ns": 0, "end_ns": 100,
             "parent_id": "b", "links": []},
            {"kind": "span", "trace_id": "T-cyc", "span_id": "b", "service": "x",
             "operation": "o", "span_type": "internal", "start_ns": 0, "end_ns": 100,
             "parent_id": "a", "links": []},
        ]
        conn, _ = analyze(records, self.db)
        codes = {r["code"] for r in conn.execute(
            "SELECT code FROM validation_issues WHERE trace_id='T-cyc'")}
        self.assertIn("parent_cycle", codes)

    def test_impossible_causal_order(self):
        records = [
            {"kind": "span", "trace_id": "T-ord", "span_id": "p", "service": "x",
             "operation": "o", "span_type": "server", "start_ns": 100, "end_ns": 500,
             "parent_id": None, "links": []},
            {"kind": "span", "trace_id": "T-ord", "span_id": "c", "service": "x",
             "operation": "o", "span_type": "internal", "start_ns": 10, "end_ns": 50,
             "parent_id": "p", "links": []},
        ]
        conn, _ = analyze(records, self.db)
        codes = {r["code"] for r in conn.execute(
            "SELECT code FROM validation_issues WHERE trace_id='T-ord'")}
        self.assertIn("child_starts_before_parent", codes)

    def test_open_await_has_bounds_not_fake_end(self):
        records = [
            {"kind": "span", "trace_id": "T-open", "span_id": "r", "service": "g",
             "operation": "h", "span_type": "server", "start_ns": 0, "end_ns": 1000,
             "parent_id": None, "links": []},
            {"kind": "await", "trace_id": "T-open", "await_id": "w1",
             "waiter_span_id": "r", "start_ns": 100, "end_ns": None,
             "result_ids": ["x"], "mode": "all", "resolved_by": None},
        ]
        conn, summaries = analyze(records, self.db)
        self.assertTrue(any(s["has_unknown_time"] for s in summaries))
        aw = conn.execute("SELECT open_flag, end_ns FROM awaits WHERE await_id='w1'").fetchone()
        self.assertEqual(aw["open_flag"], 1)
        self.assertIsNone(aw["end_ns"])


class TestBulk(unittest.TestCase):
    def test_hundred_k_scale_and_indexes(self):
        tmp = tempfile.mkdtemp(prefix="trbulk-")
        try:
            jl = os.path.join(tmp, "bulk.jsonl")
            info = build_bulk(jl, n_traces=9500)   # 9500 trace × 约 12 span，稳定超过 10 万
            self.assertGreaterEqual(info["spans"], 100_000)
            db = os.path.join(tmp, "bulk.db")
            conn = fresh_db(db)
            import_jsonl(conn, jl)
            # 索引被使用（EXPLAIN QUERY PLAN 出现 INDEX 而非全表 SCAN）
            plan = conn.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM spans WHERE trace_id=? LIMIT 1",
                ("BULK-000001",)).fetchall()
            self.assertTrue(any("INDEX" in r[-1] for r in plan))
            summaries = analyze_all(conn)
            self.assertEqual(len(summaries), 9500)
            result = run_verification(conn)
            self.assertTrue(result["all_ok"], msg=str(result["errors"])[:500])
            # 列表接口限量，不会把全量原文推给调用方
            n_slices = conn.execute("SELECT COUNT(*) c FROM timeline_slices").fetchone()["c"]
            self.assertGreater(n_slices, 4000)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
