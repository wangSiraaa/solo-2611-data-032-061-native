"""受限 Avro 子集的 schema 加载与校验。

支持的类型：record、array、null、boolean、int、long、double、string，
以及恰好由两种不同支持类型组成的 union。允许内联命名 record。

明确拒绝：递归引用、enum、map、logicalType，以及 fixed/error 等其它 Avro 特性。
"""

from __future__ import annotations

import json
from typing import Any

# 支持的原始类型
PRIMITIVES = frozenset({"null", "boolean", "int", "long", "double", "string"})
# 显式拒绝的命名/复杂类型
UNSUPPORTED_COMPLEX = frozenset({"enum", "map", "fixed", "error"})

# int / long 的合法取值范围（用于 default 校验，default 必须遵守声明类型）
INT_MIN, INT_MAX = -(2**31), 2**31 - 1
LONG_MIN, LONG_MAX = -(2**63), 2**63 - 1


class SchemaError(ValueError):
    """schema 不在支持子集内或自身非法。"""


def load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def fullname(name: str, namespace: str | None) -> str:
    """按 Avro 全名规则计算 record 全名（点号开头视为绝对名）。"""
    if "." in name:
        return name
    return f"{namespace}.{name}" if namespace else name


def _type_key(node: Any) -> str:
    """union 分支去重用的结构键：原始类型按名字、record 按全名、array 按元素结构。"""
    if isinstance(node, str):
        return node
    kind = node["type"]
    if kind == "record":
        # 内联 record 在校验早期可能还没注入 _fullname，按名字现场计算
        fq = node.get("_fullname") or fullname(node["name"], node.get("namespace"))
        return f"record:{fq}"
    if kind == "array":
        return f"array:{_type_key(node['items'])}"
    raise SchemaError(f"不支持的 union 分支类型: {kind!r}")


def validate_schema(schema: Any, *, require_root_record: bool = True) -> None:
    """对整个 schema 做子集校验，非法时抛出 :class:`SchemaError`。

    校验内容包括：类型白名单、union 必须是两种不同支持类型、record 字段结构、
    内联 record 重名必须结构一致、禁止递归/前向引用、禁止 logicalType、
    字段 default 必须符合声明类型。
    """
    if not isinstance(schema, (dict, list, str)):
        raise SchemaError("schema 节点必须是字符串、对象或数组")

    known: dict[str, dict] = {}
    # 当前解析栈中的 record 全名，用于检出递归引用
    on_stack: set[str] = set()

    def walk(node: Any, path: str, enclosing_ns: str | None = None) -> None:
        # 字符串节点：原始类型，或对已定义 record 的引用
        if isinstance(node, str):
            if node in PRIMITIVES:
                return
            if node in UNSUPPORTED_COMPLEX:
                raise SchemaError(f"{path}: 不支持的类型 {node!r}")
            target = known.get(node)
            if target is None:
                raise SchemaError(
                    f"{path}: 未定义的类型引用 {node!r}（不支持前向/递归引用）"
                )
            if node in on_stack:
                raise SchemaError(f"{path}: 检测到对 {node!r} 的递归引用，禁止递归 record")
            return

        # union 节点
        if isinstance(node, list):
            if len(node) != 2:
                raise SchemaError(f"{path}: union 必须恰好包含两种不同类型，实际 {len(node)} 种")
            keys = set()
            for i, branch in enumerate(node):
                walk(branch, f"{path}[{i}]", enclosing_ns)
                key = _type_key(branch)
                if key in keys:
                    raise SchemaError(f"{path}: union 的两个分支类型重复（{key}）")
                keys.add(key)
            return

        if not isinstance(node, dict) or "type" not in node:
            raise SchemaError(f"{path}: 非法 schema 节点 {node!r}")

        if "logicalType" in node:
            raise SchemaError(f"{path}: 不支持 logicalType（{node['logicalType']!r}）")

        kind = node["type"]

        if isinstance(kind, str) and kind in PRIMITIVES:
            return  # 带额外键的原始类型包装（且无 logicalType），按原始类型接受
        if isinstance(kind, str) and kind in UNSUPPORTED_COMPLEX:
            raise SchemaError(f"{path}: 不支持的类型 {kind!r}")

        if kind == "array":
            if set(node) - {"type", "items"}:
                raise SchemaError(f"{path}: array 仅允许 type/items 键")
            walk(node["items"], f"{path}.items", enclosing_ns)
            return

        if kind == "record":
            name = node.get("name")
            if not isinstance(name, str) or not name:
                raise SchemaError(f"{path}: record 缺少合法 name")
            ns = node.get("namespace", enclosing_ns)
            if ns is not None and not isinstance(ns, str):
                raise SchemaError(f"{path}: namespace 必须是字符串")
            fq = fullname(name, ns)
            node["_fullname"] = fq
            node["_short_name"] = name

            if fq in on_stack:
                raise SchemaError(f"{path}: 检测到递归 record {fq!r}，禁止递归引用")
            if fq in known:
                if known[fq] != node:
                    raise SchemaError(f"{path}: 同名 record {fq!r} 的内联定义结构不一致")
                return
            known[fq] = node

            fields = node.get("fields")
            if not isinstance(fields, list) or not fields:
                raise SchemaError(f"{path}: record {fq!r} 必须包含非空 fields 数组")

            on_stack.add(fq)
            try:
                seen_names: set[str] = set()
                # 子 record 继承的是父 record 的 namespace，而非父 record 全名
                child_ns = node.get("namespace", enclosing_ns)
                for i, field in enumerate(fields):
                    validate_field(field, f"{path}.fields[{i}]", walk, known, child_ns)
                    fname = field["name"]
                    if fname in seen_names:
                        raise SchemaError(f"{path}.fields[{i}]: 字段名 {fname!r} 重复")
                    seen_names.add(fname)
            finally:
                on_stack.discard(fq)
            return

        if isinstance(kind, (dict, list)):
            raise SchemaError(f"{path}: 不支持内联嵌套的类型表达式作为 type 值")
        raise SchemaError(f"{path}: 未知/不支持的类型 {kind!r}")

    def validate_field(field: Any, path: str, walk, known, enclosing_ns: str | None) -> None:
        if not isinstance(field, dict) or not isinstance(field.get("name"), str):
            raise SchemaError(f"{path}: record 字段必须是含 name 的对象")
        if not field["name"]:
            raise SchemaError(f"{path}: 字段名不能为空")
        ftype = field.get("type")
        if ftype is None:
            raise SchemaError(f"{path}: 字段 {field['name']!r} 缺少 type")
        walk(ftype, f"{path}.type", enclosing_ns)
        aliases = field.get("aliases")
        if aliases is not None:
            if not isinstance(aliases, list) or not all(isinstance(a, str) for a in aliases):
                raise SchemaError(f"{path}: aliases 必须是字符串数组")
        if "default" in field:
            validate_default(field["default"], ftype, f"{path}.default", known)
        for extra in ("order",):
            if extra in field:
                raise SchemaError(f"{path}: 不支持字段属性 {extra!r}")

    root_path = "$"
    if require_root_record and not (isinstance(schema, dict) and schema.get("type") == "record"):
        raise SchemaError(f"{root_path}: 根 schema 必须是 record 类型")
    walk(schema, root_path)


