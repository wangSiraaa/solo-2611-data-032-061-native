#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mvcc.py — 小型多版本键值 CLI(Python 3.11 + SQLite,快照隔离)

语义摘要(完整说明见 README.md):
  * 键/值均为非空 UTF-8 字符串;删除用独立墓碑(tombstone)表示,不允许空串冒充删除。
  * 每次成功的写事务分配严格递增的整数提交版本;只读事务不消耗版本号。
  * begin 记录当时最新已提交版本为 begin_version;事务编号由调用者提供,
    重复 begin 返回原事务身份,不创建新快照;事务跨 CLI 调用存活,无超时过期。
  * 事务内 get/scan 读取 <= begin_version 的最新已提交值,再叠加本事务私有写;
    其他事务未提交的修改不可见。
  * 提交时若写集中任一键在 begin_version 之后被其他事务成功修改,整笔写事务
    冲突失败,私有修改均不发布;写不同键可成功(不承诺消除跨键写偏差)。
  * 一个事务的多键版本、提交编号与终态在同一 SQLite 事务中原子持久化;
    重复提交返回原回执,不产生第二个提交版本。
  * gc 保留:每键最新版本 + 每个活跃快照实际会读到的版本;其余已提交版本物理
    删除;墓碑同样参与可见性;事务终态与幂等回执永不回收。

退出码:0 = 命令已执行(写冲突也是正常结果,见 JSON status);
       2 = 用法/状态错误;1 = 内部错误;118 = 故障注入点触发的模拟崩溃。
"""
from __future__ import annotations

import argparse
import bisect
import json
import os
import random
import sqlite3
import subprocess
import sys
import time

# ---------------------------------------------------------------------------
# 数据库结构
# ---------------------------------------------------------------------------
# meta          : 单行,next_version 为下一个待分配的提交版本(首个提交为 1)。
# txns          : 事务登记簿。txn_id 由调用者提供;state ∈ active/committed/
#                 aborted/conflicted;receipt 为终态回执(JSON),用于幂等重放。
#                 事务终态与回执永不删除,不随业务版本回收。
# kv_versions   : 已提交的多版本数据,(key, version) 主键;tombstone=1 表示
#                 删除墓碑,此时 value 为 NULL。gc 只清理本表。
# writes        : 各活跃事务的私有写缓冲区,提交时发布、中止/冲突时丢弃;
#                 其他事务不可见,gc 不参与本表。
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  id            INTEGER PRIMARY KEY CHECK (id = 1),
  next_version  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS txns (
  txn_id         TEXT PRIMARY KEY,
  begin_version  INTEGER NOT NULL,
  state          TEXT NOT NULL CHECK (state IN ('active','committed','aborted','conflicted')),
  commit_version INTEGER,
  receipt        TEXT,
  created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ended_at       TEXT
);
CREATE TABLE IF NOT EXISTS kv_versions (
  key       TEXT    NOT NULL,
  version   INTEGER NOT NULL,
  value     TEXT,
  tombstone INTEGER NOT NULL CHECK (tombstone IN (0, 1)),
  txn_id    TEXT    NOT NULL,
  PRIMARY KEY (key, version)
);
CREATE INDEX IF NOT EXISTS idx_kv_versions_version ON kv_versions(version);
CREATE TABLE IF NOT EXISTS writes (
  txn_id    TEXT    NOT NULL,
  key       TEXT    NOT NULL,
  value     TEXT,
  tombstone INTEGER NOT NULL CHECK (tombstone IN (0, 1)),
  PRIMARY KEY (txn_id, key)
);
"""


class CliError(Exception):
    """用法/状态错误,退出码 2。"""


def emit(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False))


