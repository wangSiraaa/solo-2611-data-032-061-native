"""writer -> reader 方向的 Avro 模式解析计划。

先在 schema 级别证明兼容性（包括样例数据从未出现的 union 分支），
再产出一个与数据无关的解析计划，投影阶段只执行计划、不做临时判断。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from .schema import PRIMITIVES, fullname, validate_schema

# Avro 数值提升表：writer 原始类型 -> 允许的 reader 原始类型
PROMOTIONS: dict[str, frozenset[str]] = {
    "int": frozenset({"int", "long", "double"}),
    "long": frozenset({"long", "double"}),
    "double": frozenset({"double"}),
}


class CompatibilityError(Exception):
    """收集到的全部不兼容项（check 阶段一次性抛出，而非逐条消息报错）。"""

    def __init__(self, errors: list[dict[str, str]]):
        self.errors = errors
        super().__init__("; ".join(
            f"[{e['writer_path']} -> {e['reader_path']}] {e['reason']}" for e in errors))


@dataclass
class CompatibilityReport:
    compatible: bool
    errors: list[dict[str, str]]
    plan: dict[str, Any]
    writer_schema_path: str
    reader_schema_path: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "compatible": self.compatible,
            "writer_schema_path": self.writer_schema_path,
            "reader_schema_path": self.reader_schema_path,
            "errors": self.errors,
            "plan": self.plan,
        }


def describe(node: Any) -> Any:
    """把 schema 节点压成计划里使用的紧凑、可 JSON 序列化的类型描述。"""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return {"union": [describe(b) for b in node]}
    kind = node["type"]
    if kind == "array":
        return {"array": describe(node["items"])}
    if kind == "record":
        return {"record": node.get("_fullname") or fullname(node["name"], node.get("namespace"))}
    return kind


def _plan_has_failure(node: Any) -> bool:
    """递归检查计划树里是否存在失败留下的 None / 非 writer 兼容来源。"""
    if node is None:
        return True
    if isinstance(node, dict):
        kind = node.get("kind")
        if kind == "record":
            return any(_plan_has_failure(f.get("resolution"))
                       for f in node["fields"] if f["source"] == "writer")
        if kind == "union":
            return any(_plan_has_failure(b["resolution"]) for b in node["branches"])
        if kind == "union_match":
            return _plan_has_failure(node["resolution"])
        if kind == "array":
            return _plan_has_failure(node["items"])
    return False


def collect_records(schema: Any) -> dict[str, dict]:
    """收集 schema 内全部命名 record 的全名 -> 节点（含引用解析用）。"""
    out: dict[str, dict] = {}

    def scan(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "record":
                out[node["_fullname"]] = node
                for f in node["fields"]:
                    scan(f["type"])
            elif node.get("type") == "array":
                scan(node["items"])
        elif isinstance(node, list):
            for branch in node:
                scan(branch)

    scan(schema)
    return out


class _Resolver:
    """带路径与错误收集的 writer->reader 计划构建器。"""

    def __init__(self, writer_root: dict, reader_root: dict):
        self.w_records = collect_records(writer_root)
        self.r_records = collect_records(reader_root)
        self.errors: list[dict[str, str]] = []
        # 尝试性匹配（reader union 逐分支排除）期间的错误暂存于此，
        # 只有所有分支都排除时才合并进 self.errors
        self._suppressed: list[list[dict[str, str]]] = []

    def fail(self, wpath: str, rpath: str, reason: str) -> None:
        entry = {"writer_path": wpath, "reader_path": rpath, "reason": reason}
        if self._suppressed:
            self._suppressed[-1].append(entry)
        else:
            self.errors.append(entry)

    def _deref(self, node: Any, records: dict[str, dict]) -> Any:
        if isinstance(node, str) and node not in PRIMITIVES:
            return records[node]
        return node

    def resolve(self, writer: Any, reader: Any, wpath: str, rpath: str) -> dict | None:
        """带错误落账的解析入口；失败时向 self.errors 记录一条并返回 None。"""
        return self._probe(writer, reader, wpath, rpath)

    def _probe(self, writer: Any, reader: Any, wpath: str, rpath: str,
               *, tentative: bool = False) -> dict | None:
        """纯探测：成功返回计划节点，失败返回 None。

        tentative=True 表示这是 reader-union 的“排除性尝试”：期间产生的错误
        只进临时桶、不污染全局错误；调用成功时临时桶直接丢弃。
        非尝试模式下每个不兼容位置恰好落一条全局错误。
        """
        if tentative:
            bucket: list[dict[str, str]] = []
            self._suppressed.append(bucket)
            try:
                result = self._probe(writer, reader, wpath, rpath)
            finally:
                self._suppressed.pop()
            if result is None:
                # 把尝试期收集到的具体不兼容项带回给调用方，便于定位到字段级原因
                return None, bucket
            return result, []

        writer = self._deref(writer, self.w_records)
        reader = self._deref(reader, self.r_records)

        # writer 是 union：每个 writer 分支都必须被 reader 接收（覆盖样例未出现的分支）。
        # 必须先于 reader-union 分支处理，否则 union->union 会被当成整值匹配。
        if isinstance(writer, list):
            branches = []
            ok_all = True
            for idx, wbranch in enumerate(writer):
                wbranch_d = self._deref(wbranch, self.w_records)
                sub = self._probe(wbranch_d, reader, f"{wpath}[{idx}]", rpath)
                branches.append({"writer_branch": describe(wbranch_d), "resolution": sub})
                if sub is None:
                    ok_all = False
            if not ok_all:
                return None
            return {"kind": "union", "writer_type": describe(writer),
                    "reader_type": describe(reader), "branches": branches}

        # reader 是 union（writer 不是）：writer 整体能被任一 reader 分支接收即可；
        # 各分支的失败只是“排除尝试”，不产生全局错误条目
        if isinstance(reader, list):
            attempt_errors: list[dict[str, str]] = []
            for idx, rbranch in enumerate(reader):
                sub, bucket = self._probe(writer, rbranch, wpath,
                                          f"{rpath}[{idx}]", tentative=True)
                if sub is not None:
                    return {"kind": "union_match", "reader_index": idx,
                            "reader_branch": describe(self._deref(rbranch, self.r_records)),
                            "resolution": sub}
                attempt_errors.extend(bucket)
            if attempt_errors:
                # 排除 null/结构不匹配这类“整分支不适用”的噪音，
                # 只保留深入到字段/元素的具体不兼容原因；全是噪音时退回 union 总错误
                informative = [e for e in attempt_errors
                               if "结构不同" not in e["reason"]]
                for entry in (informative or attempt_errors):
                    self.fail(entry["writer_path"], entry["reader_path"], entry["reason"])
            else:
                self.fail(wpath, rpath,
                          f"reader union {describe(reader)} 没有分支可接收 writer 类型 "
                          f"{describe(writer)}（已排除分支 "
                          f"{[describe(self._deref(b, self.r_records)) for b in reader]}）")
            return None

        w_kind = writer if isinstance(writer, str) else writer["type"]
        r_kind = reader if isinstance(reader, str) else reader["type"]

        # 原始类型：只允许 Avro 规定的数值提升，禁止 Python 式隐式转换（如 string->int）
        if w_kind in PRIMITIVES:
            if r_kind not in PRIMITIVES:
                self.fail(wpath, rpath,
                          f"类型不兼容：writer {w_kind} 与 reader {describe(reader)} 结构不同")
                return None
            allowed = PROMOTIONS.get(w_kind, frozenset({w_kind}))
            if r_kind not in allowed:
                self.fail(wpath, rpath,
                          f"类型不兼容：writer {w_kind} 不能按 Avro 规则读为 reader {r_kind}"
                          "（不允许字符串等隐式转换）")
                return None
            return {"kind": "primitive", "writer_type": w_kind,
                    "reader_type": r_kind, "promoted": w_kind != r_kind}

        if w_kind == "array":
            if r_kind != "array":
                self.fail(wpath, rpath,
                          f"类型不兼容：writer array 与 reader {describe(reader)} 结构不同")
                return None
            sub = self._probe(writer["items"], reader["items"],
                              f"{wpath}.items", f"{rpath}.items")
            return {"kind": "array", "items": sub} if sub is not None else None

        if w_kind == "record":
            if r_kind != "record":
                self.fail(wpath, rpath,
                          f"类型不兼容：writer record 与 reader {describe(reader)} 结构不同")
                return None
            w_names = self._record_names(writer)
            r_names = self._record_names(reader)
            if not (w_names & r_names):
                self.fail(wpath, rpath,
                          f"record 名称不匹配：writer {describe(writer)} 与 reader {describe(reader)}")
                return None
            sub = self.resolve_record(writer, reader, wpath, rpath)
            # 子字段可能在 tentative 探测中失败（错误进了抑制桶），
            # 这里必须显式检查，不能把残缺 record 计划当成兼容
            if _plan_has_failure(sub):
                return None
            return sub

        self.fail(wpath, rpath, f"不支持的 writer 类型 {w_kind!r}")  # pragma: no cover
        return None

    @staticmethod
    def _record_names(node: dict) -> set[str]:
        fq = node.get("_fullname") or fullname(node["name"], node.get("namespace"))
        return {fq, *node.get("aliases", ())}

    def resolve_record(self, wnode: dict, rnode: dict, wpath: str, rpath: str) -> dict:
        w_fields = {f["name"]: f for f in wnode["fields"]}
        plan_fields: list[dict] = []
        claimed: set[str] = set()

        for i, rf in enumerate(rnode["fields"]):
            rf_path = f"{rpath}.fields[{i}]({rf['name']})"
            matches: list[str] = []
            if rf["name"] in w_fields:
                matches.append(rf["name"])
            for alias in rf.get("aliases", []):
                if alias in w_fields and alias not in matches:
                    matches.append(alias)

            entry: dict[str, Any] = {
                "reader_name": rf["name"],
                "reader_path": rf_path,
                "aliases": list(rf.get("aliases", [])),
            }

            if len(matches) > 1:
                # 同名字段与 alias 同时命中、或多个 alias 命中不同 writer 字段
                self.fail(f"{wpath}.fields({sorted(matches)})", rf_path,
                          f"alias 冲突：reader 字段 {rf['name']!r} 同时匹配多个 writer 字段 "
                          f"{sorted(matches)}")
                entry.update({"source": "conflict", "writer_path": None,
                              "writer_name": None, "resolution": None, "default": None})
                plan_fields.append(entry)
                continue

            if len(matches) == 1:
                wname = matches[0]
                wf = w_fields[wname]
                wf_index = wnode["fields"].index(wf)
                wf_path = f"{wpath}.fields[{wf_index}]({wname})"
                claimed.add(wname)
                sub = self.resolve(wf["type"], rf["type"],
                                   f"{wf_path}.type", f"{rf_path}.type")
                entry.update({"source": "writer", "writer_path": wf_path,
                              "writer_name": wname, "renamed": wname != rf["name"],
                              "resolution": sub, "default": None})
                plan_fields.append(entry)
                continue

            # reader 新字段：必须有合法 default。显式 null 与“字段缺失”在此严格区分：
            # 缺失字段走 default；writer 显式写出的 null 由类型计划处理，二者不混用。
            if "default" in rf:
                entry.update({
                    "source": "default",
                    "writer_path": None,
                    "writer_name": None,
                    "resolution": {
                        "kind": "default",
                        "reader_type": describe(rf["type"]),
                        "default_branch": 0 if isinstance(rf["type"], list) else None,
                    },
                    "default": copy.deepcopy(rf["default"]),
                })
            else:
                self.fail(wpath, rf_path,
                          f"reader 新字段 {rf['name']!r} 没有匹配的 writer 字段，且缺少合法 default")
                entry.update({"source": "missing_default", "writer_path": None,
                              "writer_name": None, "resolution": None, "default": None})
            plan_fields.append(entry)

        dropped = [name for name in w_fields if name not in claimed]
        return {
            "kind": "record",
            "name": rnode.get("_fullname") or fullname(rnode["name"], rnode.get("namespace")),
            "fields": plan_fields,
            "dropped_writer_fields": dropped,
        }


def build_plan(writer_schema: Any, reader_schema: Any,
               writer_schema_path: str = "$", reader_schema_path: str = "$") -> CompatibilityReport:
    """校验两个 schema 并建立完整解析计划，返回含全部错误的报告（不抛兼容异常）。"""
    # 深拷贝避免把校验期注入的 _fullname 写回调用方的 schema 对象
    w = copy.deepcopy(writer_schema)
    r = copy.deepcopy(reader_schema)
    validate_schema(w)
    validate_schema(r)

    resolver = _Resolver(w, r)
    plan = resolver.resolve_record(w, r, "$", "$")
    return CompatibilityReport(
        compatible=not resolver.errors,
        errors=resolver.errors,
        plan=plan,
        writer_schema_path=writer_schema_path,
        reader_schema_path=reader_schema_path,
    )


def assert_compatible(report: CompatibilityReport) -> None:
    if not report.compatible:
        raise CompatibilityError(report.errors)
