"""按解析计划投影已解码的 Avro 记录。

投影只执行 check 阶段定下的计划，运行期不再做兼容性判断；
遇到与计划不符的数据直接抛 :class:`ProjectionError`，绝不静默转换。
"""

from __future__ import annotations

import copy
from typing import Any

from .schema import INT_MAX, INT_MIN, LONG_MAX, LONG_MIN
from .resolver import CompatibilityReport, assert_compatible


class ProjectionError(RuntimeError):
    """数据与已通过预检的计划不符（理论上不应发生，出现即 fail-fast）。"""


def _check_number(value: Any, reader_type: str, path: str) -> Any:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProjectionError(f"{path}: 期望 {reader_type}，实际值 {value!r} 不是整数")
    if reader_type == "int" and not (INT_MIN <= value <= INT_MAX):
        raise ProjectionError(f"{path}: 值 {value} 超出 int 范围")
    if reader_type == "long" and not (LONG_MIN <= value <= LONG_MAX):
        raise ProjectionError(f"{path}: 值 {value} 超出 long 范围")
    return value


def _apply_primitive(node: dict, value: Any, path: str) -> Any:
    r = node["reader_type"]
    if r == "null":
        if value is not None:
            raise ProjectionError(f"{path}: 期望 null，实际 {value!r}")
        return None
    if r == "boolean":
        if not isinstance(value, bool):
            raise ProjectionError(f"{path}: 期望 boolean，实际 {value!r}")
        return value
    if r == "string":
        if not isinstance(value, str):
            # 计划已禁止 string<->number，这里是防御性检查
            raise ProjectionError(f"{path}: 期望 string，实际 {value!r}")
        return value
    if r in ("int", "long"):
        return _check_number(value, r, path)
    if r == "double":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ProjectionError(f"{path}: 期望 double，实际 {value!r}")
        return float(value) if node.get("promoted") else value
    raise ProjectionError(f"{path}: 未知原始类型 {r!r}")  # pragma: no cover


def _union_branch_index(branches: list[dict], value: Any, path: str) -> int:
    """按运行期 Python 值选择 writer union 分支（分支类型两两不同）。"""
    def matches(desc: Any) -> bool:
        if desc == "null":
            return value is None
        if desc == "boolean":
            return isinstance(value, bool)
        if desc == "string":
            return isinstance(value, str)
        if desc in ("int", "long", "double"):
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        if isinstance(desc, dict) and "array" in desc:
            return isinstance(value, list)
        if isinstance(desc, dict) and "record" in desc:
            return isinstance(value, dict)
        return False  # pragma: no cover

    numeric_order = {"int": 0, "long": 1, "double": 2}
    candidates = [i for i, b in enumerate(branches) if matches(b["writer_branch"])]
    if not candidates:
        raise ProjectionError(f"{path}: union 值 {value!r} 不匹配任何声明分支")
    if len(candidates) > 1:
        # int/long/double 在 Python 中同为 int/float：按 int -> long -> double 消歧
        candidates.sort(
            key=lambda i: numeric_order.get(branches[i]["writer_branch"], 9))
    return candidates[0]


def apply_node(node: dict | None, value: Any, path: str) -> Any:
    """按计划节点转换单个值；不修改入参，只构造新对象。"""
    if node is None:
        raise ProjectionError(f"{path}: 该位置在预检中不兼容，无法投影")
    kind = node["kind"]

    if kind == "primitive":
        return _apply_primitive(node, value, path)

    if kind == "array":
        if not isinstance(value, list):
            raise ProjectionError(f"{path}: 期望 array，实际 {value!r}")
        return [apply_node(node["items"], item, f"{path}[{i}]")
                for i, item in enumerate(value)]

    if kind == "union_match":
        # writer 不是 union、reader 是 union：值走 reader 命中分支的子计划
        return apply_node(node["resolution"], value, path)

    if kind == "union":
        idx = _union_branch_index(node["branches"], value, path)
        return apply_node(node["branches"][idx]["resolution"], value, path)

    if kind == "record":
        return _apply_record(node, value, path)

    raise ProjectionError(f"{path}: 未知计划节点 {kind!r}")  # pragma: no cover


def _apply_record(node: dict, value: Any, path: str) -> dict:
    if not isinstance(value, dict):
        raise ProjectionError(f"{path}: 期望 record，实际 {value!r}")
    out: dict[str, Any] = {}
    for entry in node["fields"]:
        rname = entry["reader_name"]
        fpath = f"{path}.{rname}"
        source = entry["source"]
        if source == "writer":
            wname = entry["writer_name"]
            if wname not in value:
                # Avro 解码后缺失字段通常已由 fastavro 填默认；此处严格区分
                # “字段缺失”与显式 null，不擅自用 reader default 顶替
                raise ProjectionError(
                    f"{fpath}: writer 记录缺少字段 {wname!r}，且该字段不是 reader 新字段")
            out[rname] = apply_node(entry["resolution"], value[wname], fpath)
        elif source == "default":
            # 新字段：无论 writer 是否碰巧存在同名键，都取声明 default
            out[rname] = copy.deepcopy(entry["default"])
        else:
            raise ProjectionError(f"{fpath}: 字段在预检中状态为 {source!r}，无法投影")
    return out


def project_records(report: CompatibilityReport, records: list[dict]) -> list[dict]:
    """预检通过后把一批已解码记录投影为 reader 字段顺序的新记录。

    - 保留输入顺序（原记录序号由调用方随索引保留）；
    - 不修改任何源记录，输出全部为新建对象；
    - 任一记录投影失败都抛异常，不返回半成品。
    """
    assert_compatible(report)
    plan = report.plan
    projected: list[dict] = []
    for idx, record in enumerate(records):
        if not isinstance(record, dict):
            raise ProjectionError(f"记录 #{idx}: 根 record 期望对象，实际 {type(record).__name__}")
        projected.append(_apply_record(plan, record, f"$[{idx}]"))
    return projected