# ---------------------------------------------------------------------------
# 故障注入(故障控制,详见 README.md)
#   MVCC_FAILPOINT="点1,点2"  命中时 os._exit(118),模拟 kill -9(无清理退出)
#   MVCC_SLEEP="点=毫秒,..."  命中时先在事务内睡眠,便于真实 kill 落在事务中间
# ---------------------------------------------------------------------------
def failpoint(name: str) -> None:
    fps = [t.strip() for t in os.environ.get("MVCC_FAILPOINT", "").split(",") if t.strip()]
    if name in fps:
        os._exit(118)
    for tok in os.environ.get("MVCC_SLEEP", "").split(","):
        tok = tok.strip()
        if tok.startswith(name + "="):
            try:
                time.sleep(int(tok.split("=", 1)[1]) / 1000.0)
            except ValueError:
                pass


def connect(db_path: str) -> sqlite3.Connection:
    # isolation_level=None:自动提交模式,事务边界全部由显式 BEGIN/COMMIT 控制。
    con = sqlite3.connect(db_path, timeout=30.0, isolation_level=None)
    con.execute("PRAGMA journal_mode=WAL")        # 读写不互斥,崩溃后由 WAL 恢复
    con.execute("PRAGMA synchronous=FULL")        # 提交即落盘,回执不丢
    con.execute("PRAGMA busy_timeout=30000")      # 写者串行排队而非立即报错
    return con


def ensure_initialized(con: sqlite3.Connection) -> None:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone()
    if not row:
        raise CliError("数据库未初始化,请先运行: mvcc.py --db <path> init")


def check_key(key) -> None:
    if not key:
        raise CliError("键必须为非空 UTF-8 字符串")


def check_value(value) -> None:
    if not value:
        raise CliError("值必须为非空字符串;删除请使用 delete(独立墓碑),不允许空串冒充删除")


def load_txn(con, txn_id):
    return con.execute(
        "SELECT begin_version, state, commit_version, receipt FROM txns WHERE txn_id=?",
        (txn_id,)).fetchone()


def require_active(con, txn_id) -> int:
    row = load_txn(con, txn_id)
    if row is None:
        raise CliError(f"事务不存在: {txn_id!r}(请先 begin)")
    if row[1] != "active":
        raise CliError(f"事务已结束: {txn_id!r} state={row[1]}")
    return row[0]


def finalize_txn(con, txn_id, state, commit_version, receipt: dict) -> None:
    """在同一 SQLite 事务内落终态并丢弃私有写;与版本发布同属一个原子单元。"""
    con.execute(
        "UPDATE txns SET state=?, commit_version=?, receipt=?, "
        "ended_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE txn_id=?",
        (state, commit_version, json.dumps(receipt, ensure_ascii=False), txn_id))
    con.execute("DELETE FROM writes WHERE txn_id=?", (txn_id,))


def rollback_safely(con) -> None:
    if con.in_transaction:
        con.execute("ROLLBACK")


# ---------------------------------------------------------------------------
# 命令实现
# ---------------------------------------------------------------------------
def cmd_init(con, args) -> int:
    con.executescript(SCHEMA)
    con.execute("INSERT OR IGNORE INTO meta(id, next_version) VALUES (1, 1)")
    emit({"status": "initialized", "db": args.db})
    return 0


def cmd_begin(con, args) -> int:
    txn = args.txn
    # BEGIN IMMEDIATE:与 commit/gc 互斥,保证“读最新版本 + 登记快照”原子完成,
    # gc 不会漏掉刚建立的快照(见 README 并发安全论证)。
    con.execute("BEGIN IMMEDIATE")
    try:
        row = load_txn(con, txn)
        if row is not None:
            con.execute("COMMIT")
            emit({"status": "exists", "txn": txn, "begin_version": row[0],
                  "state": row[1], "commit_version": row[2],
                  "note": "重复 begin,返回原事务身份,未创建新快照"})
            return 0
        bv = con.execute("SELECT next_version - 1 FROM meta WHERE id=1").fetchone()[0]
        con.execute(
            "INSERT INTO txns(txn_id, begin_version, state) VALUES (?,?,'active')",
            (txn, bv))
        failpoint("begin:after_insert")
        con.execute("COMMIT")
    except Exception:
        rollback_safely(con)
        raise
    emit({"status": "begun", "txn": txn, "begin_version": bv, "state": "active"})
    return 0


