"""规范 JSON 报告组装与序列化。

稳定性保证（重复输入 → 字节级相同报告）：
- 所有列表按明确规则排序（事务编号、seq、边的端点、证据的 from_seq…）；
- 字典按固定键顺序手工组装，不依赖插入时序的偶然结果；
- 不含时间戳、机器名、随机顺序等非确定性内容（运行耗时只打到控制台）；
- 统一 UTF-8、indent=2、键名不做字母重排以保持语义分组，
  序列化参数固定并以 '\n' 结尾。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from . import SCHEMA_VERSION
from .analysis import Analysis
from .model import History, SemanticError

CANONICAL_JSON_KWARGS: dict[str, Any] = {
    "ensure_ascii": False,
    "indent": 2,
    "separators": (",", ": "),
}


def canonical_dumps(obj: Any) -> str:
    return json.dumps(obj, **CANONICAL_JSON_KWARGS) + "\n"


def fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_error_report(history_id: str, input_fingerprint: str,
                       errors: list[SemanticError], input_summary: dict[str, Any]) -> dict[str, Any]:
    """输入语义不合法时的报告：明确不进入可串行化判定。"""
    return {
        "schema_version": SCHEMA_VERSION,
        "history_id": history_id,
        "input_fingerprint_sha256": input_fingerprint,
        "input_summary": input_summary,
        "status": "input_semantic_error",
        "errors": [e.to_dict() for e in sorted(
            errors, key=lambda e: (e.seq if e.seq is not None else -1, e.code))],
        "serializability_note": "存在输入语义错误，不进入可串行化判定",
        "view_serializability": None,
        "conflict_graph": None,
        "verdict": None,
        "independent_verification": None,
    }


def build_report(history: History, analysis: Analysis, input_fingerprint: str,
                 verification: dict[str, Any] | None, include_all_perms: bool) -> dict[str, Any]:
    view = analysis.view
    graph = analysis.graph
    return {
        "schema_version": SCHEMA_VERSION,
        "history_id": history.history_id,
        "input_fingerprint_sha256": input_fingerprint,
        "input_summary": {
            "n_transactions": len(history.tids),
            "transactions": history.tids,
            "n_keys": len(history.keys),
            "keys": sorted(history.keys),
            "n_operations": len(history.ops),
            "n_writes": sum(1 for o in history.ops if o.kind == "write"),
            "n_reads": sum(1 for o in history.ops if o.kind == "read"),
            "n_commits": sum(1 for o in history.ops if o.kind == "commit"),
        },
        "status": "ok",
        "errors": [],
        "read_from": [
            {
                "seq": op.seq,
                "transaction": op.tid,
                "key": op.key,
                "value": op.value,
                "read_from": op.read_from,
            }
            for op in sorted(
                (o for o in history.ops if o.kind == "read"),
                key=lambda o: o.seq,
            )
        ],
        "final_writers": [
            {"key": k, "final_write_id": history.final_writer[k]}
            for k in sorted(history.keys)
        ],
        "view_serializability": {
            "definition": "事务整段按原序连续执行；read 来源 write 身份与最终写入者均与原历史一致",
            **view.to_dict(include_all=include_all_perms),
        },
        "conflict_graph": graph.to_dict(),
        "verdict": {
            "view_serializable": analysis.view_serializable,
            "conflict_serializable": analysis.conflict_serializable,
            "note": "冲突图有环仅否定冲突可串行化；视图可串行化独立枚举判定",
        },
        "independent_verification": verification,
    }
