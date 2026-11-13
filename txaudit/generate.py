"""本地合成事务历史，不连接任何真实数据库。

两部分：
1. FIXED_SUITES：人工构造、语义自洽的固定样例，覆盖题目要求的全部情形；
   每条 read 的 read_from 与 value 已按单版本语义手工核对。
2. generate_random_history：以确定性伪随机数（种子固定即可复现）合成任意规模
   历史，并通过独立的单版本回放自动标注 read_from 与实际读值，保证生成结果
   本身满足输入语义。
"""

from __future__ import annotations

import random
from typing import Any

from .model import INITIAL, MAX_KEYS, MAX_OPERATIONS, MAX_TRANSACTIONS

# --------------------------------------------------------------------------- #
# 固定样例套件
# --------------------------------------------------------------------------- #

FIXED_SUITES: dict[str, dict[str, Any]] = {
    # 1) 正常串行：T1 整段先于 T2，两事务在同一键 x 上有写-写/读-写冲突，
    #    唯一合法排列就是 [1,2]，两个判定都应为真
    "serial": {
        "history_id": "serial",
        "keys": {"x": "x0", "y": "y0"},
        "transactions": [1, 2],
        "operations": [
            {"seq": 1, "transaction": 1, "op": "write", "write_id": "w1", "key": "x", "value": "a"},
            {"seq": 2, "transaction": 1, "op": "read", "key": "x", "value": "a", "read_from": "w1"},
            {"seq": 3, "transaction": 1, "op": "write", "write_id": "w2", "key": "y", "value": "ay"},
            {"seq": 4, "transaction": 1, "op": "commit"},
            {"seq": 5, "transaction": 2, "op": "read", "key": "x", "value": "a", "read_from": "w1"},
            {"seq": 6, "transaction": 2, "op": "write", "write_id": "w3", "key": "x", "value": "b"},
            {"seq": 7, "transaction": 2, "op": "read", "key": "y", "value": "ay", "read_from": "w2"},
            {"seq": 8, "transaction": 2, "op": "commit"},
        ],
    },

    # 2) 只读事务 T3：T1 写 x、T2 写 y，两个写事务之间没有任何冲突边；
    #    T3 只读取 x 的来源 w1，因此 T3 必须排在 T1 之后，但与 T2 的相对
    #    次序自由 -> 合法排列 [1,2,3]、[1,3,2]，共 2 个
    "readonly": {
        "history_id": "readonly",
        "keys": {"x": "x0", "y": "y0"},
        "transactions": [1, 2, 3],
        "operations": [
            {"seq": 1, "transaction": 1, "op": "write", "write_id": "w1", "key": "x", "value": "a"},
            {"seq": 2, "transaction": 2, "op": "write", "write_id": "w2", "key": "y", "value": "b"},
            {"seq": 3, "transaction": 3, "op": "read", "key": "x", "value": "a", "read_from": "w1"},
            {"seq": 4, "transaction": 3, "op": "read", "key": "x", "value": "a", "read_from": "w1"},
            {"seq": 5, "transaction": 3, "op": "commit"},
            {"seq": 6, "transaction": 1, "op": "commit"},
            {"seq": 7, "transaction": 2, "op": "commit"},
        ],
    },

    # 3) 读后写循环：两事务都从 INITIAL 读 x 再写 x，视图/冲突均不可串行化，
    #    合法排列数必须为 0（枚举完 2! 个排列后得出，不能一次失败就下结论）
    "rw_cycle_bad": {
        "history_id": "rw_cycle_bad",
        "keys": {"x": "x0"},
        "transactions": [1, 2],
        "operations": [
            {"seq": 1, "transaction": 1, "op": "read", "key": "x", "value": "x0", "read_from": INITIAL},
            {"seq": 2, "transaction": 2, "op": "read", "key": "x", "value": "x0", "read_from": INITIAL},
            {"seq": 3, "transaction": 1, "op": "write", "write_id": "w1", "key": "x", "value": "a"},
            {"seq": 4, "transaction": 2, "op": "write", "write_id": "w2", "key": "x", "value": "b"},
            {"seq": 5, "transaction": 1, "op": "commit"},
            {"seq": 6, "transaction": 2, "op": "commit"},
        ],
    },

    # 4) 事务读自身写入：含同键多次自写自读，read 来源必须精确到 write_id
    "self_read": {
        "history_id": "self_read",
        "keys": {"x": "x0"},
        "transactions": [1],
        "operations": [
            {"seq": 1, "transaction": 1, "op": "read", "key": "x", "value": "x0", "read_from": INITIAL},
            {"seq": 2, "transaction": 1, "op": "write", "write_id": "w1", "key": "x", "value": "a"},
            {"seq": 3, "transaction": 1, "op": "read", "key": "x", "value": "a", "read_from": "w1"},
            {"seq": 4, "transaction": 1, "op": "write", "write_id": "w2", "key": "x", "value": "c"},
            {"seq": 5, "transaction": 1, "op": "read", "key": "x", "value": "c", "read_from": "w2"},
            {"seq": 6, "transaction": 1, "op": "commit"},
        ],
    },

    # 5a) 两事务、无任何 read 的盲写交叉：冲突图 1<->2 有环（冲突不可串行化）。
    #     两事务在两个键上互为最终写入者，视图等价排列数同样为 0——
    #     说明“图有环”在两事务纯盲写下两个判定都失败（与 5b 的三事务反例对照）
    "blind_write_cycle_two_txn": {
        "history_id": "blind_write_cycle_two_txn",
        "keys": {"x": "x0", "y": "y0"},
        "transactions": [1, 2],
        "operations": [
            {"seq": 1, "transaction": 1, "op": "write", "write_id": "w1", "key": "x", "value": "a"},
            {"seq": 2, "transaction": 2, "op": "write", "write_id": "w2", "key": "x", "value": "b"},
            {"seq": 3, "transaction": 2, "op": "write", "write_id": "w3", "key": "y", "value": "c"},
            {"seq": 4, "transaction": 1, "op": "write", "write_id": "w4", "key": "y", "value": "d"},
            {"seq": 5, "transaction": 1, "op": "commit"},
            {"seq": 6, "transaction": 2, "op": "commit"},
        ],
    },

    # 5b) 题目核心反例——盲写使冲突图有环，但仍存在视图等价排列（至少 3 事务）。
    #     T1: w(x); T2: w(x) w(y); T3: w(x) w(y)，全程没有 read。
    #     冲突图含 1->2、2->1 等成环边；但最终写入者 x、y 都是 T3，
    #     任何以 T3 收尾的排列（[1,2,3]、[2,1,3]）都视图等价，码点序最小为 [1,2,3]
    "blind_write_cycle_view_ok": {
        "history_id": "blind_write_cycle_view_ok",
        "keys": {"x": "x0", "y": "y0"},
        "transactions": [1, 2, 3],
        "operations": [
            {"seq": 1, "transaction": 1, "op": "write", "write_id": "w1", "key": "x", "value": "a"},
            {"seq": 2, "transaction": 2, "op": "write", "write_id": "w2", "key": "x", "value": "b"},
            {"seq": 3, "transaction": 2, "op": "write", "write_id": "w3", "key": "y", "value": "c"},
            {"seq": 4, "transaction": 1, "op": "write", "write_id": "w4", "key": "y", "value": "d"},
            {"seq": 5, "transaction": 3, "op": "write", "write_id": "w5", "key": "x", "value": "e"},
            {"seq": 6, "transaction": 3, "op": "write", "write_id": "w6", "key": "y", "value": "f"},
            {"seq": 7, "transaction": 1, "op": "commit"},
            {"seq": 8, "transaction": 2, "op": "commit"},
            {"seq": 9, "transaction": 3, "op": "commit"},
        ],
    },

    # 6a) 两个不同 write_id 写相同字符串，来源身份仍须区分——合法历史。
    #     初始值也故意设为 SAME，使“值恰好相同”的干扰贯穿 INITIAL/w1/w2 三者
    "same_value_identity_ok": {
        "history_id": "same_value_identity_ok",
        "keys": {"x": "SAME"},
        "transactions": [1, 2],
        "operations": [
            {"seq": 1, "transaction": 1, "op": "write", "write_id": "w1", "key": "x", "value": "SAME"},
            {"seq": 2, "transaction": 2, "op": "write", "write_id": "w2", "key": "x", "value": "SAME"},
            {"seq": 3, "transaction": 2, "op": "read", "key": "x", "value": "SAME", "read_from": "w2"},
            {"seq": 4, "transaction": 1, "op": "commit"},
            {"seq": 5, "transaction": 2, "op": "commit"},
        ],
    },

    # 6b) 同上但 read_from 故意错指为 w1：值恰好相同也必须判输入语义错误，
    #     不得因为字符串相同而混同来源
    "same_value_bad_source": {
        "history_id": "same_value_bad_source",
        "keys": {"x": "SAME"},
        "transactions": [1, 2],
        "operations": [
            {"seq": 1, "transaction": 1, "op": "write", "write_id": "w1", "key": "x", "value": "SAME"},
            {"seq": 2, "transaction": 2, "op": "write", "write_id": "w2", "key": "x", "value": "SAME"},
            {"seq": 3, "transaction": 2, "op": "read", "key": "x", "value": "SAME", "read_from": "w1"},
            {"seq": 4, "transaction": 1, "op": "commit"},
            {"seq": 5, "transaction": 2, "op": "commit"},
        ],
    },

    # 6c) 排列级身份核对反例：INITIAL、w1、w2 的字符串全为 SAME。
    #     原历史中 T3 在 w2 之后读 x、来源 w2（合法）；但排列把 T3 放到最前时，
    #     它读到的来源身份变成 INITIAL，字符串却仍然相同——视图等价判定必须
    #     按 write_id 身份拒绝该排列，而不能被相同字符串骗过
    "same_value_permutation_identity": {
        "history_id": "same_value_permutation_identity",
        "keys": {"x": "SAME"},
        "transactions": [1, 2, 3],
        "operations": [
            {"seq": 1, "transaction": 1, "op": "write", "write_id": "w1", "key": "x", "value": "SAME"},
            {"seq": 2, "transaction": 2, "op": "write", "write_id": "w2", "key": "x", "value": "SAME"},
            {"seq": 3, "transaction": 3, "op": "read", "key": "x", "value": "SAME", "read_from": "w2"},
            {"seq": 4, "transaction": 1, "op": "commit"},
            {"seq": 5, "transaction": 2, "op": "commit"},
            {"seq": 6, "transaction": 3, "op": "commit"},
        ],
    },

    # 7) 视图可串行化但冲突不可串行化的经典结构（含盲写 T4）：
    #    T1 读 x(INITIAL)；T2 读 x 来源 w1、写 x(w2)；
    #    T3 读 x 来源 w2；T4 盲写 x(w4)、盲写 y(w5)。
    #    冲突图成环，唯一视图等价排列 [1,2,3,4]（T4 为全部键的最终写入者）
    "view_but_not_conflict": {
        "history_id": "view_but_not_conflict",
        "keys": {"x": "x0", "y": "y0"},
        "transactions": [1, 2, 3, 4],
        "operations": [
            {"seq": 1, "transaction": 1, "op": "read", "key": "x", "value": "x0", "read_from": INITIAL},
            {"seq": 2, "transaction": 1, "op": "write", "write_id": "w1", "key": "x", "value": "a"},
            {"seq": 3, "transaction": 2, "op": "read", "key": "x", "value": "a", "read_from": "w1"},
            {"seq": 4, "transaction": 2, "op": "write", "write_id": "w2", "key": "x", "value": "b"},
            {"seq": 5, "transaction": 3, "op": "read", "key": "x", "value": "b", "read_from": "w2"},
            {"seq": 6, "transaction": 4, "op": "write", "write_id": "w3", "key": "x", "value": "c"},
            {"seq": 7, "transaction": 3, "op": "write", "write_id": "w4", "key": "x", "value": "d"},
            {"seq": 8, "transaction": 4, "op": "write", "write_id": "w5", "key": "x", "value": "e"},
            {"seq": 9, "transaction": 4, "op": "write", "write_id": "w6", "key": "y", "value": "f"},
            {"seq": 10, "transaction": 1, "op": "commit"},
            {"seq": 11, "transaction": 2, "op": "commit"},
            {"seq": 12, "transaction": 3, "op": "commit"},
            {"seq": 13, "transaction": 4, "op": "commit"},
        ],
    },

    # 8) 未写入键保持 INITIAL：y 从未被写，最终写入者必须是 INITIAL
    "untouched_key": {
        "history_id": "untouched_key",
        "keys": {"x": "x0", "y": "y0"},
        "transactions": [1, 2],
        "operations": [
            {"seq": 1, "transaction": 1, "op": "read", "key": "y", "value": "y0", "read_from": INITIAL},
            {"seq": 2, "transaction": 1, "op": "write", "write_id": "w1", "key": "x", "value": "a"},
            {"seq": 3, "transaction": 2, "op": "read", "key": "y", "value": "y0", "read_from": INITIAL},
            {"seq": 4, "transaction": 2, "op": "read", "key": "x", "value": "a", "read_from": "w1"},
            {"seq": 5, "transaction": 1, "op": "commit"},
            {"seq": 6, "transaction": 2, "op": "commit"},
        ],
    },
}


