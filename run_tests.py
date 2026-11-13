#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_tests.py — MVCC CLI 语义测试套件。

每个测试在独立临时目录中运行;所有 CLI 调用都是独立子进程,天然覆盖
“事务跨多次 CLI 调用存活”。运行:  python3 run_tests.py
"""
from __future__ import annotations

import json
import os
import random
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
MVCC = os.path.join(HERE, "mvcc.py")

TESTS = []
FAILURES = 0


def test(fn):
    TESTS.append(fn)
    return fn


def run_cli(db, *argv, env=None, check=True):
    e = dict(os.environ)
    e.update(env or {})
    p = subprocess.run([sys.executable, MVCC, "--db", db, *argv],
                       capture_output=True, text=True, env=e)
    if check and p.returncode != 0:
        raise AssertionError(
            f"cli {argv} rc={p.returncode}\nstdout={p.stdout}\nstderr={p.stderr}")
    return p


def jout(p):
    return json.loads(p.stdout)


def jcli(db, *argv, **kw):
    return jout(run_cli(db, *argv, **kw))


def init_db(d):
    db = os.path.join(d, "test.db")
    jcli(db, "init")
    return db


def sql(db, q, *params):
    con = sqlite3.connect(db)
    try:
        return con.execute(q, params).fetchall()
    finally:
        con.close()


class Driver:
    """测试驱动:执行 CLI 的同时把完整历史写入 oracle(回收前的提交历史)。"""

    def __init__(self, db, oracle_path):
        self.db = db
        self.fo = open(oracle_path, "a", encoding="utf-8")
        self.ws = {}

    def log(self, ev):
        self.fo.write(json.dumps(ev, ensure_ascii=False) + "\n")
        self.fo.flush()

    def begin(self, txn):
        r = jcli(self.db, "begin", f"--txn={txn}")
        if r["status"] == "begun":
            self.log({"type": "begin", "txn": txn, "begin_version": r["begin_version"]})
            self.ws[txn] = {}
        return r

    def put(self, txn, k, v):
        jcli(self.db, "put", f"--txn={txn}", f"--key={k}", f"--value={v}")
        self.ws[txn][k] = v
        self.log({"type": "put", "txn": txn, "key": k, "value": v})

    def put_file(self, txn, path):
        jcli(self.db, "put", f"--txn={txn}", f"--file={path}")
        with open(path, encoding="utf-8") as f:
            for line in f:
                o = json.loads(line)
                self.ws[txn][o["key"]] = o["value"]
                self.log({"type": "put", "txn": txn, "key": o["key"], "value": o["value"]})

    def delete(self, txn, k):
        jcli(self.db, "delete", f"--txn={txn}", f"--key={k}")
        self.ws[txn][k] = None
        self.log({"type": "delete", "txn": txn, "key": k})

    def delete_file(self, txn, path):
        jcli(self.db, "delete", f"--txn={txn}", f"--file={path}")
        with open(path, encoding="utf-8") as f:
            for line in f:
                o = json.loads(line)
                self.ws[txn][o["key"]] = None
                self.log({"type": "delete", "txn": txn, "key": o["key"]})

    def commit(self, txn):
        r = jcli(self.db, "commit", f"--txn={txn}")
        ev = {"type": "commit", "txn": txn, "status": r["status"]}
        if r["status"] == "committed":
            ev["commit_version"] = r["commit_version"]
            if not r["read_only"]:
                ev["writes"] = dict(self.ws.get(txn, {}))
        self.log(ev)
        self.ws.pop(txn, None)
        return r

    def abort(self, txn):
        r = jcli(self.db, "abort", f"--txn={txn}")
        self.log({"type": "abort", "txn": txn})
        self.ws.pop(txn, None)
        return r

    def close(self):
        self.fo.close()


def verify_ok(db, oracle, strict=True):
    argv = ["verify", f"--oracle={oracle}"]
    if strict:
        argv.append("--strict-gc")
    p = run_cli(db, *argv, check=False)
    r = json.loads(p.stdout)
    assert p.returncode == 0 and r["status"] == "ok", \
        f"verify 失败: {json.dumps(r, ensure_ascii=False)[:2000]}"
    return r


# ---------------------------------------------------------------------------
# 基础与持久化
# ---------------------------------------------------------------------------
@test
def test_basic_cross_process(d):
    """每个命令都是独立进程:begin/put/commit/get 跨 CLI 调用存活。"""
    db = init_db(d)
    assert jcli(db, "begin", "--txn=T")["status"] == "begun"
    jcli(db, "put", "--txn=T", "--key=hello", "--value=世界")
    c = jcli(db, "commit", "--txn=T")
    assert c["status"] == "committed" and c["commit_version"] == 1
    jcli(db, "begin", "--txn=R")
    g = jcli(db, "get", "--txn=R", "--key=hello")
    assert g["status"] == "found" and g["value"] == "世界"
    sc = jcli(db, "scan", "--txn=R")
    assert [i["key"] for i in sc["items"]] == ["hello"]
    jcli(db, "commit", "--txn=R")


@test
def test_readonly_commit_no_version_consumed(d):
    """只读事务显式结束,不消耗提交版本号。"""
    db = init_db(d)
    jcli(db, "begin", "--txn=R1")
    c = jcli(db, "commit", "--txn=R1")
    assert c["status"] == "committed" and c["read_only"] and c["commit_version"] is None
    jcli(db, "begin", "--txn=W")
    jcli(db, "put", "--txn=W", "--key=k", "--value=v")
    c = jcli(db, "commit", "--txn=W")
    assert c["commit_version"] == 1, "只读事务不得消耗版本号"
    jcli(db, "begin", "--txn=R2")
    c = jcli(db, "abort", "--txn=R2")
    assert c["status"] == "aborted"


# ---------------------------------------------------------------------------
# 快照隔离语义
# ---------------------------------------------------------------------------
@test
def test_snapshot_isolation(d):
    db = init_db(d)
    jcli(db, "begin", "--txn=seed")
    jcli(db, "put", "--txn=seed", "--key=k", "--value=old")
    jcli(db, "put", "--txn=seed", "--key=x", "--value=1")
    jcli(db, "commit", "--txn=seed")
    assert jcli(db, "begin", "--txn=T1")["begin_version"] == 1
    jcli(db, "begin", "--txn=T2")
    jcli(db, "put", "--txn=T2", "--key=k", "--value=new")
    jcli(db, "commit", "--txn=T2")
    g = jcli(db, "get", "--txn=T1", "--key=k")
    assert g["value"] == "old", "T1 必须读到开始时的已提交值"
    sc = jcli(db, "scan", "--txn=T1")
    assert {i["key"]: i["value"] for i in sc["items"]} == {"k": "old", "x": "1"}
    # T2 未提交的修改对其他事务不可见
    jcli(db, "begin", "--txn=T3")
    jcli(db, "put", "--txn=T3", "--key=x", "--value=dirty")
    g = jcli(db, "get", "--txn=T1", "--key=x")
    assert g["value"] == "1"
    jcli(db, "abort", "--txn=T3")
    jcli(db, "commit", "--txn=T1")
    jcli(db, "begin", "--txn=T4")
    assert jcli(db, "get", "--txn=T4", "--key=k")["value"] == "new"
    jcli(db, "commit", "--txn=T4")


@test
def test_read_own_writes(d):
    """读自身修改:put/delete 立即可见,scan 同步反映。"""
    db = init_db(d)
    jcli(db, "begin", "--txn=seed")
    jcli(db, "put", "--txn=seed", "--key=a", "--value=1")
    jcli(db, "put", "--txn=seed", "--key=b", "--value=2")
    jcli(db, "commit", "--txn=seed")
    jcli(db, "begin", "--txn=T")
    jcli(db, "put", "--txn=T", "--key=a", "--value=10")
    g = jcli(db, "get", "--txn=T", "--key=a")
    assert g["value"] == "10" and g["source"] == "private"
    jcli(db, "delete", "--txn=T", "--key=b")
    assert jcli(db, "get", "--txn=T", "--key=b")["status"] == "not_found"
    jcli(db, "put", "--txn=T", "--key=c", "--value=30")
    sc = jcli(db, "scan", "--txn=T")
    assert {i["key"]: i["value"] for i in sc["items"]} == {"a": "10", "c": "30"}
    c = jcli(db, "commit", "--txn=T")
    assert c["commit_version"] == 2 and c["keys"] == ["a", "b", "c"]
    jcli(db, "begin", "--txn=V")
    sc = jcli(db, "scan", "--txn=V")
    assert {i["key"]: i["value"] for i in sc["items"]} == {"a": "10", "c": "30"}
    jcli(db, "commit", "--txn=V")


@test
def test_write_conflict_full_rollback(d):
    """双写冲突 → 整笔写事务失败,多键全部回滚;重复提交返回同一冲突回执。"""
    db = init_db(d)
    jcli(db, "begin", "--txn=seed")
    jcli(db, "put", "--txn=seed", "--key=ka", "--value=0")
    jcli(db, "put", "--txn=seed", "--key=kb", "--value=0")
    jcli(db, "commit", "--txn=seed")
    jcli(db, "begin", "--txn=T1")
    jcli(db, "begin", "--txn=T2")
    jcli(db, "put", "--txn=T1", "--key=ka", "--value=1")
    jcli(db, "put", "--txn=T1", "--key=kb", "--value=1")
    jcli(db, "put", "--txn=T2", "--key=kb", "--value=2")
    c2 = jcli(db, "commit", "--txn=T2")
    assert c2["status"] == "committed" and c2["commit_version"] == 2
    c1 = jcli(db, "commit", "--txn=T1")
    assert c1["status"] == "conflicted" and c1["conflict_keys"] == ["kb"]
    # 多键全回滚:T1 对 ka 的修改也不得发布
    jcli(db, "begin", "--txn=V")
    assert jcli(db, "get", "--txn=V", "--key=ka")["value"] == "0"
    assert jcli(db, "get", "--txn=V", "--key=kb")["value"] == "2"
    jcli(db, "commit", "--txn=V")
    # 幂等:重复提交返回原冲突回执
    assert jcli(db, "commit", "--txn=T1") == c1
    # 冲突事务不占用版本号
    jcli(db, "begin", "--txn=T3")
    jcli(db, "put", "--txn=T3", "--key=kz", "--value=9")
    assert jcli(db, "commit", "--txn=T3")["commit_version"] == 3


@test
def test_independent_keys_both_commit(d):
    """写不同键可以成功(不额外承诺消除跨键写偏差)。"""
    db = init_db(d)
    jcli(db, "begin", "--txn=A")
    jcli(db, "begin", "--txn=B")
    jcli(db, "put", "--txn=A", "--key=a", "--value=1")
    jcli(db, "put", "--txn=B", "--key=b", "--value=2")
    ca = jcli(db, "commit", "--txn=A")
    cb = jcli(db, "commit", "--txn=B")
    assert ca["status"] == cb["status"] == "committed"
    assert ca["commit_version"] == 1 and cb["commit_version"] == 2


@test
def test_idempotent_begin_commit_abort(d):
    db = init_db(d)
    b1 = jcli(db, "begin", "--txn=T")
    b2 = jcli(db, "begin", "--txn=T")
    assert b1["begin_version"] == b2["begin_version"] and b2["status"] == "exists"
    jcli(db, "put", "--txn=T", "--key=k", "--value=v")
    c1 = jcli(db, "commit", "--txn=T")
    c2 = jcli(db, "commit", "--txn=T")
    assert c1 == c2 and c1["commit_version"] == 1, "重复提交必须返回原回执"
    # 已提交事务再 abort:返回原终态回执,不改变状态
    assert jcli(db, "abort", "--txn=T")["status"] == "committed"
    # 已提交事务再 begin:返回原身份,不新建快照
    b3 = jcli(db, "begin", "--txn=T")
    assert b3["status"] == "exists" and b3["state"] == "committed"
    # abort 幂等;已中止事务再 commit 返回中止回执
    jcli(db, "begin", "--txn=A")
    a1 = jcli(db, "abort", "--txn=A")
    a2 = jcli(db, "abort", "--txn=A")
    assert a1 == a2 and a1["status"] == "aborted"
    assert jcli(db, "commit", "--txn=A")["status"] == "aborted"
    assert sql(db, "SELECT COUNT(*) FROM kv_versions") == [(1,)]


# ---------------------------------------------------------------------------
# 墓碑与 GC
# ---------------------------------------------------------------------------
@test
def test_tombstone_gc_no_resurrect(d):
    """墓碑参与可见性:清理后不得暴露更老值使已删除键复活。"""
    db = init_db(d)
    oracle = os.path.join(d, "oracle.jsonl")
    drv = Driver(db, oracle)
    drv.begin("seed")
    drv.put("seed", "k", "v1")
    drv.commit("seed")                       # k@v1
    drv.begin("T1")                          # 快照 @1
    drv.begin("D")
    drv.delete("D", "k")
    drv.commit("D")                          # k@v2 墓碑
    g1 = jcli(db, "gc")
    kept = {(e["key"], e["version"]): e["reasons"] for e in g1["kept"]}
    assert ("k", 1) in kept and any("snapshot:T1" in r for r in kept[("k", 1)])
    assert ("k", 2) in kept and any("latest" in r for r in kept[("k", 2)])
    assert g1["deleted_count"] == 0
    assert jcli(db, "get", "--txn=T1", "--key=k")["value"] == "v1"
    drv.commit("T1")                         # 只读提交
    g2 = jcli(db, "gc")
    assert {e["version"] for e in g2["deleted"] if e["key"] == "k"} == {1}
    drv.begin("N")
    assert jcli(db, "get", "--txn=N", "--key=k")["status"] == "not_found"
    assert all(i["key"] != "k" for i in jcli(db, "scan", "--txn=N")["items"])
    drv.commit("N")
    drv.close()
    verify_ok(db, oracle)


@test
def test_gc_release_after_reader_ends(d):
    """长读固定旧版本;旧读结束后 gc 释放原先被固定的版本。"""
    db = init_db(d)
    oracle = os.path.join(d, "oracle.jsonl")
    drv = Driver(db, oracle)
    drv.begin("seed")
    drv.put("seed", "alpha", "v0")
    drv.commit("seed")                       # @1
    drv.begin("T1")                          # 长读 @1
    drv.begin("w2")
    drv.put("w2", "alpha", "v1")
    drv.commit("w2")                         # @2 覆盖
    drv.begin("w3")
    drv.delete("w3", "alpha")
    drv.commit("w3")                         # @3 删除(墓碑)
    drv.begin("w4")
    drv.put("w4", "alpha", "v2")
    drv.commit("w4")                         # @4 重新插入
    g1 = jcli(db, "gc")
    kept = {(e["key"], e["version"]): e["reasons"] for e in g1["kept"]}
    assert any("snapshot:T1" in r for r in kept[("alpha", 1)])
    assert any("latest" in r for r in kept[("alpha", 4)])
    assert {e["version"] for e in g1["deleted"] if e["key"] == "alpha"} == {2, 3}
    # 长读事务在整个过程中始终看到 v0
    assert jcli(db, "get", "--txn=T1", "--key=alpha")["value"] == "v0"
    drv.commit("T1")
    g2 = jcli(db, "gc")
    assert {e["version"] for e in g2["deleted"] if e["key"] == "alpha"} == {1}
    kept2 = {(e["key"], e["version"]) for e in g2["kept"]}
    assert ("alpha", 4) in kept2 and ("alpha", 1) not in kept2
    drv.begin("N")
    assert jcli(db, "get", "--txn=N", "--key=alpha")["value"] == "v2"
    drv.commit("N")
    drv.close()
    verify_ok(db, oracle)


@test
def test_gc_crash_is_atomic(d):
    """gc 中途崩溃:整个 gc 回滚,不产生半清理状态。"""
    db = init_db(d)
    jcli(db, "begin", "--txn=a")
    jcli(db, "put", "--txn=a", "--key=k", "--value=v1")
    jcli(db, "commit", "--txn=a")
    jcli(db, "begin", "--txn=b")
    jcli(db, "put", "--txn=b", "--key=k", "--value=v2")
    jcli(db, "commit", "--txn=b")
    before = sql(db, "SELECT COUNT(*) FROM kv_versions")[0][0]
    assert before == 2
    p = run_cli(db, "gc", env={"MVCC_FAILPOINT": "gc:after_delete"}, check=False)
    assert p.returncode == 118
    assert sql(db, "SELECT COUNT(*) FROM kv_versions")[0][0] == before
    g = jcli(db, "gc")
    assert g["deleted_count"] == 1
    assert sql(db, "SELECT COUNT(*) FROM kv_versions")[0][0] == 1


# ---------------------------------------------------------------------------
# 进程崩溃(提交前后终止)
# ---------------------------------------------------------------------------
@test
def test_crash_before_finalize(d):
    """提交事务在落终态前崩溃:SQLite 回滚,不发布任何版本,可干净重提。"""
    db = init_db(d)
    jcli(db, "begin", "--txn=T")
    jcli(db, "put", "--txn=T", "--key=k", "--value=v")
    p = run_cli(db, "commit", "--txn=T",
                env={"MVCC_FAILPOINT": "commit:before_finalize"}, check=False)
    assert p.returncode == 118
    jcli(db, "begin", "--txn=R")
    assert jcli(db, "get", "--txn=R", "--key=k")["status"] == "not_found"
    jcli(db, "commit", "--txn=R")
    c1 = jcli(db, "commit", "--txn=T")
    assert c1["status"] == "committed" and c1["commit_version"] == 1
    assert jcli(db, "commit", "--txn=T") == c1
    assert sql(db, "SELECT COUNT(*) FROM kv_versions WHERE key='k'") == [(1,)]


@test
def test_crash_after_commit(d):
    """库已提交、回执未及打印即崩溃:重提返回同一回执,不产生第二版本。"""
    db = init_db(d)
    jcli(db, "begin", "--txn=T")
    jcli(db, "put", "--txn=T", "--key=k", "--value=v")
    p = run_cli(db, "commit", "--txn=T",
                env={"MVCC_FAILPOINT": "commit:after_commit"}, check=False)
    assert p.returncode == 118
    c = jcli(db, "commit", "--txn=T")
    assert c["status"] == "committed" and c["commit_version"] == 1
    assert jcli(db, "commit", "--txn=T") == c
    assert sql(db, "SELECT COUNT(*) FROM kv_versions") == [(1,)]
    assert sql(db, "SELECT next_version FROM meta") == [(2,)]


@test
def test_kill9_during_and_after_commit(d):
    """真实 kill -9:提交事务中途被杀死 → 回滚可重提;提交后被杀 → 回执仍在。"""
    db = init_db(d)
    jcli(db, "begin", "--txn=T1")
    jcli(db, "put", "--txn=T1", "--key=k", "--value=v1")
    env = dict(os.environ, MVCC_SLEEP="commit:before_finalize=3000")
    p = subprocess.Popen([sys.executable, MVCC, "--db", db, "commit", "--txn=T1"],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, env=env)
    time.sleep(1.0)
    p.send_signal(signal.SIGKILL)
    p.wait()
    assert p.returncode == -signal.SIGKILL
    assert sql(db, "SELECT state FROM txns WHERE txn_id='T1'") == [("active",)]
    c = jcli(db, "commit", "--txn=T1")
    assert c["status"] == "committed" and c["commit_version"] == 1
    # 提交完成后(回执打印前)被杀
    jcli(db, "begin", "--txn=T2")
    jcli(db, "put", "--txn=T2", "--key=k", "--value=v2")
    env2 = dict(os.environ, MVCC_SLEEP="commit:after_commit=3000")
    p2 = subprocess.Popen([sys.executable, MVCC, "--db", db, "commit", "--txn=T2"],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, env=env2)
    time.sleep(1.0)
    p2.send_signal(signal.SIGKILL)
    p2.wait()
    assert p2.returncode == -signal.SIGKILL
    c2 = jcli(db, "commit", "--txn=T2")
    assert c2["status"] == "committed" and c2["commit_version"] == 2
    assert jcli(db, "commit", "--txn=T2") == c2
    assert sql(db, "SELECT COUNT(*) FROM kv_versions WHERE key='k'") == [(2,)]


# ---------------------------------------------------------------------------
# 输入校验
# ---------------------------------------------------------------------------
@test
def test_scan_utf8_byte_order_and_range(d):
    """scan 按 UTF-8 字节序;start 含 / end 不含;limit 生效。"""
    db = init_db(d)
    keys = ["中", "a", "😀", "é", "ab", "€", "z"]
    jcli(db, "begin", "--txn=T")
    for i, k in enumerate(keys):
        jcli(db, "put", "--txn=T", f"--key={k}", f"--value=v{i}")
    jcli(db, "commit", "--txn=T")
    jcli(db, "begin", "--txn=R")
    sc = jcli(db, "scan", "--txn=R")
    got = [i["key"] for i in sc["items"]]
    want = sorted(keys, key=lambda k: k.encode("utf-8"))
    assert got == want, f"{got} != {want}"
    sc = jcli(db, "scan", "--txn=R", "--start=ab", "--end=中")
    got = [i["key"] for i in sc["items"]]
    assert got == [k for k in want if "ab" <= k < "中"], got
    assert len(jcli(db, "scan", "--txn=R", "--limit=2")["items"]) == 2
    jcli(db, "commit", "--txn=R")


@test
def test_validation_and_state_errors(d):
    db = init_db(d)
    jcli(db, "begin", "--txn=T")
    assert run_cli(db, "put", "--txn=T", "--key=", "--value=v", check=False).returncode == 2
    p = run_cli(db, "put", "--txn=T", "--key=k", "--value=", check=False)
    assert p.returncode == 2 and "空串" in p.stdout
    assert run_cli(db, "delete", "--txn=T", "--key=", check=False).returncode == 2
    assert run_cli(db, "get", "--txn=NOPE", "--key=k", check=False).returncode == 2
    assert run_cli(db, "commit", "--txn=NOPE", check=False).returncode == 2
    jcli(db, "commit", "--txn=T")
    # 事务已结束:读写都报错
    assert run_cli(db, "put", "--txn=T", "--key=k", "--value=v", check=False).returncode == 2
    assert run_cli(db, "get", "--txn=T", "--key=k", check=False).returncode == 2


# ---------------------------------------------------------------------------
# 并发:gc 与 begin/commit 同时进行
# ---------------------------------------------------------------------------
@test
def test_concurrent_begin_gc_commit(d):
    db = init_db(d)
    oracle = os.path.join(d, "oracle.jsonl")
    drv = Driver(db, oracle)
    drv.begin("s")
    for i in range(20):
        drv.put("s", f"k{i:02d}", "v0")
    drv.commit("s")                          # @1
    drv.begin("u")
    for i in range(0, 20, 2):
        drv.put("u", f"k{i:02d}", "v1")
    drv.commit("u")                          # @2
    drv.begin("R")                           # 长读 @2
    drv.begin("w1")
    drv.put("w1", "x1", "1")
    drv.begin("w2")
    drv.put("w2", "x2", "2")
    # 并发:1 个慢 gc + 4 个 begin + 2 个 commit + 1 个 gc
    procs = []
    env = dict(os.environ, MVCC_SLEEP="gc:after_snapshot_read=800")
    procs.append(subprocess.Popen([sys.executable, MVCC, "--db", db, "gc"],
                                  env=env, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True))
    for i in range(4):
        procs.append(subprocess.Popen(
            [sys.executable, MVCC, "--db", db, "begin", f"--txn=c{i}"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
    for t in ("w1", "w2"):
        procs.append(subprocess.Popen(
            [sys.executable, MVCC, "--db", db, "commit", f"--txn={t}"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
    procs.append(subprocess.Popen([sys.executable, MVCC, "--db", db, "gc"],
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True))
    for p in procs:
        out, err = p.communicate()
        assert p.returncode == 0, f"并发子进程失败 rc={p.returncode}: {out}{err}"
    # 并发 begin 的事务登记到 oracle(重复 begin 返回原身份)
    for i in range(4):
        drv.begin(f"c{i}")
    drv.commit("w1")
    drv.commit("w2")
    # 长读视图不受并发影响
    assert jcli(db, "get", "--txn=R", "--key=k00")["value"] == "v1"
    assert jcli(db, "get", "--txn=R", "--key=k01")["value"] == "v0"
    # 独立验证:所有活跃快照(R + c0..c3)可见性 + 严格 gc
    verify_ok(db, oracle)
    for i in range(4):
        drv.abort(f"c{i}")
    drv.commit("R")
    jcli(db, "gc", "--summary")
    drv.close()
    verify_ok(db, oracle)


# ---------------------------------------------------------------------------
# 随机工作负载 + 独立验证器
# ---------------------------------------------------------------------------
@test
def test_randomized_workload_verify(d):
    db = init_db(d)
    oracle = os.path.join(d, "oracle.jsonl")
    drv = Driver(db, oracle)
    rng = random.Random(7)
    keys = [f"k{i:02d}" for i in range(25)]
    active = []
    n = 0
    for step in range(120):
        if not active:
            op = "begin"
        else:
            op = rng.choice(["begin", "put", "put", "put", "delete",
                             "commit", "abort", "gc", "get", "scan"])
        if op == "begin":
            if active and rng.random() < 0.3:
                t = rng.choice(active)
                assert drv.begin(t)["status"] == "exists"  # 幂等 begin
            else:
                n += 1
                drv.begin(f"t{n}")
                active.append(f"t{n}")
        elif op == "put":
            drv.put(rng.choice(active), rng.choice(keys), f"v{step}")
        elif op == "delete":
            drv.delete(rng.choice(active), rng.choice(keys))
        elif op == "commit":
            t = rng.choice(active)
            drv.commit(t)
            active.remove(t)
        elif op == "abort":
            t = rng.choice(active)
            drv.abort(t)
            active.remove(t)
        elif op == "gc":
            jcli(db, "gc", "--summary")
        elif op == "get":
            jcli(db, "get", f"--txn={rng.choice(active)}", f"--key={rng.choice(keys)}")
        else:
            jcli(db, "scan", f"--txn={rng.choice(active)}", "--limit=5")
    # 保留若干活跃事务,验证器按完整历史比对各快照的 get/scan 与 gc 结果
    # (严格 gc 检查要求“无多余版本”,先跑一次 gc 使保留集收敛到当前活跃快照)
    jcli(db, "gc", "--summary")
    verify_ok(db, oracle)
    # 结束全部事务后再 gc + 验证
    for i, t in enumerate(list(active)):
        if i % 2 == 0:
            drv.commit(t)
        else:
            drv.abort(t)
    jcli(db, "gc", "--summary")
    drv.close()
    verify_ok(db, oracle)


# ---------------------------------------------------------------------------
# 万级键压力(语义验证,非性能榜)
# ---------------------------------------------------------------------------
@test
def test_stress_10k_keys(d):
    db = init_db(d)
    oracle = os.path.join(d, "oracle.jsonl")
    drv = Driver(db, oracle)
    N = 10000
    seed_file = os.path.join(d, "seed.jsonl")
    with open(seed_file, "w", encoding="utf-8") as f:
        for i in range(N):
            f.write(json.dumps({"key": f"key{i:06d}", "value": f"v0-{i}"}) + "\n")
    drv.begin("seed")
    drv.put_file("seed", seed_file)
    drv.commit("seed")                       # @1: 10000 键
    drv.begin("R")                           # 长读 @1
    upd_file = os.path.join(d, "upd.jsonl")
    with open(upd_file, "w", encoding="utf-8") as f:
        for i in range(0, N, 10):
            f.write(json.dumps({"key": f"key{i:06d}", "value": f"v1-{i}"}) + "\n")
    drv.begin("u1")
    drv.put_file("u1", upd_file)
    drv.commit("u1")                         # @2: 更新 1000 键
    del_file = os.path.join(d, "del.jsonl")
    with open(del_file, "w", encoding="utf-8") as f:
        for i in range(0, N, 37):
            f.write(json.dumps({"key": f"key{i:06d}"}) + "\n")
    drv.begin("u2")
    drv.delete_file("u2", del_file)
    drv.commit("u2")                         # @3: 删除 271 键
    # 长读期间 gc:只有“更新过又被删除”的 28 键的中间版本(@2)可回收
    g1 = jcli(db, "gc", "--summary")
    assert g1["deleted_count"] == 28, g1
    # 长读视图不变:10000 键全在,被删键仍读得到旧值
    sc = jcli(db, "scan", "--txn=R")
    assert sc["count"] == N
    assert jcli(db, "get", "--txn=R", "--key=key000037")["value"] == "v0-37"
    assert jcli(db, "get", "--txn=R", "--key=key000000")["value"] == "v0-0"
    drv.commit("R")
    # 旧读结束后:被固定的 @1 版本全部释放
    g2 = jcli(db, "gc", "--summary")
    assert g2["deleted_count"] == 1243, g2
    assert sql(db, "SELECT COUNT(*) FROM kv_versions")[0][0] == N
    # 新快照:9729 键可见,删除键不复活
    drv.begin("T_check")
    sc = jcli(db, "scan", "--txn=T_check")
    assert sc["count"] == N - 271
    assert jcli(db, "get", "--txn=T_check", "--key=key000037")["status"] == "not_found"
    assert jcli(db, "get", "--txn=T_check", "--key=key000010")["value"] == "v1-10"
    verify_ok(db, oracle)                    # T_check 活跃时验证
    drv.abort("T_check")
    drv.close()
    verify_ok(db, oracle)


def main():
    global FAILURES
    only = sys.argv[1:] or None
    for fn in TESTS:
        if only and fn.__name__ not in only:
            continue
        t0 = time.time()
        try:
            with tempfile.TemporaryDirectory(prefix="mvcc_test_") as d:
                fn(d)
            print(f"PASS {fn.__name__} ({time.time()-t0:.1f}s)")
        except Exception:
            FAILURES += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(TESTS) if not only else ''} 测试,失败 {FAILURES}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
