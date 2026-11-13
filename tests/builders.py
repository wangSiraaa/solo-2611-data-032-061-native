"""测试辅助：用 openpyxl 生成小工作簿，再用 lxml 注入 openpyxl 不支持
或不易表达的 OOXML 结构（共享公式跟随格、数组公式、外部链接部件等）。

所有产物都写到 tmp_path，绝不触碰 fixtures/。
"""

from __future__ import annotations

import os
import zipfile

from lxml import etree
from openpyxl import Workbook

NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
NSMAP = {"m": NS}


def save_wb(wb: Workbook, path: str) -> str:
    wb.save(path)
    return path


def new_wb(sheets: list[str]) -> Workbook:
    wb = Workbook()
    first = wb.active
    first.title = sheets[0]
    for name in sheets[1:]:
        wb.create_sheet(name)
    return wb


def _sheet_xml_path(path: str, sheet_index_1based: int) -> str:
    return f"xl/worksheets/sheet{sheet_index_1based}.xml"


def _edit_sheet_xml(path: str, sheet_index_1based: int, mutator) -> None:
    """读出 worksheet XML，交给 mutator(root) 原地修改后写回 ZIP。"""
    part = _sheet_xml_path(path, sheet_index_1based)
    tmp = path + ".tmp"
    with zipfile.ZipFile(path, "r") as zin:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)
                if item.filename == part:
                    root = etree.fromstring(data)
                    mutator(root)
                    data = etree.tostring(root, xml_declaration=False, encoding="UTF-8")
                zout.writestr(item, data)
    os.replace(tmp, path)


def set_static_inline_string(path: str, sheet_index_1based: int,
                             coord: str, text: str) -> None:
    """对外入口：把指定格改写为 inlineStr 文本（不经 openpyxl 公式识别）。"""
    _edit_sheet_xml(
        path, sheet_index_1based,
        lambda root: set_inline_string(root, coord, text),
    )


def find_cell(root, coord: str) -> etree._Element:
    cells = root.xpath(".//m:c[@r=$c]", c=coord, namespaces=NSMAP)
    assert cells, "找不到单元格 " + coord
    return cells[0]


def ensure_cell(root, coord: str) -> etree._Element:
    """openpyxl 不写 None 空单元格；需要时按 row 结构补一个 <c r=...>。"""
    existing = root.xpath(".//m:c[@r=$c]", c=coord, namespaces=NSMAP)
    if existing:
        return existing[0]
    import re

    row_num = int(re.match(r"[A-Z]+([0-9]+)", coord).group(1))
    sheet_data = root.find(f"{{{NS}}}sheetData")
    row_el = sheet_data.find(f"m:row[@r='{row_num}']", namespaces=NSMAP)
    if row_el is None:
        row_el = etree.SubElement(sheet_data, f"{{{NS}}}row", r=str(row_num))
    cell = etree.Element(f"{{{NS}}}c", r=coord)
    row_el.append(cell)
    return cell


def set_formula(cell_el: etree._Element, text: str, *,
                ftype: str | None = None, si: str | None = None,
                ref: str | None = None, drop_text: bool = False) -> etree._Element:
    """把单元格改写为公式格（可指定共享/数组类型），并清空旧值缓存。"""
    for child in list(cell_el):
        cell_el.remove(child)
    f = etree.SubElement(cell_el, f"{{{NS}}}f")
    if ftype is not None:
        f.set("t", ftype)
    if si is not None:
        f.set("si", si)
    if ref is not None:
        f.set("ref", ref)
    if not drop_text:
        f.text = text
    v = etree.SubElement(cell_el, f"{{{NS}}}v")
    v.text = "9999"  # 故意陈旧的缓存
    return cell_el


def set_inline_string(root, coord: str, text: str) -> None:
    """把单元格写成 inlineStr 静态文本（用于以 = 开头、不能被当公式的文本）。"""
    cell = ensure_cell(root, coord)
    for child in list(cell):
        cell.remove(child)
    cell.set("t", "inlineStr")
    is_el = etree.SubElement(cell, f"{{{NS}}}is")
    t_el = etree.SubElement(is_el, f"{{{NS}}}t")
    t_el.text = text


def make_shared_formula(path: str, sheet_index_1based: int,
                        master_coord: str, follower_coords: list[str],
                        formula_text: str, range_ref: str, si: str = "0") -> None:
    """注入一组共享公式：主格带文本与 ref，跟随格只有 si。"""
    def mutator(root):
        master = ensure_cell(root, master_coord)
        set_formula(master, formula_text, ftype="shared", si=si, ref=range_ref)
        for coord in follower_coords:
            follower = ensure_cell(root, coord)
            set_formula(follower, "", ftype="shared", si=si, drop_text=True)
    _edit_sheet_xml(path, sheet_index_1based, mutator)


def make_array_formula(path: str, sheet_index_1based: int,
                       coord: str, formula_text: str, range_ref: str) -> None:
    def mutator(root):
        cell = ensure_cell(root, coord)
        set_formula(cell, formula_text, ftype="array", ref=range_ref)
    _edit_sheet_xml(path, sheet_index_1based, mutator)


def add_external_link(path: str) -> None:
    """注入最小外部链接部件与关系，使工作簿出现 externalLink。"""
    ext_xml = (
        f'<externalLink xmlns="{NS}">'
        f"<externalBook><sheetNames><sheetName val=\"Other\"/></sheetNames>"
        f"</externalBook></externalLink>"
    )
    tmp = path + ".tmp"
    with zipfile.ZipFile(path, "r") as zin:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)
                if item.filename == "xl/_rels/workbook.xml.rels":
                    root = etree.fromstring(data)
                    rel = etree.SubElement(root, f"{{{PKG_REL}}}Relationship")
                    rel.set("Id", "rIdExt1")
                    rel.set(
                        "Type",
                        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/externalLink",
                    )
                    rel.set("Target", "externalLinks/externalLink1.xml")
                    data = etree.tostring(root, xml_declaration=False, encoding="UTF-8")
                zout.writestr(item, data)
            zout.writestr("xl/externalLinks/externalLink1.xml", ext_xml)
    os.replace(tmp, path)


def add_defined_name(path: str, name: str, refers_to: str) -> None:
    """在 workbook.xml 注入命名区域。"""
    tmp = path + ".tmp"
    with zipfile.ZipFile(path, "r") as zin:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)
                if item.filename == "xl/workbook.xml":
                    root = etree.fromstring(data)
                    dn = root.find(f"{{{NS}}}definedNames")
                    if dn is None:
                        dn = etree.Element(f"{{{NS}}}definedNames")
                        # 必须位于 sheets 之后、calcPr 之前
                        sheets = root.find(f"{{{NS}}}sheets")
                        sheets.addnext(dn)
                    d = etree.SubElement(dn, f"{{{NS}}}definedName")
                    d.set("name", name)
                    d.text = refers_to
                    data = etree.tostring(root, xml_declaration=False, encoding="UTF-8")
                zout.writestr(item, data)
    os.replace(tmp, path)
