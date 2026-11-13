"""存在/缺失证明生成。

证明文件（UTF-8 JSON）结构：

{
  "format_version": 1,
  "record_count":  <清单记录数，仅用于与可信清单交叉核对>,
  "height":        <根所在层号>,
  "answers": [
    {"query": <字符串>, "present": true,
     "index": <真实叶下标>, "key": <字符串>, "value": <字符串>},
    {"query": <字符串>, "present": false,
     "predecessor": null | {"index","key","value"},
     "successor":   null | {"index","key","value"}}
  ],
  "nodes": [{"level": L, "index": I, "digest_hex": ".."} ...]
}

共享语义：所有查询需要的兄弟节点按 (层号, 下标) 去重，只保存一次；
验证方按完全相同的规则推导所需节点坐标，因此多查询共享的上层路径
不会被机械复制。节点坐标按 (level, index) 排序，保证产物确定。
"""

from __future__ import annotations

import json
from typing import Optional

from . import FORMAT_VERSION
from .errors import InputError, ProofError
from .hashing import tree_height
from .manifest import Manifest
from .storage import Tree

PROOF_TYPE = "ordered-merkle-proof"


def _decode_queries(queries: list[str]) -> list[bytes]:
    if not isinstance(queries, list) or not queries:
        raise InputError("查询集合必须是非空 JSON 字符串数组")
    decoded: list[bytes] = []
    seen: set[bytes] = set()
    for q in queries:
        if not isinstance(q, str):
            raise InputError("查询键必须是字符串")
        raw = q.encode("utf-8")
        if len(raw) == 0:
            raise InputError("查询键必须为非空字符串")
        if raw in seen:
            raise InputError(f"查询键重复: {q!r}")
        seen.add(raw)
        decoded.append(raw)
    return decoded


def _leaf_entry(index: int, key: bytes, value: bytes) -> dict:
    return {
        "index": index,
        "key": key.decode("utf-8"),
        "value": value.decode("utf-8"),
    }


def sibling_path(index: int, height: int) -> list[tuple[int, int]]:
    """叶下标 index 到根路径上全部兄弟节点的 (level, index) 坐标。

    level 0 兄弟是相邻叶；level h 的兄弟是同一父节点下的另一子树。
    """
    coords: list[tuple[int, int]] = []
    cur = index
    for level in range(height):
        if cur % 2 == 0:
            sibling = cur + 1
        else:
            sibling = cur - 1
        coords.append((level, sibling))
        cur //= 2
    return coords


def _answer_for(tree: Tree, query: bytes) -> dict:
    hit = tree.lookup(query)
    if hit is not None:
        index, value = hit
        return {
            "query": query.decode("utf-8"),
            "present": True,
            "index": index,
            "key": query.decode("utf-8"),
            "value": value.decode("utf-8"),
        }

    pred = tree.predecessor(query)
    succ = tree.successor(query)
    return {
        "query": query.decode("utf-8"),
        "present": False,
        "predecessor": None if pred is None else _leaf_entry(*pred),
        "successor": None if succ is None else _leaf_entry(*succ),
    }


def _disclosed_indices(answer: dict) -> list[int]:
    """该答案自身携带的真实叶下标（存在 1 个，缺失 0/1/2 个）。"""
    if answer["present"]:
        return [answer["index"]]
    out: list[int] = []
    for side in ("predecessor", "successor"):
        leaf = answer[side]
        if leaf is not None:
            out.append(leaf["index"])
    return out


def generate_proof(tree: Tree, manifest: Manifest, queries: list[str]) -> dict:
    if tree.manifest != manifest:
        raise ProofError("树与可信清单不一致，拒绝出证")
    raw_queries = _decode_queries(queries)
    height = tree_height(manifest.record_count)

    answers = [_answer_for(tree, q) for q in raw_queries]

    # 收集全部答案需要的兄弟坐标；set 天然实现跨查询去重共享。
    needed: set[tuple[int, int]] = set()
    for answer in answers:
        for idx in _disclosed_indices(answer):
            needed.update(sibling_path(idx, height))

    nodes = [
        {
            "level": level,
            "index": idx,
            "digest_hex": tree.node_digest(level, idx).hex(),
        }
        for level, idx in sorted(needed)
    ]
    return {
        "type": PROOF_TYPE,
        "format_version": FORMAT_VERSION,
        "record_count": manifest.record_count,
        "height": height,
        "answers": answers,
        "nodes": nodes,
    }


def write_proof(proof: dict, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(proof, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def read_proof(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            proof = json.load(f)
    except FileNotFoundError as exc:
        raise ProofError(f"证明文件不存在: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ProofError(f"证明文件不是合法 JSON: {exc}") from exc
    if not isinstance(proof, dict) or proof.get("type") != PROOF_TYPE:
        raise ProofError("证明类型标识缺失或不符")
    return proof


# ---- 证明体积指标 -------------------------------------------------------

def proof_byte_size(proof: dict) -> int:
    """证明落盘后的 UTF-8 字节数（与 write_proof 的序列化方式一致）。"""
    return len(
        json.dumps(proof, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    ) + 1  # write_proof 末尾的换行符


def naive_path_digest_count(proof: dict) -> int:
    """逐条复制完整路径时需要的 32 字节摘要总数（不含答案本身）。"""
    height = proof["height"]
    total = 0
    for answer in proof["answers"]:
        total += height * len(_disclosed_indices(answer))
    return total


def shared_node_count(proof: dict) -> int:
    return len(proof["nodes"])


def per_query_bytes(proof: dict) -> list[int]:
    """每个查询的证明体积：答案 JSON 字节 + 均摊后的共享节点体积。"""
    node_bytes = sum(
        len(json.dumps(n, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        for n in proof["nodes"]
    )
    n = len(proof["answers"])
    out: list[int] = []
    for answer in proof["answers"]:
        answer_bytes = len(
            json.dumps(answer, ensure_ascii=False, sort_keys=True).encode("utf-8")
        )
        out.append(answer_bytes + node_bytes // n)
    return out
