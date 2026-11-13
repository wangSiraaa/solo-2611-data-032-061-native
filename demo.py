#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
demo.py — 两进程交错演示 MVCC 语义。

  python3 demo.py run --dir .runtime/demo

编排进程(init/种子/最终 gc/独立验证)之外,启动两个并发工作进程:
  进程 A(长读事务 T1):begin 后一直保持旧视图,跨多次 gc 读到的仍是旧值;
  进程 B(写事务 Tw1..Tw3):对同一键 alpha 依次 覆盖 → 删除 → 重新插入,
                          每步提交后执行一次 gc。
A 提交(只读)结束后,编排进程再执行一次 gc,原先被 T1 固定的旧版本被释放。
最后由 verify 按完整提交历史(oracle.jsonl)独立校验,并用 --strict-gc
确认没有多留任何不需要的版本。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
MVCC = os.path.join(HERE, "mvcc.py")


def cli(db, *argv, check=True):
    p = subprocess.run([sys.executable, MVCC, "--db", db, *argv],
                       capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"mvcc {argv} 失败 rc={p.returncode}\n{p.stdout}{p.stderr}")
    return p


def jcli(db, *argv):
    return json.loads(cli(db, *argv).stdout)


def touch(d, name):
    open(os.path.join(d, name), "w").close()


def wait_flag(d, name, timeout=120.0):
    t0 = time.time()
    while not os.path.exists(os.path.join(d, name)):
        if time.time() - t0 > timeout:
            raise TimeoutError(f"等待标志超时: {name}")
        time.sleep(0.05)


def log_line(path, obj):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# 进程 A:长读事务
# ---------------------------------------------------------------------------
def cmd_worker_a(args) -> int:
    db, d = args.db, os.path.abspath(args.dir)
    oracle = os.path.join(d, "oracle_a.jsonl")
    b = jcli(db, "begin", "--txn=T1")
    log_line(oracle, {"type": "begin", "txn": "T1", "begin_version": b["begin_version"]})
    print(f"[A] begin T1,快照版本 = {b['begin_version']}", flush=True)
    touch(d, "a_begun")
    for step, note in [(1, "B 覆盖后"), (2, "B 删除后"), (3, "B 重新插入后")]:
        wait_flag(d, f"b_step{step}")
        g = jcli(db, "get", "--txn=T1", "--key=alpha")
        print(f"[A] {note}: get alpha -> {g['status']} value={g.get('value')}", flush=True)
        assert g["status"] == "found" and g["value"] == "v0", \
            f"长读事务视图被污染: {g}"
    sc = jcli(db, "scan", "--txn=T1")
    print(f"[A] scan 全库 -> {[(i['key'], i['value']) for i in sc['items']]}", flush=True)
    c = jcli(db, "commit", "--txn=T1")
    assert c["status"] == "committed" and c["read_only"], c
    log_line(oracle, {"type": "commit", "txn": "T1", "status": "committed",
                      "commit_version": None})
    print(f"[A] 只读提交完成(不消耗版本号): {c}", flush=True)
    touch(d, "a_done")
    return 0


# ---------------------------------------------------------------------------
# 进程 B:覆盖 / 删除 / 重新插入同一键,每步后 gc
# ---------------------------------------------------------------------------
def cmd_worker_b(args) -> int:
    db, d = args.db, os.path.abspath(args.dir)
    oracle = os.path.join(d, "oracle_b.jsonl")
    wait_flag(d, "a_begun")
    ops = [("put", "v1", "覆盖"), ("delete", None, "删除"), ("put", "v2", "重新插入")]
    for i, (op, val, note) in enumerate(ops, 1):
        txn = f"Tw{i}"
        b = jcli(db, "begin", f"--txn={txn}")
        log_line(oracle, {"type": "begin", "txn": txn,
                          "begin_version": b["begin_version"]})
        if op == "put":
            jcli(db, "put", f"--txn={txn}", "--key=alpha", f"--value={val}")
            log_line(oracle, {"type": "put", "txn": txn, "key": "alpha", "value": val})
        else:
            jcli(db, "delete", f"--txn={txn}", "--key=alpha")
            log_line(oracle, {"type": "delete", "txn": txn, "key": "alpha"})
        c = jcli(db, "commit", f"--txn={txn}")
        assert c["status"] == "committed", c
        log_line(oracle, {"type": "commit", "txn": txn, "status": "committed",
                          "commit_version": c["commit_version"],
                          "writes": {"alpha": val}})
        g = jcli(db, "gc")
        kept_alpha = [(e["version"], e["reasons"]) for e in g["kept"] if e["key"] == "alpha"]
        print(f"[B] 步骤{i}({note})提交为版本 {c['commit_version']};"
              f" gc 删除 {g['deleted_count']} 个版本,alpha 保留: {kept_alpha}", flush=True)
        touch(d, f"b_step{i}")
    wait_flag(d, "a_done")
    return 0


