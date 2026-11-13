"""结果副本写入：只更新公式格的 OOXML 值缓存与类型。

严格约束：
- 公式节点 <f> 原样保留（共享主格文本、跟随格空节点、ref/si 全不动），
  绝不把公式替换成常数；
- 合并区域、样式 s、工作表顺序、所有其他部件（styles/theme/docProps…）
  原样复制，不经过 openpyxl 重新保存（避免缓存被清空、部件被重排）；
- 仅重写 worksheet XML 中公式格的 <v>/<t>：
    数字 -> 删除 t，写 <v>规范化数字</v>
    布尔 -> t="b"，<v>1/0</v>
    文本 -> t="str"（公式字符串缓存），<v>文本</v>
    错误 -> t="e"，<v>错误码</v>
    空白 -> 删除 <v> 且不写 t（公式结果为空白的情形，本子集基本不产生）
"""

from __future__ import annotations

import zipfile

from lxml import etree

from .engine import CellResult
from .ooxml import NS, NSMAP, WorkbookData, split_coord
from .values import BLANK, BOOLEAN, EMPTY_STRING, ERROR, NUMBER, TEXT


def write_result_copy(
    source_path: str,
    dest_path: str,
    wb: WorkbookData,
    results: dict,
) -> dict:
    """生成结果副本，返回写入统计。"""
    stats = {"formula_cells": 0, "errors": 0, "unsupported": 0, "updated": 0}
    sheet_paths = {name: wb.sheets[name].path for name in wb.sheet_order}

    with zipfile.ZipFile(source_path, "r") as zin:
        with zipfile.ZipFile(dest_path, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)
                if item.filename in sheet_paths.values():
                    sheet_name = next(
                        n for n, p in sheet_paths.items() if p == item.filename
                    )
                    data, local_stats = _rewrite_sheet(
                        data, sheet_name, wb, results
                    )
                    for k, v in local_stats.items():
                        stats[k] += v
                zout.writestr(item, data)
    return stats


def _rewrite_sheet(
    xml_bytes: bytes, sheet_name: str, wb: WorkbookData, results: dict
) -> tuple[bytes, dict]:
    stats = {"formula_cells": 0, "errors": 0, "unsupported": 0, "updated": 0}
    parser = etree.XMLParser(remove_blank_text=False)
    root = etree.fromstring(xml_bytes, parser)

    for c_el in root.findall("m:sheetData/m:row/m:c", NSMAP):
        f_el = c_el.find("m:f", NSMAP)
        if f_el is None:
            continue  # 静态格一律不碰
        stats["formula_cells"] += 1
        coord = c_el.get("r")
        col, row = split_coord(coord)
        result: CellResult = results[(sheet_name, col, row)]
        _write_cache(c_el, result)
        stats["updated"] += 1
        if result.value.is_error:
            stats["errors"] += 1
            if result.status == "unsupported":
                stats["unsupported"] += 1

    # 不写 XML 声明，与源文件风格一致；lxml 默认 utf-8
    out = etree.tostring(root, xml_declaration=False, encoding="UTF-8", standalone=None)
    return out, stats


def _write_cache(c_el: etree._Element, result: CellResult) -> None:
    v_el = c_el.find("m:v", NSMAP)
    vtype = result.value.type

    if vtype == BLANK:
        if v_el is not None:
            c_el.remove(v_el)
        if "t" in c_el.attrib:
            del c_el.attrib["t"]
        return

    if v_el is None:
        v_el = etree.SubElement(c_el, f"{{{NS}}}v")
    # 元素顺序要求 f 在 v 前；SubElement 追加到末尾，worksheet 单元格
    # 内 f 之后通常只有 v，故安全。防御性重排一次。
    f_el = c_el.find("m:f", NSMAP)
    if f_el is not None and list(c_el).index(v_el) < list(c_el).index(f_el):
        c_el.remove(v_el)
        f_el.addnext(v_el)

    if vtype == NUMBER:
        c_el.attrib.pop("t", None)
        v_el.text = _format_number(float(result.value.value))  # type: ignore[arg-type]
    elif vtype == BOOLEAN:
        c_el.set("t", "b")
        v_el.text = "1" if result.value.value else "0"
    elif vtype in (TEXT, EMPTY_STRING):
        c_el.set("t", "str")
        v_el.text = str(result.value.value)
    elif vtype == ERROR:
        c_el.set("t", "e")
        v_el.text = str(result.value.value)
    else:  # pragma: no cover - 六类值已穷尽
        raise RuntimeError(f"未知值类型 {vtype}")


def _format_number(x: float) -> str:
    if x == int(x) and abs(x) < 1e16:
        return str(int(x))
    return repr(x)