def list_suites() -> list[str]:
    return sorted(FIXED_SUITES)


# --------------------------------------------------------------------------- #
# 确定性随机合成
# --------------------------------------------------------------------------- #

def generate_random_history(
    *,
    seed: int = 20261113,
    n_transactions: int = 8,
    n_keys: int = 12,
    n_ops: int = 64,
    history_id: str | None = None,
) -> dict[str, Any]:
    """合成满足输入语义的随机历史。

    生成过程中实时维护单版本状态（最近 write 身份 + 当前值），因此每条 read
    的 read_from/value 都是真实回放结果，生成结果可直接通过 model 校验。
    每个事务预留一条 commit，故实际 read/write 条数 = n_ops - n_transactions。
    """
    if not (1 <= n_transactions <= MAX_TRANSACTIONS):
        raise ValueError(f"事务数须在 1..{MAX_TRANSACTIONS}")
    if not (1 <= n_keys <= MAX_KEYS):
        raise ValueError(f"键数须在 1..{MAX_KEYS}")
    if n_ops > MAX_OPERATIONS or n_ops <= n_transactions:
        raise ValueError(f"操作数须 > 事务数且 <= {MAX_OPERATIONS}")

    rng = random.Random(seed)
    tids = list(range(1, n_transactions + 1))
    key_names = [f"k{i}" for i in range(n_keys)]
    keys = {k: f"init_{k}" for k in key_names}

    active = set(tids)  # 尚未 commit 的事务
    last_source: dict[str, str] = {k: INITIAL for k in key_names}
    last_value: dict[str, str] = dict(keys)
    operations: list[dict[str, Any]] = []
    write_seq = 0
    rw_budget = n_ops - n_transactions  # 给每个事务留一条 commit

    for seq in range(1, n_ops + 1):
        remaining_slots = n_ops - seq + 1
        # 最后阶段强制收尾：剩余槽位 == 剩余未提交事务数时，全部提交
        if not active or remaining_slots == len(active):
            tid = min(active)
            operations.append({"seq": seq, "transaction": tid, "op": "commit"})
            active.discard(tid)
            continue

        tid = rng.choice(sorted(active))
        key = rng.choice(key_names)
        kind = rng.choice(["read", "write", "write"])  # 写权重高一些，冲突更丰富
        if kind == "read":
            operations.append({
                "seq": seq, "transaction": tid, "op": "read", "key": key,
                "value": last_value[key], "read_from": last_source[key],
            })
        else:
            write_seq += 1
            wid = f"w{write_seq}"
            val = f"v{write_seq}_{key}"
            operations.append({
                "seq": seq, "transaction": tid, "op": "write",
                "write_id": wid, "key": key, "value": val,
            })
            last_source[key] = wid
            last_value[key] = val

    assert not active, "所有事务都应已提交"
    return {
        "history_id": history_id or f"random_seed{seed}_t{n_transactions}_k{n_keys}_o{n_ops}",
        "keys": keys,
        "transactions": tids,
        "operations": operations,
    }