def validate_default(value: Any, declared: Any, path: str, known: dict) -> None:
    """校验字段 default 遵守其声明类型（含 union 取首分支、容器逐元素校验）。"""
    if isinstance(declared, list):
        # Avro 规定 union 字段的 default 必须匹配第一个分支
        validate_default(value, declared[0], f"{path}[0]", known)
        return
    if isinstance(declared, str):
        tname = declared
    else:
        tname = declared["type"]

    if tname == "null":
        if value is not None:
            raise SchemaError(f"{path}: null 字段的 default 必须是 null")
    elif tname == "boolean":
        if not isinstance(value, bool):
            raise SchemaError(f"{path}: boolean 字段的 default 必须是布尔值")
    elif tname == "int":
        if not isinstance(value, int) or isinstance(value, bool) or not (INT_MIN <= value <= INT_MAX):
            raise SchemaError(f"{path}: int 字段的 default 必须是 32 位整数")
    elif tname == "long":
        if not isinstance(value, int) or isinstance(value, bool) or not (LONG_MIN <= value <= LONG_MAX):
            raise SchemaError(f"{path}: long 字段的 default 必须是 64 位整数")
    elif tname == "double":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SchemaError(f"{path}: double 字段的 default 必须是数值")
    elif tname == "string":
        if not isinstance(value, str):
            raise SchemaError(f"{path}: string 字段的 default 必须是字符串")
    elif tname == "array":
        if not isinstance(value, list):
            raise SchemaError(f"{path}: array 字段的 default 必须是数组")
        for i, item in enumerate(value):
            validate_default(item, declared["items"], f"{path}[{i}]", known)
    elif tname == "record":
        if not isinstance(value, dict):
            raise SchemaError(f"{path}: record 字段的 default 必须是对象")
        target = known.get(declared.get("_fullname", ""), declared)
        for fdef in target["fields"]:
            if fdef["name"] not in value:
                if "default" not in fdef:
                    raise SchemaError(
                        f"{path}: record default 缺少必填字段 {fdef['name']!r} 且该字段无 default"
                    )
                continue
            validate_default(value[fdef["name"]], fdef["type"],
                             f"{path}.{fdef['name']}", known)
        extra = set(value) - {f["name"] for f in target["fields"]}
        if extra:
            raise SchemaError(f"{path}: record default 含未知字段 {sorted(extra)!r}")
    else:
        raise SchemaError(f"{path}: 类型 {tname!r} 不支持 default")
