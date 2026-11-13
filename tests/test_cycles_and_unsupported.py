"""循环引用定位与不支持特性的显式错误。

- 表内/跨表循环必须落到具体单元格，错误码 #CIRC! 且原因含完整引用链；
- 循环之外的独立单元格仍可正常求值；
- 命名区域、外部链接、数组公式、其他函数（VLOOKUP/POWER）一律
  #UNSUPPORTED!，带可读原因，绝不读取旧缓存。
"""

from __future__ import annotations

from openpyxl import Workbook

from xlcalc.engine import Engine
from xlcalc.errors import CIRCULAR, UNSUPPORTED
from xlcalc.ooxml import read_workbook

from .builders import (
    add_defined_name,
    add_external_link,
    make_array_formula,
    save_wb,
)


def _result(results, sheet, coord):
    from xlcalc.ooxml import split_coord

    col, row = split_coord(coord)
    return results[(sheet, col, row)]


def test_cross_sheet_cycle_is_located_and_independent_cell_survives(tmp_path):
    path = str(tmp_path / "cycle.xlsx")
    wb = Workbook()
    a = wb.active
    a.title = "A"
    b = wb.create_sheet("B")
    a["A1"] = "=B!B1+1"          # A!A1 -> B!B1
    b["B1"] = "=A!A1+1"          # B!B1 -> A!A1（跨表循环）
    a["A2"] = 40
    b["B2"] = "=A!A2+2"          # 与循环无关的独立格 => 42
    save_wb(wb, path)

    wb_data = read_workbook(path)
    results = Engine(wb_data).evaluate_all()

    a1 = _result(results, "A", "A1")
    b1 = _result(results, "B", "B1")
    assert a1.value.is_error and a1.value.value == CIRCULAR
    assert b1.value.is_error and b1.value.value == CIRCULAR
    # 原因里要能定位到具体的两个格
    assert "A!A1" in a1.reason and "B!B1" in a1.reason
    assert "A!A1" in b1.reason and "B!B1" in b1.reason
    # 独立单元格不受循环影响
    b2 = _result(results, "B", "B2")
    assert b2.status == "ok" and b2.value.value == 42


def test_same_sheet_cycle_chain(tmp_path):
    path = str(tmp_path / "self_cycle.xlsx")
    wb = Workbook()
    ws = wb.active
    ws.title = "S"
    ws["C1"] = "=C2+1"
    ws["C2"] = "=C3+1"
    ws["C3"] = "=C1+1"  # 回到 C1
    ws["D1"] = "=1+2"   # 独立格
    save_wb(wb, path)

    results = Engine(read_workbook(path)).evaluate_all()
    for coord in ("C1", "C2", "C3"):
        r = _result(results, "S", coord)
        assert r.value.is_error and r.value.value == CIRCULAR
        assert "S!C1" in r.reason and "S!C3" in r.reason
    assert _result(results, "S", "D1").value.value == 3


def test_array_formula_unsupported(tmp_path):
    path = str(tmp_path / "array.xlsx")
    wb = Workbook()
    ws = wb.active
    ws.title = "S"
    ws["A1"] = 1
    ws["A2"] = 2
    ws["B1"] = None  # 预留给数组公式
    save_wb(wb, path)
    make_array_formula(path, 1, "B1", "A1:A2*2", "B1:B2")

    results = Engine(read_workbook(path)).evaluate_all()
    b1 = _result(results, "S", "B1")
    assert b1.status == "unsupported"
    assert b1.value.value == UNSUPPORTED
    assert "数组公式" in b1.reason
    # 静态格照常
    assert _result(results, "S", "A1").value.value == 1


def test_unknown_function_unsupported(tmp_path):
    path = str(tmp_path / "vlookup.xlsx")
    wb = Workbook()
    ws = wb.active
    ws["A1"] = "=VLOOKUP(1,A2:B3,2,FALSE)"
    ws["A2"] = "=POWER(2,3)"
    ws["A3"] = "=1+2"  # 支持的子集照常
    save_wb(wb, path)

    results = Engine(read_workbook(path)).evaluate_all()
    for coord in ("A1", "A2"):
        r = _result(results, "Sheet", coord)
        assert r.status == "unsupported"
        assert r.value.value == UNSUPPORTED
    assert _result(results, "Sheet", "A3").value.value == 3


def test_defined_name_unsupported(tmp_path):
    path = str(tmp_path / "named.xlsx")
    wb = Workbook()
    ws = wb.active
    ws.title = "S"
    ws["A1"] = 10
    ws["A2"] = "=MyRange+1"  # MyRange 是命名区域
    save_wb(wb, path)
    add_defined_name(path, "MyRange", "S!$A$1")

    results = Engine(read_workbook(path)).evaluate_all()
    a2 = _result(results, "S", "A2")
    assert a2.status == "unsupported"
    assert a2.value.value == UNSUPPORTED
    assert "命名区域" in a2.reason or "名称" in a2.reason


def test_external_link_workbook_marked_and_formula_unsupported(tmp_path):
    path = str(tmp_path / "ext.xlsx")
    wb = Workbook()
    ws = wb.active
    ws.title = "S"
    ws["A1"] = "=[1]Sheet1!B2+1"
    ws["A2"] = 5
    save_wb(wb, path)
    add_external_link(path)

    wb_data = read_workbook(path)
    assert wb_data.has_external_links is True
    results = Engine(wb_data).evaluate_all()
    a1 = _result(results, "S", "A1")
    assert a1.status == "unsupported"
    assert a1.value.value == UNSUPPORTED
    assert "外部链接" in a1.reason
    # 独立格不受影响
    assert _result(results, "S", "A2").value.value == 5


def test_invalid_sheet_reference_is_ref_error(tmp_path):
    path = str(tmp_path / "badref.xlsx")
    wb = Workbook()
    ws = wb.active
    ws.title = "S"
    ws["A1"] = "=Missing!A1+1"
    ws["A2"] = "=2+2"
    save_wb(wb, path)

    results = Engine(read_workbook(path)).evaluate_all()
    a1 = _result(results, "S", "A1")
    assert a1.value.is_error
    assert a1.value.value == "#REF!"
    assert "Missing" in a1.reason
    # 独立格仍可求值
    assert _result(results, "S", "A2").value.value == 4
