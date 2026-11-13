"""测试共享工具：临时工作区、JSONL/查询文件写入。"""

from __future__ import annotations

import json
import os
import tempfile


class WorkDir:
    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="merkle-test-")

    def path(self, name: str) -> str:
        return os.path.join(self.dir, name)

    def write_jsonl(self, name: str, records: list[tuple[str, str]]) -> str:
        path = self.path(name)
        with open(path, "w", encoding="utf-8") as f:
            for key, value in records:
                f.write(json.dumps({"key": key, "value": value}, ensure_ascii=False))
                f.write("\n")
        return path

    def write_empty_jsonl(self, name: str) -> str:
        path = self.path(name)
        open(path, "w", encoding="utf-8").close()
        return path

    def write_queries(self, name: str, queries: list[str]) -> str:
        path = self.path(name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(queries, f, ensure_ascii=False)
        return path

    def write_json(self, name: str, obj) -> str:
        path = self.path(name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
        return path

    def read_json(self, name: str):
        with open(self.path(name), "r", encoding="utf-8") as f:
            return json.load(f)
