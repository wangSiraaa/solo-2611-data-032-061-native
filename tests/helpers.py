"""测试辅助：在临时目录构造 .avsc/.avro 文件。"""

from __future__ import annotations

import json
import os
from typing import Any

import fastavro


def write_avsc(directory: str, name: str, schema: Any) -> str:
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(schema, fh, ensure_ascii=False)
    return path


def write_avro(directory: str, name: str, schema: Any, records: list[dict]) -> str:
    path = os.path.join(directory, name)
    parsed = fastavro.parse_schema(copy_schema(schema))
    with open(path, "wb") as fh:
        fastavro.writer(fh, parsed, records)
    return path


def copy_schema(schema: Any) -> Any:
    return json.loads(json.dumps(schema))


# ---- 常用 schema 构件 -------------------------------------------------------

def rec(name: str, fields: list[dict], namespace: str | None = "demo",
        aliases: list[str] | None = None) -> dict:
    node: dict[str, Any] = {"type": "record", "name": name, "fields": fields}
    if namespace is not None:
        node["namespace"] = namespace
    if aliases is not None:
        node["aliases"] = aliases
    return node


def field(name: str, ftype: Any, **kw) -> dict:
    node = {"name": name, "type": ftype}
    node.update(kw)
    return node


def arr(items: Any) -> dict:
    return {"type": "array", "items": items}