def _apply_write(con, txn_id, key, value, tombstone) -> None:
    con.execute(
        "INSERT OR REPLACE INTO writes(txn_id, key, value, tombstone) VALUES (?,?,?,?)",
        (txn_id, key, value, tombstone))


def cmd_put(con, args) -> int:
    if args.file is None:
        check_key(args.key)
        check_value(args.value)
    con.execute("BEGIN IMMEDIATE")
    n = 0
    try:
        require_active(con, args.txn)
        if args.file is not None:
            with open(args.file, "r", encoding="utf-8") as f:
                for ln, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue
                    obj = json.loads(line)
                    check_key(obj.get("key"))
                    check_value(obj.get("value"))
                    _apply_write(con, args.txn, obj["key"], obj["value"], 0)
                    n += 1
        else:
            _apply_write(con, args.txn, args.key, args.value, 0)
            n = 1
        failpoint("put:after_write")
        con.execute("COMMIT")
    except Exception:
        rollback_safely(con)
        raise
    emit({"status": "ok", "txn": args.txn, "op": "put", "count": n})
    return 0


def cmd_delete(con, args) -> int:
    if args.file is None:
        check_key(args.key)
    con.execute("BEGIN IMMEDIATE")
    n = 0
    try:
        require_active(con, args.txn)
        if args.file is not None:
            with open(args.file, "r", encoding="utf-8") as f:
                for ln, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue
                    obj = json.loads(line)
                    check_key(obj.get("key"))
                    _apply_write(con, args.txn, obj["key"], None, 1)
                    n += 1
        else:
            _apply_write(con, args.txn, args.key, None, 1)
            n = 1
        failpoint("delete:after_write")
        con.execute("COMMIT")
    except Exception:
        rollback_safely(con)
        raise
    emit({"status": "ok", "txn": args.txn, "op": "delete", "count": n})
    return 0


def cmd_get(con, args) -> int:
    check_key(args.key)
    con.execute("BEGIN")  # 只读事务:两次查询处于同一读快照
    try:
        bv = require_active(con, args.txn)
        row = con.execute(
            "SELECT value, tombstone FROM writes WHERE txn_id=? AND key=?",
            (args.txn, args.key)).fetchone()
        src = "private"
        if row is None:
            row = con.execute(
                "SELECT value, tombstone FROM kv_versions WHERE key=? AND version<=? "
                "ORDER BY version DESC LIMIT 1", (args.key, bv)).fetchone()
            src = "committed"
        con.execute("COMMIT")
    except Exception:
        rollback_safely(con)
        raise
    if row is None or row[1] == 1:
        emit({"status": "not_found", "txn": args.txn, "key": args.key,
              "begin_version": bv})
    else:
        emit({"status": "found", "txn": args.txn, "key": args.key, "value": row[0],
              "begin_version": bv, "source": src})
    return 0


def cmd_scan(con, args) -> int:
    # 区间按 UTF-8 字节序:SQLite TEXT 的 BINARY 排序即 UTF-8 字节序;
    # 合并私有写后在 Python 侧再按 key.encode('utf-8') 排一次,语义一致。
    con.execute("BEGIN")
    try:
        bv = require_active(con, args.txn)
        cond, params = ["v.version <= ?"], [bv]
        if args.start is not None:
            cond.append("v.key >= ?")
            params.append(args.start)
        if args.end is not None:
            cond.append("v.key < ?")
            params.append(args.end)
        rows = con.execute(
            f"SELECT v.key, v.version, v.value, v.tombstone FROM kv_versions v "
            f"WHERE {' AND '.join(cond)} ORDER BY v.key, v.version", params).fetchall()
        visible = {}
        for k, _ver, val, ts in rows:  # 同键按版本升序,后者覆盖前者 → 留下 <=begin 的最新
            visible[k] = (val, ts)
        wcond, wparams = ["txn_id = ?"], [args.txn]
        if args.start is not None:
            wcond.append("key >= ?")
            wparams.append(args.start)
        if args.end is not None:
            wcond.append("key < ?")
            wparams.append(args.end)
        for k, val, ts in con.execute(
                f"SELECT key, value, tombstone FROM writes WHERE {' AND '.join(wcond)}",
                wparams):
            visible[k] = (val, ts)  # 本事务私有写覆盖已提交视图
        con.execute("COMMIT")
    except Exception:
        rollback_safely(con)
        raise
    items = [{"key": k, "value": val} for k, (val, ts) in visible.items() if ts == 0]
    items.sort(key=lambda it: it["key"].encode("utf-8"))
    if args.limit is not None:
        items = items[: args.limit]
    emit({"status": "ok", "txn": args.txn, "begin_version": bv,
          "count": len(items), "items": items})
    return 0