# ---------------------------------------------------------------------------
# 编排进程
# ---------------------------------------------------------------------------
def cmd_run(args) -> int:
    d = os.path.abspath(args.dir)
    os.makedirs(d, exist_ok=True)
    db = os.path.join(d, "demo.db")
    # 清理上一轮残留
    for name in os.listdir(d):
        if name.startswith(("demo.db", "oracle_", "a_", "b_step")) or name == "oracle.jsonl":
            os.remove(os.path.join(d, name))
    print("== init ==", flush=True)
    print(jcli(db, "init"))
    oracle_main = os.path.join(d, "oracle_main.jsonl")
    print("== 种子事务:alpha=v0, beta=b0, gamma=g0 ==", flush=True)
    jcli(db, "begin", "--txn=seed")
    for k, v in [("alpha", "v0"), ("beta", "b0"), ("gamma", "g0")]:
        jcli(db, "put", "--txn=seed", f"--key={k}", f"--value={v}")
    c = jcli(db, "commit", "--txn=seed")
    log_line(oracle_main, {"type": "begin", "txn": "seed", "begin_version": 0})
    log_line(oracle_main, {"type": "commit", "txn": "seed", "status": "committed",
                           "commit_version": c["commit_version"],
                           "writes": {"alpha": "v0", "beta": "b0", "gamma": "g0"}})
    print(f"种子提交为版本 {c['commit_version']}", flush=True)

    print("== 启动进程 A(长读)与进程 B(写+gc)==", flush=True)
    me = os.path.abspath(__file__)
    pa = subprocess.Popen([sys.executable, me, "worker-a", "--db", db, "--dir", d])
    pb = subprocess.Popen([sys.executable, me, "worker-b", "--db", db, "--dir", d])
    rc_a, rc_b = pa.wait(), pb.wait()
    if rc_a != 0 or rc_b != 0:
        print(f"演示失败: worker-a rc={rc_a}, worker-b rc={rc_b}")
        return 1

    print("== 长读事务已结束,执行最终 gc ==", flush=True)
    g = jcli(db, "gc")
    print(f"最终 gc 删除 {g['deleted_count']} 个版本:", flush=True)
    for e in g["deleted"]:
        print(f"  删除 {e['key']}@{e['version']}")
    for e in g["kept"]:
        print(f"  保留 {e['key']}@{e['version']} <- {e['reasons']}")

    print("== 独立验证(完整历史 oracle + 严格 gc 检查)==", flush=True)
    chk = jcli(db, "begin", "--txn=T_check")
    oracle = os.path.join(d, "oracle.jsonl")
    with open(oracle, "w", encoding="utf-8") as out:
        for part in ("oracle_main.jsonl", "oracle_a.jsonl", "oracle_b.jsonl"):
            with open(os.path.join(d, part), encoding="utf-8") as f:
                out.write(f.read())
        out.write(json.dumps({"type": "begin", "txn": "T_check",
                              "begin_version": chk["begin_version"]}) + "\n")
    v = jcli(db, "verify", f"--oracle={oracle}", "--strict-gc")
    print(json.dumps(v, ensure_ascii=False, indent=2))
    jcli(db, "abort", "--txn=T_check")
    ok = v["status"] == "ok"
    print("演示结果:", "OK" if ok else "FAILED", flush=True)
    return 0 if ok else 1


def main() -> int:
    p = argparse.ArgumentParser(prog="demo.py", description=__doc__)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=None)
    common.add_argument("--dir", default=".runtime/demo")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", parents=[common])
    sub.add_parser("worker-a", parents=[common])
    sub.add_parser("worker-b", parents=[common])
    args = p.parse_args()
    d = os.path.abspath(args.dir)
    if args.db is None:
        args.db = os.path.join(d, "demo.db")
    if args.cmd == "run":
        return cmd_run(args)
    if args.cmd == "worker-a":
        return cmd_worker_a(args)
    return cmd_worker_b(args)


if __name__ == "__main__":
    sys.exit(main())
