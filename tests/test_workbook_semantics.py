"""工作簿级手算小表。

工作簿两张表，第二张表名含空格 'Calc Sheet'，手算布局：
  Data（跨表数据源）:
    B1=3（绝对引用锁定目标）  B2=2  B3=0（分母）
    B4=(空)  B5=TRUE（布尔）  A7="文本" B7=9
    A8='=1+2'（等号开头普通文本，禁止执行）
  Calc Sheet（本表 B/C 列数据 + D..H 列公式）:
    B2=2 C2=5   B3=0 C3=10   B4=(空) C4=7
    D2:D4 共享公式主格 D2 = B2*C2（跟随格 D3/D4 位移展开）
    E2:E4 共享公式，含绝对/相对混合：E2 = Data!B2*Data!$B$1
    F2    = SUM(Data!B2:B5) 范围聚合：空白 B4、布尔 B5 都跳过 -> 2
    F3    = SUM(Data!B2)+Data!B5 标量布尔按 1 -> 3
    F4    = Data!B4+1       空白直接算术按 0 -> 1
    F5    = Data!A7+1       文本直接算术 -> #VALUE!
    G1    = IF(Data!B3=0,0,10/Data!B3) 条件真选 0；除零分支不求值 -> 0
    G2    = IF(Data!B3=1,10/Data!B3,7) 条件假选 7
    G3    = IFERROR(1/Data!B3,"回退")  除零被捕获 -> "回退"
    G4    = IFERROR(Data!B3+1,"回退")  无错误 -> 1
    G5    = Data!A8                    引用等号开头文本 -> 文本 "=1+2"
    H1    = Data!B2 + D2    跨表+本表 -> 2 + 10 = 12
    H2    = (B2+C2)*2       括号 -> 14
    H3    = B2>=2           比较 -> TRUE
"""

from __future__ import annotations

import pytest
from openpyxl import Workbook

from xlcalc.engine import Engine, _EvalContext
from xlcalc.errors import DIV0, VALUE, FormulaError
from xlcalc.ooxml import read_workbook, split_coord
from xlcalc.parser import parse_formula
from xlcalc.values import BOOLEAN, NUMBER, TEXT

from .builders import make_shared_formula, save_wb, set_static_inline_string


@pytest.fixture(scope="module")
def calc_path(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("wb") / "calc.xlsx")
    wb = Workbook()
    data = wb.active
    data.title = "Data"
    data["B1"] = 3
    data["B2"] = 2
    data["B3"] = 0
    # Data!B4 留空（空白格）
    data["B5"] = True
    data["A7"] = "文本"
    data["B7"] = 9
    # A8 目标是以 = 开头的普通文本；openpyxl 会把它当公式，
    # 故先占位，保存后用 lxml 改写为 inlineStr 静态文本
    data["A8"] = None

    calc = wb.create_sheet("Calc Sheet")
    calc["B2"] = 2
    calc["C2"] = 5
    calc["B3"] = 0
    calc["C3"] = 10
    # Calc Sheet!B4 留空（空白格）
    calc["C4"] = 7
    for coord in ("D2", "D3", "D4", "E2", "E3", "E4"):
        calc[coord] = None
    calc["F2"] = "=SUM(Data!B2:B5)"
    calc["F3"] = "=SUM(Data!B2)+Data!B5"
    calc["F4"] = "=Data!B4+1"
    calc["F5"] = "=Data!A7+1"
    calc["G1"] = "=IF(Data!B3=0,0,10/Data!B3)"
    calc["G2"] = "=IF(Data!B3=1,10/Data!B3,7)"
    calc["G3"] = '=IFERROR(1/Data!B3,"回退")'
    calc["G4"] = '=IFERROR(Data!B3+1,"回退")'
    calc["G5"] = "=Data!A8"
    calc["H1"] = "=Data!B2+D2"
    calc["H2"] = "=(B2+C2)*2"
    calc["H3"] = "=B2>=2"
    calc["I1"] = "=1/Data!B3"  # 未捕获除零 -> #DIV/0! 错误值
    save_wb(wb, path)

    # 注入两组共享公式（sheet 顺序：Data=1, Calc Sheet=2）
    make_shared_formula(path, 2, "D2", ["D3", "D4"], "B2*C2", "D2:D4", si="0")
    make_shared_formula(path, 2, "E2", ["E3", "E4"],
                        "Data!B2*Data!$B$1", "E2:E4", si="1")
    # A8 写为 inlineStr 文本 "=1+2"，验证引擎不会执行等号开头的普通文本
    set_static_inline_string(path, 1, "A8", "=1+2")
    return path


@pytest.fixture(scope="module")
def evaluated(calc_path):
    wb = read_workbook(calc_path)
    engine = Engine(wb)
    results = engine.evaluate_all()
    return wb, engine, results


def _val(evaluated, sheet, coord):
    col, row = split_coord(coord)
    return evaluated[2][(sheet, col, row)].value