def cmd_commit(con, args) -> int:
    txn = args.txn
    con.execute("BEGIN IMMEDIATE")
    try:
        row = load_txn(con, txn)
        if row is None:
            raise CliError(f"事务不存在: {txn!r}(请先 begin)")
        bv, state, _cv, receipt = row
        if state != "active":
            # 幂等:重复提交(或对已中止事务提交)一律返回原终态回执,
            # 绝不产生第二个提交版本。
            rec = json.loads(receipt)
            con.execute("COMMIT")
            emit(rec)
            return 0
        ws = con.execute(
            "SELECT key, value, tombstone FROM writes WHERE txn_id=? ORDER BY key",
            (txn,)).fetchall()
        if ws:
            # 快照隔离首次提交者胜:写集中任一键在 begin_version 之后出现
            # 新的已提交版本(含墓碑)即整笔冲突。
            conflict = [k for (k, _v, _t) in ws if con.execute(
                "SELECT 1 FROM kv_versions WHERE key=? AND version>? LIMIT 1",
                (k, bv)).fetchone()]
            if conflict:
                rec = {"status": "conflicted", "txn": txn, "begin_version": bv,
                       "conflict_keys": sorted(conflict),
                       "note": "快照隔离写冲突:整笔写事务失败,私有修改均未发布"}
                finalize_txn(con, txn, "conflicted", None, rec)
                con.execute("COMMIT")
                emit(rec)
                return 0
            ver = con.execute("SELECT next_version FROM meta WHERE id=1").fetchone()[0]
            con.execute("UPDATE meta SET next_version = next_version + 1 WHERE id=1")
            failpoint("commit:after_version_alloc")
            for k, v, t in ws:
                con.execute(
                    "INSERT INTO kv_versions(key, version, value, tombstone, txn_id) "
                    "VALUES (?,?,?,?,?)", (k, ver, v, t, txn))
            failpoint("commit:before_finalize")  # 在此崩溃 → SQLite 回滚,不落任何版本
            rec = {"status": "committed", "txn": txn, "begin_version": bv,
                   "commit_version": ver, "read_only": False,
                   "keys": [k for (k, _v, _t) in ws]}
            finalize_txn(con, txn, "committed", ver, rec)
        else:
            # 只读事务:显式结束,不消耗提交版本号。
            rec = {"status": "committed", "txn": txn, "begin_version": bv,
                   "commit_version": None, "read_only": True, "keys": []}
            finalize_txn(con, txn, "committed", None, rec)
        con.execute("COMMIT")
    except Exception:
        rollback_safely(con)
        raise
    failpoint("commit:after_commit")  # 库已提交、回执未及打印;重试返回同一回执
    emit(rec)
    return 0


def cmd_abort(con, args) -> int:
    txn = args.txn
    con.execute("BEGIN IMMEDIATE")
    try:
        row = load_txn(con, txn)
        if row is None:
            raise CliError(f"事务不存在: {txn!r}(请先 begin)")
        bv, state, _cv, receipt = row
        if state != "active":
            rec = json.loads(receipt)  # 幂等:返回原终态回执
            con.execute("COMMIT")
            emit(rec)
            return 0
        rec = {"status": "aborted", "txn": txn, "begin_version": bv}
        finalize_txn(con, txn, "aborted", None, rec)
        con.execute("COMMIT")
    except Exception:
        rollback_safely(con)
        raise
    emit(rec)
    return 0


