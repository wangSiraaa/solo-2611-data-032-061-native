"""Avro 文件读取与 JSONL / 计划文件的原子写出。"""

from __future__ import annotations

import copy
import json
import os
import tempfile
from typing import Any

import fastavro

from .schema import load_json
from .resolver import CompatibilityReport, build_plan


def load_schemas(writer_path: str, reader_path: str):
    writer_schema = load_json(writer_path)
    reader_schema = load_json(reader_path)
    return writer_schema, reader_schema


def build_report(writer_path: str, reader_path: str) -> CompatibilityReport:
    writer_schema, reader_schema = load_schemas(writer_path, reader_path)
    return build_plan(writer_schema, reader_schema,
                      writer_schema_path=writer_path, reader_schema_path=reader_path)


def decode_avro(path: str) -> tuple[list[dict], dict]:
    """用 fastavro 解码整个文件；解码失败时异常直接向上抛，不产生任何输出文件。

    返回 (记录列表, fastavro 看到的 writer schema)。
    """
    with open(path, "rb") as fh:
        reader = fastavro.reader(fh)
        writer_schema = reader.writer_schema
        records = list(reader)
    return records, writer_schema


def normalize_schema(node: Any, *, is_root: bool = True) -> Any:
    """把 schema 归一化为可比较的纯结构。

    命名与 fastavro 写进 .avro 的内嵌 writer schema 对齐：根 record 用全名，
    嵌套内联 record 用短 name（fastavro 同样如此序列化）。
    同时兼容 fastavro 回填后把内联类型放进 ``type`` 键的形态。
    """
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return [normalize_schema(b, is_root=False) for b in node]
    kind = node["type"]
    if isinstance(kind, (dict, list)):  # fastavro 可能把内联类型放在 type 里
        return normalize_schema(kind, is_root=False)
    if kind == "array":
        return {"type": "array", "items": normalize_schema(node["items"], is_root=False)}
    if kind == "record":
        # 我们的节点用注入的 _fullname（继承 namespace 后计算）；
        # fastavro 回填的节点 name 本身就是全名
        fq = node.get("_fullname") or node["name"]
        return {
            "type": "record",
            "name": fq,
            "fields": [
                {"name": f["name"],
                 "type": normalize_schema(f["type"], is_root=False),
                 **({"aliases": f["aliases"]} if "aliases" in f else {}),
                 **({"default": copy.deepcopy(f["default"])} if "default" in f else {})}
                for f in node["fields"]
            ],
        }
    return kind


def _atomic_write(path: str, payload: str) -> None:
    """先写临时文件再原子替换；中途失败不会留下声称完成的残缺结果。"""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def render_jsonl(rows: list[dict]) -> str:
    return "".join(json.dumps(row, ensure_ascii=False, sort_keys=False) + "\n" for row in rows)


def render_plan(report: CompatibilityReport) -> str:
    return json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n"


def write_jsonl(path: str, rows: list[dict]) -> None:
    _atomic_write(path, render_jsonl(rows))


def write_plan(path: str, report: CompatibilityReport) -> None:
    _atomic_write(path, render_plan(report))


def write_outputs(*, out_path: str, rows: list[dict],
                  plan_path: str | None, report: CompatibilityReport) -> None:
    """先在内存渲染全部载荷，再依次原子替换落盘，失败可回滚。

    渲染期失败不触碰文件系统。落盘时若 JSONL 写出失败，已写出的计划文件
    会恢复到调用前的内容（原本不存在则删除），绝不留下孤立的半成品。
    """
    jsonl_payload = render_jsonl(rows)
    plan_payload = render_plan(report) if plan_path is not None else None

    plan_existed = plan_path is not None and os.path.exists(plan_path)
    plan_backup = None
    if plan_existed:
        with open(plan_path, "rb") as fh:
            plan_backup = fh.read()

    try:
        if plan_path is not None:
            _atomic_write(plan_path, plan_payload)
        _atomic_write(out_path, jsonl_payload)
    except BaseException:
        if plan_path is not None:
            try:
                if plan_existed:
                    _atomic_write(plan_path, plan_backup.decode("utf-8"))
                else:
                    os.unlink(plan_path)
            except OSError:
                pass
        raise
