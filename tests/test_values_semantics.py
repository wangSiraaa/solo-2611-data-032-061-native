"""手算小表：六类值语义、空白/空串/文本/布尔的区别、SUM 与直接算术
的文本处理不混用、比较运算。每个用例都在注释里给出独立手算结果。
"""

from __future__ import annotations

import pytest

from xlcalc.errors import DIV0, VALUE, FormulaError
from xlcalc.values import (
    BLANK,
    BOOLEAN,
    EMPTY_STRING,
    NUMBER,
    TEXT,
    CellValue,
    arithmetic,
    compare,
    negate,
)


def test_blank_vs_empty_string_are_distinct_types():
    assert CellValue.blank().type == BLANK
    assert CellValue.empty_string().type == EMPTY_STRING
    assert CellValue.text("").type == EMPTY_STRING  # 空串归到独立类型
    assert CellValue.text("x").type == TEXT


def test_direct_arithmetic_blank_is_zero():
    # 手算：空白 + 1 = 0 + 1 = 1
    out = arithmetic("+", CellValue.blank(), CellValue.number(1))
    assert out.type == NUMBER and out.value == 1


def test_direct_arithmetic_boolean_counts_as_one():
    # 手算：TRUE * 2 = 1 * 2 = 2；FALSE - 1 = -1
    assert arithmetic("*", CellValue.boolean(True), CellValue.number(2)).value == 2
    assert arithmetic("-", CellValue.boolean(False), CellValue.number(1)).value == -1


def test_direct_arithmetic_text_is_value_error():
    # 直接算术语义：文本（含空串）参与即 #VALUE!，与 SUM 范围语义刻意区分
    with pytest.raises(FormulaError) as exc:
        arithmetic("+", CellValue.text("12"), CellValue.number(1))
    assert exc.value.code == VALUE
    with pytest.raises(FormulaError) as exc:
        arithmetic("*", CellValue.empty_string(), CellValue.number(1))
    assert exc.value.code == VALUE


def test_division_by_zero_is_div0():
    with pytest.raises(FormulaError) as exc:
        arithmetic("/", CellValue.number(1), CellValue.number(0))
    assert exc.value.code == DIV0
    # 空白按 0 参与，同样是除零
    with pytest.raises(FormulaError) as exc:
        arithmetic("/", CellValue.number(1), CellValue.blank())
    assert exc.value.code == DIV0


def test_unary_minus_on_blank_and_text():
    assert negate(CellValue.blank()).value == 0
    with pytest.raises(FormulaError) as exc:
        negate(CellValue.text("x"))
    assert exc.value.code == VALUE


def test_comparison_ordering():
    # Excel 排序：数字(空白按0) < 文本 < 布尔；同类型各自比较
    assert compare("=", CellValue.blank(), CellValue.number(0)).value is True
    assert compare("<", CellValue.number(1), CellValue.text("1")).value is True
    assert compare(">", CellValue.boolean(False), CellValue.text("z")).value is True
    assert compare("=", CellValue.text("a"), CellValue.text("a")).value is True
    assert compare("<>", CellValue.text("a"), CellValue.text("b")).value is True
    assert compare("<=", CellValue.number(2), CellValue.number(2)).value is True
    # 空串是文本类别，大于数字
    assert compare(">", CellValue.empty_string(), CellValue.number(0)).value is True
    # 布尔 FALSE < TRUE
    assert compare("<", CellValue.boolean(False), CellValue.boolean(True)).value is True


def test_error_propagates_through_arithmetic_and_compare():
    err = CellValue.error(DIV0, "除数为零")
    with pytest.raises(FormulaError) as exc:
        arithmetic("+", err, CellValue.number(1))
    assert exc.value.code == DIV0
    with pytest.raises(FormulaError) as exc:
        compare("=", err, CellValue.number(1))
    assert exc.value.code == DIV0


def test_boolean_is_not_number_type():
    # bool 不能被 number() 静默吞掉
    assert CellValue.number(True).type == BOOLEAN
    assert CellValue.number(False).type == BOOLEAN
    assert CellValue.number(3).type == NUMBER