def cmd_gc(con, args) -> int:
    # BEGIN IMMEDIATE:与 begin/commit 互斥。要么看到新登记的快照并为其保留版本,
    # 要么先于新快照完成——而新快照只读各键最新版本,gc 永远保留最新版本,故安全。
    con.execute("BEGIN IMMEDIATE")
    try:
        snaps = con.execute(
            "SELECT txn_id, begin_version FROM txns WHERE state='active' "
            "ORDER BY begin_version, txn_id").fetchall()
        failpoint("gc:after_snapshot_read")
        rows = con.execute(
            "SELECT key, version FROM kv_versions ORDER BY key, version").fetchall()
        by_key: dict[str, list[int]] = {}
        for k, v in rows:
            by_key.setdefault(k, []).append(v)
        keep: dict[tuple[str, int], set[str]] = {}

        def mark(k, v, reason):
            keep.setdefault((k, v), set()).add(reason)

        for k, vers in by_key.items():
            mark(k, vers[-1], "latest(每键最新版本必须保留)")
            for txn_id, b in snaps:
                i = bisect.bisect_right(vers, b) - 1
                if i >= 0:  # 该快照对此键实际会读到的版本(可能是墓碑)
                    mark(k, vers[i], f"snapshot:{txn_id}@begin={b}")
        deleted = [(k, v) for k, vers in by_key.items() for v in vers
                   if (k, v) not in keep]
        for k, v in deleted:
            con.execute("DELETE FROM kv_versions WHERE key=? AND version=?", (k, v))
        failpoint("gc:after_delete")  # 在此崩溃 → 整个 gc 回滚,无半清理状态
        con.execute("COMMIT")
    except Exception:
        rollback_safely(con)
        raise
    result = {
        "status": "gc_done",
        "active_snapshots": [{"txn": t, "begin_version": b} for t, b in snaps],
        "deleted_count": len(deleted),
        "kept_count": len(keep),
    }
    if not args.summary:
        result["kept"] = [
            {"key": k, "version": v, "reasons": sorted(rs)}
            for (k, v), rs in sorted(keep.items(),
                                     key=lambda x: (x[0][0].encode("utf-8"), x[0][1]))]
        result["deleted"] = [{"key": k, "version": v} for k, v in deleted]
    emit(result)
    return 0


# ---------------------------------------------------------------------------
# verify:独立验证器
#   从 oracle(JSONL,由测试/演示驱动在回收前记录的完整提交历史)重建各键全部
#   已提交版本,计算每个活跃快照应见值,再用 CLI 的 scan/get 实际比对;并检查
#   gc 不变量(活跃快照需要的版本必须还在;--strict-gc 时不许多余版本)、
#   事务终态与幂等回执、提交版本序列。
# ---------------------------------------------------------------------------
def run_cli(db_path, *argv):
    p = subprocess.run(
        [sys.executable, os.path.abspath(__file__), "--db", db_path, *argv],
        capture_output=True, text=True)
    if p.returncode != 0:
        raise CliError(f"子命令失败 {argv}: rc={p.returncode} out={p.stdout} err={p.stderr}")
    return json.loads(p.stdout)