def test_shared_formula_expands_with_relative_shift(evaluated):
    # D2=2*5=10, D3=0*10=0, D4=空白(0)*7=0
    assert _val(evaluated, "Calc Sheet", "D2").value == 10
    assert _val(evaluated, "Calc Sheet", "D3").value == 0
    assert _val(evaluated, "Calc Sheet", "D4").value == 0
    # 跟随格必须有各自的展开式，不能与主格同公式
    c, r = split_coord("D3")
    d3 = evaluated[2][("Calc Sheet", c, r)]
    assert d3.formula_text == "B2*C2"       # 原文是主格公式
    assert d3.expanded_formula == "B3*C3"   # 展开后是自己的公式


def test_mixed_absolute_relative_shift(evaluated):
    # E2 = Data!B2(2)*Data!$B$1(3) = 6；E3=0*3=0；E4=空白0*3=0
    assert _val(evaluated, "Calc Sheet", "E2").value == 6
    assert _val(evaluated, "Calc Sheet", "E3").value == 0
    assert _val(evaluated, "Calc Sheet", "E4").value == 0
    c, r = split_coord("E3")
    expanded = evaluated[2][("Calc Sheet", c, r)].expanded_formula
    assert expanded == "Data!B3*Data!$B$1"  # 相对行平移，绝对引用锁定


def test_sum_range_skips_blank_boolean_but_scalar_boolean_counts(evaluated):
    # 范围 SUM(Data!B2:B5)：B4 空白、B5 布尔都跳过 => 2
    f2 = _val(evaluated, "Calc Sheet", "F2")
    assert f2.type == NUMBER and f2.value == 2
    # 标量 SUM(Data!B2)+Data!B5：布尔标量按 1 => 2 + 1 = 3
    f3 = _val(evaluated, "Calc Sheet", "F3")
    assert f3.type == NUMBER and f3.value == 3


def test_blank_in_direct_arithmetic_is_zero(evaluated):
    # Data!B4 空白直接算术按 0 => 1
    assert _val(evaluated, "Calc Sheet", "F4").value == 1


def test_text_in_direct_arithmetic_is_value_error(evaluated):
    # F5 = Data!A7+1，A7 是文本 => 直接算术 #VALUE!
    v = _val(evaluated, "Calc Sheet", "F5")
    assert v.is_error and v.value == VALUE
    # 数字格 B7 直接算术不受影响
    ctx = _EvalContext(evaluated[1], "Calc Sheet", [])
    assert ctx.eval(parse_formula("Data!B7+1")).value == 10


def test_if_lazy_branch_division_by_zero_never_evaluated(evaluated):
    # G1 条件真选 0；未选分支 10/0 不求值，结果是 0 而非错误
    g1 = _val(evaluated, "Calc Sheet", "G1")
    assert g1.type == NUMBER and g1.value == 0
    c, r = split_coord("G1")
    deps = evaluated[2][("Calc Sheet", c, r)].dependencies
    assert deps == ["Data!B3"]
    # G2 条件假选常量 7
    assert _val(evaluated, "Calc Sheet", "G2").value == 7


def test_iferror_only_catches_formula_errors(evaluated):
    g3 = _val(evaluated, "Calc Sheet", "G3")
    assert g3.type == TEXT and g3.value == "回退"
    g4 = _val(evaluated, "Calc Sheet", "G4")
    assert g4.type == NUMBER and g4.value == 1


def test_equals_prefixed_text_is_not_executed(evaluated):
    g5 = _val(evaluated, "Calc Sheet", "G5")
    assert g5.type == TEXT and g5.value == "=1+2"
    a8 = _val(evaluated, "Data", "A8")
    assert a8.type == TEXT and a8.value == "=1+2"


def test_cross_sheet_and_parentheses_and_comparison(evaluated):
    assert _val(evaluated, "Calc Sheet", "H1").value == 12
    assert _val(evaluated, "Calc Sheet", "H2").value == 14
    h3 = _val(evaluated, "Calc Sheet", "H3")
    assert h3.type == BOOLEAN and h3.value is True


def test_blank_cell_reference_is_blank_type(evaluated):
    # Data!B4 / Calc Sheet!B4 在 OOXML 中无 <c> 记录：经引用解析按空白处理
    engine = evaluated[1]
    for sheet in ("Data", "Calc Sheet"):
        col, row = split_coord("B4")
        assert engine.resolve_cell(sheet, col, row, []).is_blank
    # 不存在的格同样按空白
    col, row = split_coord("Z99")
    assert engine.resolve_cell("Data", col, row, []).is_blank


def test_div0_error_code_on_uncatched_division(evaluated):
    import pytest

    engine = evaluated[1]
    ctx = _EvalContext(engine, "Data", [])
    # 未被 IFERROR 包裹的除零：引擎内部抛 FormulaError
    with pytest.raises(FormulaError) as exc:
        ctx.eval(parse_formula("1/B3"))  # B3=0
    assert exc.value.code == DIV0
    # 端到端：I1 公式格落为 #DIV/0! 错误值；G3 被 IFERROR 捕获，状态 ok
    i1 = _val(evaluated, "Calc Sheet", "I1")
    assert i1.is_error and i1.value == DIV0
    col, row = split_coord("G3")
    assert evaluated[2][("Calc Sheet", col, row)].status == "ok"
