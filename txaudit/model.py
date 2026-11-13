"""输入模型：JSON 加载、约束校验与单版本语义回放。

输入 JSON 顶层结构：
{
  "history_id": "可选字符串，缺省用 'default'",
  "keys": {"k1": "初始字符串值", ...},          # 1..20 个键，全部预存
  "transactions": [1, 2, 3],                    # 事务编号（整数，1..8 个，无需连续）
  "operations": [
    {"seq": 1, "transaction": 1, "op": "write", "write_id": "w1", "key": "k1", "value": "a"},
    {"seq": 2, "transaction": 1, "op": "read",  "key": "k1", "value": "a", "read_from": "w1"},
    {"seq": 9, "transaction": 1, "op": "commit"}
  ]
}

校验规则（违反任意一条都属于“输入语义错误”，不进入可串行化判定，
所有错误一次性收集，而不是遇到第一个就停下）：
1. 结构与规模：最多 8 个事务、20 个键、200 条操作；操作必须含唯一整数 seq。
2. 每条操作注明合法事务编号；write 必须带唯一 write_id、存在的键和字符串值；
   read 必须带存在的键、字符串实际读值以及 read_from（某个 write_id 或 INITIAL）。
3. 每个事务必须且只能提交一次；提交之后不允许再出现该事务的任何操作；
   不存在回滚；不允许声明了事务却没有任何操作。
4. 所有键预先存在，不支持新增或删除（write/read 只允许引用 keys 中声明的键）。
5. 单版本语义回放：按全局 seq 顺序，read 必须引用该键此前最近一次 write 的
   write_id（该 write 所属事务即使尚未提交也照读）；此前没有任何 write 时只能
   引用 INITIAL，且实际值必须等于该键的初始值。
6. 引用身份与实际值双重核对：两个不同 write_id 即使写入相同字符串也不得混同。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

INITIAL = "INITIAL"

MAX_TRANSACTIONS = 8
MAX_KEYS = 20
MAX_OPERATIONS = 200


class InputError(Exception):
    """输入文件本身无法解析（非历史语义错误），例如 JSON 语法错误。"""


@dataclass(frozen=True)
class Op:
    """一条规范化后的操作。"""

    seq: int
    tid: int
    kind: str  # 'read' | 'write' | 'commit'
    key: str | None = None
    value: str | None = None
    write_id: str | None = None
    read_from: str | None = None


@dataclass
class SemanticError:
    """一条可定位到操作（或结构层面）的输入语义错误。"""

    code: str
    message: str
    seq: int | None = None
    transaction: int | None = None
    write_id: str | None = None
    key: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.seq is not None:
            d["seq"] = self.seq
        if self.transaction is not None:
            d["transaction"] = self.transaction
        if self.write_id is not None:
            d["write_id"] = self.write_id
        if self.key is not None:
            d["key"] = self.key
        return d


@dataclass
class History:
    """通过校验后的规范化历史。"""

    history_id: str
    keys: dict[str, str]
    tids: list[int]  # 已排序的事务编号
    ops: list[Op]  # 已按 seq 升序
    # 派生数据
    writes_by_id: dict[str, Op] = field(default_factory=dict)
    last_write_on_key: dict[str, str | None] = field(default_factory=dict)  # key -> write_id/INITIAL
    final_writer: dict[str, str | None] = field(default_factory=dict)  # key -> write_id/INITIAL
    read_sources: dict[int, str | None] = field(default_factory=dict)  # seq -> read_from

    def op_index(self) -> dict[int, Op]:
        return {op.seq: op for op in self.ops}


def load_json(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - 由 CLI 层兜底
        raise InputError(f"无法读取输入文件 {p}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InputError(f"输入不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise InputError("输入 JSON 顶层必须是对象")
    return data


def _err(errors: list[SemanticError], code: str, message: str, **kw: Any) -> None:
    errors.append(SemanticError(code=code, message=message, **kw))


def parse_and_validate(data: dict[str, Any]) -> tuple[History | None, list[SemanticError]]:
    """解析并校验输入。返回 (规范化历史, 错误列表)；有错误时历史为 None。"""
    errors: list[SemanticError] = []

    history_id = data.get("history_id", "default")
    if not isinstance(history_id, str) or not history_id:
        _err(errors, "BAD_HISTORY_ID", "history_id 必须是非空字符串")

    raw_keys = data.get("keys")
    keys: dict[str, str] = {}
    if not isinstance(raw_keys, dict) or not raw_keys:
        _err(errors, "BAD_KEYS", "keys 必须是非空对象，声明所有预存键及其初始字符串值")
    else:
        if len(raw_keys) > MAX_KEYS:
            _err(errors, "TOO_MANY_KEYS", f"键数量 {len(raw_keys)} 超过上限 {MAX_KEYS}")
        for k, v in raw_keys.items():
            if not isinstance(k, str) or not k:
                _err(errors, "BAD_KEY_NAME", f"非法键名：{k!r}")
                continue
            if not isinstance(v, str):
                _err(errors, "BAD_KEY_VALUE", f"键 {k} 的初始值必须是字符串", key=k)
                continue
            keys[k] = v

    raw_tids = data.get("transactions")
    tids: list[int] = []
    if not isinstance(raw_tids, list) or not raw_tids:
        _err(errors, "BAD_TRANSACTIONS", "transactions 必须是非空数组")
    else:
        if len(raw_tids) > MAX_TRANSACTIONS:
            _err(errors, "TOO_MANY_TRANSACTIONS",
                 f"事务数量 {len(raw_tids)} 超过上限 {MAX_TRANSACTIONS}")
        seen_tid: set[int] = set()
        for t in raw_tids:
            if isinstance(t, bool) or not isinstance(t, int):
                _err(errors, "BAD_TID", f"事务编号必须是整数：{t!r}", transaction=t if isinstance(t, int) else None)
                continue
            if t in seen_tid:
                _err(errors, "DUP_TID", f"事务编号重复：{t}", transaction=t)
                continue
            seen_tid.add(t)
            tids.append(t)
        tids.sort()

    raw_ops = data.get("operations")
    ops: list[Op] = []
    if not isinstance(raw_ops, list) or not raw_ops:
        _err(errors, "BAD_OPERATIONS", "operations 必须是非空数组")
    else:
        if len(raw_ops) > MAX_OPERATIONS:
            _err(errors, "TOO_MANY_OPERATIONS",
                 f"操作数量 {len(raw_ops)} 超过上限 {MAX_OPERATIONS}")
        seen_seq: set[int] = set()
        seen_wid: set[str] = set()
        pending_read_from: list[tuple[int, int, str, str]] = []  # (seq, tid, key, read_from)
        for i, raw in enumerate(raw_ops):
            if not isinstance(raw, dict):
                _err(errors, "BAD_OP", f"第 {i} 条操作不是对象")
                continue
            seq = raw.get("seq")
            if isinstance(seq, bool) or not isinstance(seq, int):
                _err(errors, "BAD_SEQ", f"操作缺少唯一整数 seq：{raw.get('seq')!r}")
                continue
            if seq in seen_seq:
                _err(errors, "DUP_SEQ", f"seq 重复：{seq}", seq=seq)
                # 不短路：继续检查该操作的其它字段（如重复 write_id），
                # 但该操作不会进入规范化列表
                dup_seq = True
            else:
                seen_seq.add(seq)
                dup_seq = False

            tid = raw.get("transaction")
            if isinstance(tid, bool) or not isinstance(tid, int) or (tids and tid not in tids):
                _err(errors, "BAD_OP_TID",
                     f"seq={seq} 的事务编号 {tid!r} 未在 transactions 中声明",
                     seq=seq, transaction=tid if isinstance(tid, int) else None)
                continue
            kind = raw.get("op")
            if kind not in ("read", "write", "commit"):
                _err(errors, "BAD_OP_KIND",
                     f"seq={seq} 的 op 必须是 read/write/commit，得到 {kind!r}",
                     seq=seq, transaction=tid)
                continue

            key = raw.get("key")
            value = raw.get("value")
            write_id = raw.get("write_id")
            read_from = raw.get("read_from")

            if kind == "commit":
                if key is not None or value is not None or write_id is not None or read_from is not None:
                    _err(errors, "COMMIT_FIELDS",
                         "commit 操作不允许携带 key/value/write_id/read_from 字段",
                         seq=seq, transaction=tid)
                if not dup_seq:
                    ops.append(Op(seq=seq, tid=tid, kind="commit"))
                continue

            # 先做字段级校验并登记 write_id，保证后续 read_from 引用检查不会因
            # 某条操作的键非法而漏报；操作本身仍不会进入规范化列表
            bad_key = not isinstance(key, str) or (keys and key not in keys)
            if bad_key:
                _err(errors, "UNKNOWN_KEY",
                     f"seq={seq} 引用了未声明的键 {key!r}（所有键必须预先存在，不支持新增）",
                     seq=seq, transaction=tid, key=key if isinstance(key, str) else None)
            if not isinstance(value, str):
                _err(errors, "BAD_OP_VALUE",
                     f"seq={seq} 的 value 必须是字符串", seq=seq, transaction=tid,
                     key=key if isinstance(key, str) else None)
                continue

            if kind == "write":
                if not isinstance(write_id, str) or not write_id:
                    _err(errors, "BAD_WRITE_ID",
                         f"seq={seq} 的 write 缺少合法 write_id",
                         seq=seq, transaction=tid,
                         key=key if isinstance(key, str) else None)
                    continue
                if write_id in seen_wid:
                    _err(errors, "DUP_WRITE_ID",
                         f"write_id 重复：{write_id}",
                         seq=seq, transaction=tid, write_id=write_id,
                         key=key if isinstance(key, str) else None)
                    continue
                seen_wid.add(write_id)
                if not dup_seq and not bad_key:
                    ops.append(Op(seq=seq, tid=tid, kind="write", key=key,
                                  value=value, write_id=write_id))
            else:  # read
                if not isinstance(read_from, str) or not read_from:
                    _err(errors, "BAD_READ_FROM",
                         f"seq={seq} 的 read 缺少 read_from（write_id 或 INITIAL）",
                         seq=seq, transaction=tid,
                         key=key if isinstance(key, str) else None)
                    continue
                pending_read_from.append((seq, tid, key if isinstance(key, str) else "", read_from))
                if not dup_seq and not bad_key:
                    ops.append(Op(seq=seq, tid=tid, kind="read", key=key,
                                  value=value, read_from=read_from))

        # read_from 引用检查放在解析完成后，确保即使存在其它结构错误
        # （如未知键导致操作被剔除）也不会漏报坏引用
        for rseq, rtid, rkey, rfrom in pending_read_from:
            if rfrom != INITIAL and rfrom not in seen_wid:
                _err(errors, "UNKNOWN_READ_FROM",
                     f"seq={rseq} 的 read_from={rfrom!r} 既不是 INITIAL 也不是已知 write_id",
                     seq=rseq, transaction=rtid, key=rkey or None)

    # ---- 提交规则（在提前返回之前检查，保证多错误能一次性收集）----
    if ops and tids:
        ops.sort(key=lambda o: o.seq)
        commit_count: dict[int, int] = {t: 0 for t in tids}
        committed: set[int] = set()
        ops_of_tid: dict[int, list[Op]] = {t: [] for t in tids}
        for op in ops:
            ops_of_tid[op.tid].append(op)
            if op.tid in committed:
                if op.kind == "commit":
                    # 重复提交单独定性，不能被通用的“提交后操作”掩盖
                    _err(errors, "MULTI_COMMIT",
                         f"事务 {op.tid} 多次提交，seq={op.seq} 是多余的 commit",
                         seq=op.seq, transaction=op.tid)
                else:
                    _err(errors, "OP_AFTER_COMMIT",
                         f"事务 {op.tid} 已提交，seq={op.seq} 是提交后的非法操作",
                         seq=op.seq, transaction=op.tid)
                continue
            if op.kind == "commit":
                commit_count[op.tid] += 1
                committed.add(op.tid)
        for t in tids:
            if commit_count[t] == 0:
                _err(errors, "MISSING_COMMIT",
                     f"事务 {t} 缺少 commit（每个事务必须且只能提交一次）",
                     transaction=t)
            if not ops_of_tid[t]:
                _err(errors, "EMPTY_TRANSACTION",
                     f"事务 {t} 声明了但没有任何操作", transaction=t)

    # 结构层面的硬错误会让语义回放不可靠，错误已尽量一次性收集，此处返回
    if errors or not ops or not tids or not keys:
        return None, errors

    # ---- write_id -> 操作 映射（read_from 引用合法性已在解析循环内检查）----
    writes_by_id: dict[str, Op] = {}
    for op in ops:
        if op.kind == "write":
            writes_by_id[op.write_id] = op

    # ---- 单版本语义回放：逐键记录最近一次 write 的身份，核对身份与实际值 ----
    last_write: dict[str, str | None] = {k: None for k in keys}  # None 表示此前无写入
    last_value: dict[str, str] = dict(keys)  # 每个键当前可见值（含未提交写入）
    read_sources: dict[int, str | None] = {}
    for op in ops:
        if op.kind == "write":
            last_write[op.key] = op.write_id  # 身份：write_id，与值无关
            last_value[op.key] = op.value
        elif op.kind == "read":
            expected_source = last_write[op.key] if last_write[op.key] is not None else INITIAL
            read_sources[op.seq] = op.read_from
            if op.read_from != expected_source:
                # 身份错误：即使值相同也必须报告，不同 write_id 不得混同
                _err(errors, "BAD_READ_SOURCE",
                     f"seq={op.seq} 的 read 应来源于 {expected_source}，"
                     f"实际声明来源 {op.read_from}（按全局顺序该键此前最近一次 write 的身份判定）",
                     seq=op.seq, transaction=op.tid, key=op.key)
            if op.value != last_value[op.key]:
                _err(errors, "BAD_READ_VALUE",
                     f"seq={op.seq} 的实际读值 {op.value!r} 与来源 {expected_source} "
                     f"可见的值 {last_value[op.key]!r} 不一致",
                     seq=op.seq, transaction=op.tid, key=op.key)
            # 额外核对：若 read_from 指向某个 write，该 write 的键与值必须匹配
            if op.read_from in writes_by_id:
                w = writes_by_id[op.read_from]
                if w.key != op.key:
                    _err(errors, "READ_FROM_WRONG_KEY",
                                 f"seq={op.seq} 读取键 {op.key}，但来源 write {w.write_id} 写的是键 {w.key}",
                                 seq=op.seq, transaction=op.tid, key=op.key)
                elif w.value != op.value:
                    _err(errors, "READ_FROM_WRONG_VALUE",
                                 f"seq={op.seq} 声明来源 {w.write_id}，但其写入值 {w.value!r} "
                                 f"与实际读值 {op.value!r} 不一致",
                                 seq=op.seq, transaction=op.tid, key=op.key)

    if errors:
        return None, errors

    final_writer = {k: (last_write[k] if last_write[k] is not None else INITIAL) for k in keys}
    history = History(
        history_id=history_id,
        keys=keys,
        tids=tids,
        ops=ops,
        writes_by_id=writes_by_id,
        last_write_on_key={k: (v if v is not None else INITIAL) for k, v in last_write.items()},
        final_writer=final_writer,
        read_sources=read_sources,
    )
    return history, []
