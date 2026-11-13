"""OOXML 读取路径测试：共享字符串 t="s"、静态错误 t="e"、布尔 t="b"、
富文本 si、以及不含公式的纯静态工作簿。"""

from __future__ import annotations

import os
import zipfile

from lxml import etree

from xlcalc.engine import Engine
from xlcalc.ooxml import NS, NSMAP, read_workbook
from xlcalc.values import BOOLEAN, ERROR, NUMBER, TEXT


def _add_shared_strings(path: str, strings: list[str]) -> None:
    ss = etree.Element(f"{{{NS}}}sst", count=str(len(strings)),
                       uniqueCount=str(len(strings)))
    for s in strings:
        si = etree.SubElement(ss, f"{{{NS}}}si")
        t = etree.SubElement(si, f"{{{NS}}}t")
        t.text = s
    data = etree.tostring(ss, xml_declaration=False, encoding="UTF-8")
    tmp = path + ".tmp"
    with zipfile.ZipFile(path, "r") as zin:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                zout.writestr(item, zin.read(item.filename))
            zout.writestr("xl/sharedStrings.xml", data)
    os.replace(tmp, path)


def _rewrite_cells_to_shared_string_indexes(path: str, mapping: dict[str, int]) -> None:
    part = "xl/worksheets/sheet1.xml"
    tmp = path + ".tmp"
    with zipfile.ZipFile(path, "r") as zin:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)
                if item.filename == part:
                    root = etree.fromstring(data)
                    for coord, idx in mapping.items():
                        cell = root.xpath(".//m:c[@r=$c]", c=coord, namespaces=NSMAP)[0]
                        for ch in list(cell):
                            cell.remove(ch)
                        cell.set("t", "s")
                        v = etree.SubElement(cell, f"{{{NS}}}v")
                        v.text = str(idx)
                    data = etree.tostring(root, xml_declaration=False, encoding="UTF-8")
                zout.writestr(item, data)
    os.replace(tmp, path)


def test_shared_strings_resolved(tmp_path):
    from openpyxl import Workbook

    path = str(tmp_path / "ss.xlsx")
    wb = Workbook()
    ws = wb.active
    ws.title = "S"
    ws["A1"] = "苹果"
    ws["A2"] = "香蕉"
    ws["A3"] = "苹果"  # 重复，共享字符串表去重
    wb.save(path)

    strings = ["苹果", "香蕉"]
    _add_shared_strings(path, strings)
    _rewrite_cells_to_shared_string_indexes(path, {"A1": 0, "A2": 1, "A3": 0})
    wb_data = read_workbook(path)
    assert wb_data.shared_strings == strings
    results = Engine(wb_data).evaluate_all()
    assert results[("S", 1, 1)].value.value == "苹果"
    assert results[("S", 1, 2)].value.value == "香蕉"
    assert results[("S", 1, 3)].value.value == "苹果"
    assert results[("S", 1, 1)].value.type == TEXT


def test_static_error_and_boolean_cells(tmp_path):
    from openpyxl import Workbook

    path = str(tmp_path / "static.xlsx")
    wb = Workbook()
    ws = wb.active
    ws.title = "S"
    ws["A1"] = True
    ws["A2"] = False
    ws["A3"] = 3.5
    wb.save(path)

    # 注入一个静态错误格 A4 t="e"
    part = "xl/worksheets/sheet1.xml"
    tmp = path + ".tmp"
    with zipfile.ZipFile(path, "r") as zin:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)
                if item.filename == part:
                    root = etree.fromstring(data)
                    sd = root.find("m:sheetData", namespaces=NSMAP)
                    row4 = sd.find("m:row[@r='4']", namespaces=NSMAP)
                    if row4 is None:
                        row4 = etree.SubElement(sd, f"{{{NS}}}row", r="4")
                    a4 = etree.SubElement(row4, f"{{{NS}}}c", r="A4", t="e")
                    v = etree.SubElement(a4, f"{{{NS}}}v")
                    v.text = "#N/A"
                    data = etree.tostring(root, xml_declaration=False, encoding="UTF-8")
                zout.writestr(item, data)
    os.replace(tmp, path)

    results = Engine(read_workbook(path)).evaluate_all()
    assert results[("S", 1, 1)].value.type == BOOLEAN
    assert results[("S", 1, 1)].value.value is True
    assert results[("S", 1, 2)].value.value is False
    assert results[("S", 1, 3)].value.type == NUMBER
    a4 = results[("S", 1, 4)].value
    assert a4.type == ERROR and a4.value == "#N/A"


def test_static_error_propagates_through_formula(tmp_path):
    from openpyxl import Workbook

    path = str(tmp_path / "prop.xlsx")
    wb = Workbook()
    ws = wb.active
    ws.title = "S"
    ws["B1"] = "=A4+1"  # A4 将是静态错误
    wb.save(path)
    part = "xl/worksheets/sheet1.xml"
    tmp = path + ".tmp"
    with zipfile.ZipFile(path, "r") as zin:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)
                if item.filename == part:
                    root = etree.fromstring(data)
                    sd = root.find("m:sheetData", namespaces=NSMAP)
                    row4 = sd.find("m:row[@r='4']", namespaces=NSMAP)
                    if row4 is None:
                        row4 = etree.SubElement(sd, f"{{{NS}}}row", r="4")
                    a4 = etree.Element(f"{{{NS}}}c", r="A4", t="e")
                    v = etree.SubElement(a4, f"{{{NS}}}v")
                    v.text = "#REF!"
                    row4.append(a4)
                    data = etree.tostring(root, xml_declaration=False, encoding="UTF-8")
                zout.writestr(item, data)
    os.replace(tmp, path)

    results = Engine(read_workbook(path)).evaluate_all()
    b1 = results[("S", 2, 1)].value
    assert b1.is_error and b1.value == "#REF!"