def cmd_verify(con, args) -> int:
    events = []
    with open(args.oracle, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    # 1) 由完整提交历史重建模型(含已被 gc 删除的旧版本)
    history: dict[str, tuple[list[int], list]] = {}  # key -> (versions, values|None)
    final_state: dict[str, tuple[str, object]] = {}
    private: dict[str, dict[str, object]] = {}
    for ev in events:
        t = ev.get("type")
        if t == "put":
            private.setdefault(ev["txn"], {})[ev["key"]] = ev["value"]
        elif t == "delete":
            private.setdefault(ev["txn"], {})[ev["key"]] = None
        elif t == "commit":
            if ev["status"] == "committed":
                cv = ev.get("commit_version")
                final_state[ev["txn"]] = ("committed", cv)
                if cv is not None:
                    for k, v in ev.get("writes", {}).items():
                        vers, vals = history.setdefault(k, ([], []))
                        vers.append(cv)
                        vals.append(v)
            else:
                final_state[ev["txn"]] = (ev["status"], None)
        elif t == "abort":
            final_state[ev["txn"]] = ("aborted", None)
    problems = []
    active = con.execute(
        "SELECT txn_id, begin_version FROM txns WHERE state='active' "
        "ORDER BY txn_id").fetchall()
    rng = random.Random(20261113)
    # 2) 每个活跃快照:期望可见 map 与 CLI scan 全量比对 + 抽样 get
    for txn_id, b in active:
        expected = {}
        for k, (vers, vals) in history.items():
            i = bisect.bisect_right(vers, b) - 1
            if i >= 0 and vals[i] is not None:
                expected[k] = vals[i]
        for k, v in private.get(txn_id, {}).items():
            if v is None:
                expected.pop(k, None)
            else:
                expected[k] = v
        got = run_cli(args.db, "scan", f"--txn={txn_id}")
        actual = {it["key"]: it["value"] for it in got["items"]}
        if actual != expected:
            diff = {k for k in expected.keys() | actual.keys()
                    if expected.get(k) != actual.get(k)}
            problems.append({"check": "scan", "txn": txn_id, "begin_version": b,
                             "diff_keys": sorted(diff)[:20], "diff_count": len(diff)})
        cand = list(history.keys())
        rng.shuffle(cand)
        tomb_latest = [k for k, (vs, vl) in history.items() if vl[-1] is None]
        sample_keys = (cand[: args.sample] + tomb_latest[: args.sample]
                       + list(private.get(txn_id, {}).keys())[: args.sample]
                       + ["__no_such_key_0__", "__no_such_key_1__"])
        for k in sample_keys:
            r = run_cli(args.db, "get", f"--txn={txn_id}", f"--key={k}")
            if k in expected:
                if r["status"] != "found" or r["value"] != expected[k]:
                    problems.append({"check": "get", "txn": txn_id, "key": k,
                                     "expected": expected[k], "got": r})
            else:
                if r["status"] != "not_found":
                    problems.append({"check": "get", "txn": txn_id, "key": k,
                                     "expected": "not_found", "got": r})
    # 3) gc 不变量:按完整历史,活跃快照需要的版本与每键最新版本必须仍然存在
    snaps = [b for _, b in active]
    existing: dict[str, set[int]] = {}
    for k, v in con.execute("SELECT key, version FROM kv_versions"):
        existing.setdefault(k, set()).add(v)
    for k, (vers, vals) in history.items():
        needed = {vers[-1]}
        for b in snaps:
            i = bisect.bisect_right(vers, b) - 1
            if i >= 0:
                needed.add(vers[i])
        have = existing.get(k, set())
        missing = needed - have
        if missing:
            problems.append({"check": "gc_missing", "key": k,
                             "missing_versions": sorted(missing)})
        if args.strict_gc:
            extra = have - needed
            if extra:
                problems.append({"check": "gc_extra", "key": k,
                                 "extra_versions": sorted(extra)})
    # 4) 事务终态与幂等回执:不随业务版本回收而丢失,重复提交返回原结果
    for txn, (st, cv) in final_state.items():
        row = con.execute(
            "SELECT state, commit_version FROM txns WHERE txn_id=?", (txn,)).fetchone()
        if row is None:
            problems.append({"check": "receipt", "txn": txn, "error": "事务终态丢失"})
            continue
        if row[0] != st or (cv is not None and row[1] != cv):
            problems.append({"check": "receipt", "txn": txn,
                             "expected": [st, cv], "got": list(row)})
        r = run_cli(args.db, "commit", f"--txn={txn}")  # 幂等重放
        if r["status"] != st or (cv is not None and r.get("commit_version") != cv):
            problems.append({"check": "receipt_replay", "txn": txn,
                             "expected": [st, cv], "got": r})
    # 5) 提交版本序列:写事务版本唯一且最大版本 == next_version - 1
    row = con.execute(
        "SELECT COUNT(*), COUNT(DISTINCT commit_version), COALESCE(MAX(commit_version),0) "
        "FROM txns WHERE state='committed' AND commit_version IS NOT NULL").fetchone()
    nv = con.execute("SELECT next_version FROM meta WHERE id=1").fetchone()[0]
    if row[0] != row[1] or row[2] != nv - 1:
        problems.append({"check": "version_sequence",
                         "committed_writes": row[0], "distinct_versions": row[1],
                         "max_version": row[2], "next_version": nv})
    ok = not problems
    emit({"status": "ok" if ok else "failed",
          "active_snapshots": len(active),
          "keys_in_history": len(history),
          "checked_receipts": len(final_state),
          "problem_count": len(problems),
          "problems": problems[:50]})
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mvcc.py", description="小型多版本键值 CLI(快照隔离,Python 3.11 + SQLite)")
    p.add_argument("--db", default=os.environ.get("MVCC_DB", "./mvcc.db"),
                   help="SQLite 数据库路径(默认 ./mvcc.db,也可用环境变量 MVCC_DB)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="初始化数据库(幂等)")

    b = sub.add_parser("begin", help="开启事务(重复 begin 返回原事务身份)")
    b.add_argument("--txn", required=True)

    pu = sub.add_parser("put", help="事务内写键值(值必须非空)")
    pu.add_argument("--txn", required=True)
    pu.add_argument("--key")
    pu.add_argument("--value")
    pu.add_argument("--file", help="批量写入:JSONL 文件,每行 {\"key\":..,\"value\":..}")

    de = sub.add_parser("delete", help="事务内删除键(写独立墓碑)")
    de.add_argument("--txn", required=True)
    de.add_argument("--key")
    de.add_argument("--file", help="批量删除:JSONL 文件,每行 {\"key\":..}")

    ge = sub.add_parser("get", help="事务内读单键")
    ge.add_argument("--txn", required=True)
    ge.add_argument("--key", required=True)

    sc = sub.add_parser("scan", help="事务内范围扫描(UTF-8 字节序,start 含/end 不含)")
    sc.add_argument("--txn", required=True)
    sc.add_argument("--start")
    sc.add_argument("--end")
    sc.add_argument("--limit", type=int)

    co = sub.add_parser("commit", help="提交事务(幂等,重复提交返回原回执)")
    co.add_argument("--txn", required=True)

    ab = sub.add_parser("abort", help="中止事务(幂等)")
    ab.add_argument("--txn", required=True)

    gc = sub.add_parser("gc", help="回收不再需要的已提交旧版本")
    gc.add_argument("--summary", action="store_true",
                    help="只输出计数,不逐条列保留/删除明细")

    ve = sub.add_parser("verify", help="独立验证器:按完整提交历史校验快照可见性与 gc")
    ve.add_argument("--oracle", required=True,
                    help="驱动侧记录的完整历史 JSONL(回收前)")
    ve.add_argument("--strict-gc", action="store_true",
                    help="严格模式:不允许保留任何不再需要的版本")
    ve.add_argument("--sample", type=int, default=20, help="每快照抽样 get 的键数")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {
        "init": cmd_init, "begin": cmd_begin, "put": cmd_put, "delete": cmd_delete,
        "get": cmd_get, "scan": cmd_scan, "commit": cmd_commit, "abort": cmd_abort,
        "gc": cmd_gc, "verify": cmd_verify,
    }
    try:
        if args.cmd != "init" and not os.path.exists(args.db):
            raise CliError(f"数据库不存在: {args.db}(请先 init)")
        con = connect(args.db)
        try:
            if args.cmd != "init":
                ensure_initialized(con)
            return handlers[args.cmd](con, args)
        finally:
            con.close()
    except CliError as e:
        emit({"status": "error", "error": str(e)})
        return 2
    except sqlite3.Error as e:
        emit({"status": "error", "error": f"sqlite: {e}"})
        return 1


if __name__ == "__main__":
    sys.exit(main())
