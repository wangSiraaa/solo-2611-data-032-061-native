"""独立验证器：只接收可信根清单、原查询集合和证明。

验证器从不打开数据集或树数据库，全部结论仅由三部分推出：
1. 可信清单里的记录数与根摘要；
2. 查询集合自身的字节序关系；
3. 证明中披露的叶答案与共享兄弟节点。

它逐项核对：查询/答案集合一致性、存在与缺失的排序邻接关系、
叶下标范围（含补齐叶不可作为真实记录）、全局键序与下标序的一致性，
最后从每个披露叶重建到根并与可信根比较。节点表必须恰好等于
按路径推导出的所需坐标集合：缺节点、重复节点、越界节点、
根节点入证或任何未使用节点都会被拒绝。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from . import FORMAT_VERSION
from .errors import ProofError
from .hashing import DIGEST_SIZE, internal_digest, leaf_digest, next_power_of_two, tree_height
from .manifest import Manifest
from .proving import sibling_path


@dataclass
class QueryConclusion:
    query: str
    present: bool
    index: Optional[int]
    predecessor: Optional[dict]
    successor: Optional[dict]


def _as_nonempty_str(obj: object, what: str) -> str:
    if not isinstance(obj, str) or len(obj) == 0:
        raise ProofError(f"{what} 必须是非空字符串")
    return obj


def _parse_leaf(obj: object, record_count: int, what: str) -> dict:
    if not isinstance(obj, dict) or set(obj.keys()) != {"index", "key", "value"}:
        raise ProofError(f"{what} 的叶对象结构非法")
    index = obj["index"]
    if not isinstance(index, int) or isinstance(index, bool):
        raise ProofError(f"{what} 的下标必须是整数")
    if not 0 <= index < record_count:
        # 下标越界同时覆盖了“把补齐叶冒充真实记录”的情况。
        raise ProofError(f"{what} 的叶下标 {index} 超出真实记录范围 [0,{record_count})")
    key = _as_nonempty_str(obj["key"], f"{what} 的 key")
    if not isinstance(obj["value"], str):
        raise ProofError(f"{what} 的 value 必须是字符串")
    return {"index": index, "key": key, "value": obj["value"]}


def verify_proof(manifest: Manifest, queries: list[str], proof: dict) -> list[QueryConclusion]:
    """验证通过返回逐项结论；任何矛盾都抛出 ProofError。"""
    # ---- 1. 信封与可信清单关系 ----------------------------------
    if not isinstance(proof, dict):
        raise ProofError("证明必须是 JSON 对象")
    if proof.get("type") != "ordered-merkle-proof":
        raise ProofError("证明类型标识缺失或不符")
    if proof.get("format_version") != FORMAT_VERSION:
        raise ProofError("证明格式版本不受支持")
    if proof.get("record_count") != manifest.record_count:
        # 证明不能用自带的记录数替换清单；篡改记录数在此被拦。
        raise ProofError("证明记录数与可信清单不一致")
    count = manifest.record_count
    height = tree_height(count)
    if proof.get("height") != height:
        raise ProofError("证明树高与清单记录数推导出的树高不一致")

    answers = proof.get("answers")
    nodes = proof.get("nodes")
    if not isinstance(answers, list) or not isinstance(nodes, list):
        raise ProofError("证明的 answers/nodes 必须是数组")

    # ---- 2. 原查询集合：非空、互不重复 ----------------------------
    if not isinstance(queries, list) or not queries:
        raise ProofError("查询集合必须是非空数组")
    query_bytes: list[bytes] = []
    query_set: set[bytes] = set()
    for q in queries:
        raw = _as_nonempty_str(q, "查询键").encode("utf-8")
        if raw in query_set:
            raise ProofError("查询集合存在重复键")
        query_set.add(raw)
        query_bytes.append(raw)

    # ---- 3. 答案集合必须与查询集合完全相等 ------------------------
    if len(answers) != len(query_set):
        raise ProofError("答案数量与查询数量不一致")
    answer_by_query: dict[bytes, dict] = {}
    for answer in answers:
        if not isinstance(answer, dict) or "query" not in answer or "present" not in answer:
            raise ProofError("答案对象结构非法")
        q = _as_nonempty_str(answer["query"], "答案的 query").encode("utf-8")
        if q not in query_set:
            raise ProofError("证明包含未请求查询的答案")
        if q in answer_by_query:
            raise ProofError("证明对同一查询给出了重复答案")
        answer_by_query[q] = answer
    if set(answer_by_query) != query_set:
        raise ProofError("答案集合与查询集合不一致")

    # ---- 4. 空数据集的专门规则 ------------------------------------
    if count == 0:
        for q in query_bytes:
            answer = answer_by_query[q]
            if answer.get("present") is not False:
                raise ProofError("空数据集上只能给出缺失结论")
            if answer.get("predecessor") is not None or answer.get("successor") is not None:
                raise ProofError("空数据集的缺失答案两侧必须均为空")
        if nodes:
            raise ProofError("空数据集证明不得包含任何节点")
        return [
            QueryConclusion(q.decode("utf-8"), False, None, None, None)
            for q in query_bytes
        ]

    leaf_count = next_power_of_two(count)
    disclosed: dict[int, bytes] = {}  # 下标 -> 键字节，全局一致性核验
    conclusions: list[QueryConclusion] = []

    # ---- 5. 逐项答案：范围、排序、邻接、无遗漏叶 ------------------
    for q in query_bytes:
        answer = answer_by_query[q]
        present = answer["present"]
        if present is True:
            if set(answer.keys()) != {"query", "present", "index", "key", "value"}:
                raise ProofError("存在答案字段集合非法")
            leaf = _parse_leaf(
                {"index": answer["index"], "key": answer["key"], "value": answer["value"]},
                count,
                "存在答案",
            )
            if leaf["key"].encode("utf-8") != q:
                raise ProofError("存在答案披露的键与查询键不一致")
            _register_disclosed(disclosed, leaf["index"], leaf["key"].encode("utf-8"))
            conclusions.append(
                QueryConclusion(leaf["key"], True, leaf["index"], None, None)
            )
        elif present is False:
            if set(answer.keys()) != {"query", "present", "predecessor", "successor"}:
                raise ProofError("缺失答案字段集合非法")
            pred = (
                None
                if answer["predecessor"] is None
                else _parse_leaf(answer["predecessor"], count, "前驱")
            )
            succ = (
                None
                if answer["successor"] is None
                else _parse_leaf(answer["successor"], count, "后继")
            )
            if pred is None and succ is None:
                raise ProofError("非空数据集的缺失答案至少要给出一侧邻接叶")
            if pred is not None:
                if not pred["key"].encode("utf-8") < q:
                    raise ProofError("前驱键必须严格小于查询键（字节序）")
                _register_disclosed(disclosed, pred["index"], pred["key"].encode("utf-8"))
            if succ is not None:
                if not q < succ["key"].encode("utf-8"):
                    raise ProofError("后继键必须严格大于查询键（字节序）")
                _register_disclosed(disclosed, succ["index"], succ["key"].encode("utf-8"))
            if pred is not None and succ is not None:
                if not pred["key"].encode("utf-8") < succ["key"].encode("utf-8"):
                    raise ProofError("前驱、后继键次序矛盾")
                # 相邻下标 + 记录数：两片真实叶之间不存在任何遗漏叶。
                if succ["index"] != pred["index"] + 1:
                    raise ProofError("前驱与后继不是相邻叶，中间可能存在未证明的记录")
            elif pred is not None:
                # 边界缺失：前驱必须是最后一条真实记录。
                if pred["index"] != count - 1:
                    raise ProofError("查询键大于最大键时，前驱必须是末位真实叶")
            else:
                # 边界缺失：后继必须是第一条真实记录。
                assert succ is not None
                if succ["index"] != 0:
                    raise ProofError("查询键小于最小键时，后继必须是首位真实叶")
            conclusions.append(
                QueryConclusion(
                    q.decode("utf-8"),
                    False,
                    None,
                    None if pred is None else {"index": pred["index"], "key": pred["key"]},
                    None if succ is None else {"index": succ["index"], "key": succ["key"]},
                )
            )
        else:
            raise ProofError("答案的 present 标记必须是布尔值")

    # ---- 6. 全局一致性：下标序与键字节序必须严格同构 --------------
    ordered = sorted(disclosed.items())
    for (i1, k1), (i2, k2) in zip(ordered, ordered[1:]):
        if not k1 < k2:
            raise ProofError("披露叶的下标顺序与键字节序不一致")

    # ---- 7. 推导所需兄弟坐标，节点表必须恰好匹配 ------------------
    required: set[tuple[int, int]] = set()
    for idx in disclosed:
        required.update(sibling_path(idx, height))

    node_map: dict[tuple[int, int], bytes] = {}
    for node in nodes:
        if not isinstance(node, dict) or set(node.keys()) != {"level", "index", "digest_hex"}:
            raise ProofError("共享节点结构非法")
        level, idx, digest_hex = node["level"], node["index"], node["digest_hex"]
        if not isinstance(level, int) or isinstance(level, bool):
            raise ProofError("节点层号必须是整数")
        if not isinstance(idx, int) or isinstance(idx, bool):
            raise ProofError("节点下标必须是整数")
        if not isinstance(digest_hex, str):
            raise ProofError("节点摘要必须是十六进制字符串")
        try:
            digest = bytes.fromhex(digest_hex)
        except ValueError as exc:
            raise ProofError("节点摘要不是合法十六进制") from exc
        if len(digest) != DIGEST_SIZE:
            raise ProofError("节点摘要长度必须为 32 字节")
        if not 0 <= level < height:
            # 根节点（level == height）永远不应出现在证明里。
            raise ProofError(f"节点层号 {level} 越界，根节点不得入证")
        width = leaf_count >> level
        if not 0 <= idx < width:
            raise ProofError(f"节点 ({level},{idx}) 下标越界")
        coord = (level, idx)
        if coord in node_map:
            raise ProofError(f"共享节点 ({level},{idx}) 重复保存")
        node_map[coord] = digest

    if set(node_map) != required:
        unused = set(node_map) - required
        missing = required - set(node_map)
        if unused:
            raise ProofError(f"证明包含 {len(unused)} 个未使用的冗余节点")
        raise ProofError(f"证明缺少 {len(missing)} 个必要分支节点")

    # ---- 8. 叶摘要：level 0 兄弟必须与披露叶重算值一致 ------------
    leaf_digests: dict[int, bytes] = {}
    for q in query_bytes:
        answer = answer_by_query[q]
        if answer["present"]:
            leaves = [
                {"index": answer["index"], "key": answer["key"], "value": answer["value"]}
            ]
        else:
            leaves = [
                leaf for leaf in (answer["predecessor"], answer["successor"]) if leaf
            ]
        for leaf in leaves:
            leaf_digests[leaf["index"]] = leaf_digest(
                leaf["key"].encode("utf-8"), leaf["value"].encode("utf-8")
            )
    for idx, digest in leaf_digests.items():
        # 仅当 level 0 兄弟本身也是披露叶时可直接交叉核对；
        # 未披露兄弟的摘要在第 9 步根重建中被隐式核验。
        sibling = idx ^ 1
        if sibling in leaf_digests:
            if node_map[(0, sibling)] != leaf_digests[sibling]:
                raise ProofError(
                    f"叶层兄弟摘要与披露叶重算值不符（下标 {sibling}）"
                )

    # ---- 9. 从每个披露叶重建到可信根 ------------------------------
    for idx, leaf_sum in leaf_digests.items():
        cur = leaf_sum
        cur_idx = idx
        for level, sibling_idx in sibling_path(idx, height):
            sibling = node_map[(level, sibling_idx)]
            if cur_idx % 2 == 0:
                cur = internal_digest(cur, sibling)
            else:
                cur = internal_digest(sibling, cur)
            cur_idx //= 2
        if cur != manifest.root:
            raise ProofError(f"从叶 {idx} 重建的根与可信清单不符")

    return conclusions


def _register_disclosed(disclosed: dict[int, bytes], index: int, key: bytes) -> None:
    existing = disclosed.get(index)
    if existing is not None and existing != key:
        raise ProofError(f"同一叶下标 {index} 对应了互相矛盾的键")
    disclosed[index] = key
