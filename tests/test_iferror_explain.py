"""IFERROR 两条捕获路径与 explain 失败来源的端到端测试。

路径一：被包裹表达式直接抛 FormulaError（如 1/0）；
路径二：被包裹引用的单元格本身存的是错误值（t="e" 或上游传播），
       此时不抛异常而是返回 error 类型的 CellValue，IFERROR 同样要捕获。
另验证：IFERROR 不掩盖非错误结果，explain 中失败格带 code+reason。
"""

from __future__ import annotations

import os
import zipfile

from lxml import etree
from openpyxl import Workbook

from xlcalc.engine import Engine
from xlcalc.ooxml import NS, NSMAP, read_workbook
from xlcalc.values import NUMBER, TEXT
from xlcalc.views import build_explain_view

from .builders import save_wb


def _inject_static_error(path: str, coord: str, code: str, row_num: int) -> None:
    part = "xl/worksheets/sheet1.xml"
    tmp = path + ".tmp"
    with zipfile.ZipFile(path, "r") as zin:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)
                if item.filename == part:
                    root = etree.fromstring(data)
                    sd = root.find("m:sheetData", namespaces=NSMAP)
                    row = sd.find(f"m:row[@r='{row_num}']", namespaces=NSMAP)
                    if row is None:
                        row = etree.SubElement(sd, f"{{{NS}}}row", r=str(row_num))
                    cell = etree.SubElement(row, f"{{{NS}}}c", r=coord, t="e")
                    v = etree.SubElement(cell, f"{{{NS}}}v")
                    v.text = code
                    data = etree.tostring(root, xml_declaration=False, encoding="UTF-8")
                zout.writestr(item, data)
    os.replace(tmp, path)


def test_iferror_catches_exception_path(tmp_path):
    path = str(tmp_path / "ie1.xlsx")
    wb = Workbook()
    ws = wb.active
    ws["A1"] = '=IFERROR(1/0,"回退")'
    save_wb(wb, path)
    results = Engine(read_workbook(path)).evaluate_all()
    v = results[("Sheet", 1, 1)].value
    assert v.type == TEXT and v.value == "回退"


def test_iferror_catches_stored_error_value_path(tmp_path):
    path = str(tmp_path / "ie2.xlsx")
    wb = Workbook()
    ws = wb.active
    ws["A2"] = "=A1+1"     # A1 是静态 #N/A -> A2 传播 #N/A
    ws["A3"] = '=IFERROR(A2,"已恢复")'  # 捕获传播来的错误值
    ws["A4"] = "=IFERROR(A1,99)"        # 直接捕获静态错误格
    save_wb(wb, path)
    _inject_static_error(path, "A1", "#N/A", 1)

    results = Engine(read_workbook(path)).evaluate_all()
    a2 = results[("Sheet", 1, 2)].value
    assert a2.is_error and a2.value == "#N/A"
    a3 = results[("Sheet", 1, 3)].value
    assert a3.type == TEXT and a3.value == "已恢复"
    a4 = results[("Sheet", 1, 4)].value
    assert a4.type == NUMBER and a4.value == 99


def test_iferror_passes_through_non_error(tmp_path):
    path = str(tmp_path / "ie3.xlsx")
    wb = Workbook()
    ws = wb.active
    ws["A1"] = 7
    ws["A2"] = '=IFERROR(A1+1,"回退")'  # 无错误，必须返回 8 而非回退
    save_wb(wb, path)
    results = Engine(read_workbook(path)).evaluate_all()
    assert results[("Sheet", 1, 2)].value.value == 8


def test_explain_carries_failure_code_and_reason(tmp_path):
    path = str(tmp_path / "ie4.xlsx")
    wb = Workbook()
    ws = wb.active
    ws["A1"] = "=1/0"  # 未捕获除零
    ws["A2"] = "=VLOOKUP(B1,B2:C3,2,FALSE)"  # 不支持函数
    save_wb(wb, path)

    wb_data = read_workbook(path)
    engine = Engine(wb_data)
    results = engine.evaluate_all()
    view = build_explain_view(wb_data, results)
    cells = view["sheets"][0]["cells"]

    a1 = cells["A1"]
    assert a1["status"] == "error"
    assert a1["failure"]["code"] == "#DIV/0!"
    assert "除数为零" in a1["failure"]["reason"]
    assert a1["dependencies"] == []  # 1/0 无单元格依赖

    a2 = cells["A2"]
    assert a2["status"] == "unsupported"
    assert a2["failure"]["code"] == "#UNSUPPORTED!"
    assert "VLOOKUP" in a2["failure"]["reason"]


def test_explain_lists_dependencies_for_chain(tmp_path):
    path = str(tmp_path / "ie5.xlsx")
    wb = Workbook()
    ws = wb.active
    ws["B1"] = 3
    ws["B2"] = 4
    ws["B3"] = "=B1+B2"
    ws["B4"] = "=B3*2"
    save_wb(wb, path)

    wb_data = read_workbook(path)
    results = Engine(wb_data).evaluate_all()
    view = build_explain_view(wb_data, results)
    b4 = view["sheets"][0]["cells"]["B4"]
    # B4 直接依赖 B3；B3 的依赖在它自己的 explain 里
    assert b4["dependencies"] == ["Sheet!B3"]
    assert b4["value"]["value"] == 14
    b3 = view["sheets"][0]["cells"]["B3"]
    assert b3["dependencies"] == ["Sheet!B1", "Sheet!B2"]
