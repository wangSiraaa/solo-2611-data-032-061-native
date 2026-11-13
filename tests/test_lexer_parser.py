"""词法/语法/AST 层单元测试：含空格表名、绝对相对混合、括号、
比较运算符、共享位移的绝对锁定、不支持 token 的显式拒绝。"""

from __future__ import annotations

import pytest

from xlcalc import parser as P
from xlcalc.errors import UNSUPPORTED, FormulaError
from xlcalc.lexer import T_REF, T_STRING, tokenize


def test_tokenize_quoted_sheet_name_with_spaces():
    tokens = tokenize("'Sales Detail'!D2:D5")
    assert len(tokens) == 1 and tokens[0].kind == T_REF
    ref = P.parse_reference(tokens[0].text)
    assert ref.sheet == "Sales Detail"
    assert (ref.col, ref.row) == (4, 2)
    assert (ref.end_col, ref.end_row) == (4, 5)


def test_tokenize_mixed_absolute_relative():
    ref = P.parse_reference("$B$2")
    assert (ref.col, ref.row) == (2, 2) and ref.abs_col and ref.abs_row
    ref = P.parse_reference("B$2")
    assert not ref.abs_col and ref.abs_row
    ref = P.parse_reference("$B2")
    assert ref.abs_col and not ref.abs_row
    ref = P.parse_reference("Inputs!B2")
    assert ref.sheet == "Inputs" and not ref.abs_col and not ref.abs_row


def test_column_letters_roundtrip():
    for col in (1, 2, 26, 27, 52, 700, 16384):
        assert P.letters_to_col(P.col_to_letters(col)) == col
    assert P.col_to_letters(1) == "A"
    assert P.col_to_letters(27) == "AA"
    assert P.col_to_letters(701) == "ZY"  # 边界检查


def test_parentheses_and_precedence():
    ast = P.parse_formula("(2+3)*4")
    assert isinstance(ast, P.Binary) and ast.op == "*"
    assert isinstance(ast.left, P.Binary) and ast.left.op == "+"
    # 无括号时乘除优先
    ast2 = P.parse_formula("2+3*4")
    assert isinstance(ast2, P.Binary) and ast2.op == "+"
    assert isinstance(ast2.right, P.Binary) and ast2.right.op == "*"


def test_all_comparison_operators_parse():
    for op in ("=", "<>", "<", "<=", ">", ">="):
        ast = P.parse_formula(f"A1{op}B1")
        assert isinstance(ast, P.Binary) and ast.op == op


def test_string_literal_with_escaped_quote():
    tokens = tokenize('"a""b"')
    assert tokens[0].kind == T_STRING
    ast = P.parse_formula('"a""b"')
    assert isinstance(ast, P.Text) and ast.value == 'a"b'


def test_true_false_are_booleans():
    assert P.parse_formula("TRUE") == P.Bool(True)
    assert P.parse_formula("false") == P.Bool(False)


def test_unsupported_operators_rejected():
    for bad in ("2^3", "A1&B1", "10%"):
        with pytest.raises(FormulaError) as exc:
            P.parse_formula(bad)
        assert exc.value.code == UNSUPPORTED


def test_unknown_function_becomes_unsupported_node_not_name_error_at_parse():
    ast = P.parse_formula("CONCAT(A1,B1)")
    assert isinstance(ast, P.Unsupported)
    assert "CONCAT" in ast.reason


def test_unknown_function_with_range_args_reports_function_not_syntax():
    # 回归：参数里的子集外语法（B:C）不应掩盖“函数不支持”的真实原因
    ast = P.parse_formula("VLOOKUP(1,B:C,2,FALSE)")
    assert isinstance(ast, P.Unsupported)
    assert "VLOOKUP" in ast.reason


def test_arity_error_is_unsupported():
    from xlcalc.errors import FormulaError
    with pytest.raises(FormulaError) as exc:
        P.parse_formula("IFERROR(1)")
    assert exc.value.code == UNSUPPORTED


def test_bare_name_is_unsupported():
    ast = P.parse_formula("MyRange+1")
    # MyRange 是裸名称（非函数调用），必须显式 unsupported
    assert isinstance(ast, P.Binary)
    assert isinstance(ast.left, P.Unsupported)


def test_external_reference_tokenizes_as_external():
    from xlcalc.lexer import T_EXTERNAL

    tokens = tokenize("[1]Sheet1!B2+1")
    assert tokens[0].kind == T_EXTERNAL
    ast = P.parse_formula("[1]Sheet1!B2+1")
    assert isinstance(ast, P.Binary)
    assert isinstance(ast.left, P.Unsupported)
    assert "外部链接" in ast.left.reason


def test_shared_shift_respects_absolute_markers():
    master = P.parse_formula("B2*Data!$B$1+C$2+$D3")
    shifted = P.shift(master, 0, 1)  # 向下一行
    text = P.render(shifted)
    # B2 相对 -> B3；$B$1 全锁；C$2 锁行保持第 2 行；$D3 锁列移行 -> $D4
    # render 按优先级最小化括号
    assert text == "B3*Data!$B$1+C$2+$D4"
    # 逐个引用核对锁定语义
    refs = [n for n in _walk(shifted) if isinstance(n, P.Reference)]
    by_raw = {r.raw: r for r in refs}
    assert (by_raw["B2"].col, by_raw["B2"].row) == (2, 3)
    assert by_raw["Data!$B$1"].abs_col and by_raw["Data!$B$1"].abs_row
    assert by_raw["C$2"].abs_row and by_raw["C$2"].row == 2
    assert by_raw["$D3"].abs_col and by_raw["$D3"].row == 4


def _walk(node):
    yield node
    if isinstance(node, P.Binary):
        yield from _walk(node.left)
        yield from _walk(node.right)
    if isinstance(node, P.Unary):
        yield from _walk(node.operand)
    if isinstance(node, P.FunctionCall):
        for a in node.args:
            yield from _walk(a)


def test_shared_shift_horizontal_and_vertical():
    # 相对 A1 双向位移；B$1 锁行只移列；$C1 锁列只移行
    master = P.parse_formula("A1+B$1+$C1")
    shifted = P.shift(master, 2, 3)
    assert P.render(shifted) == "C4+D$1+$C4"
    refs = {r.raw: r for r in _walk(shifted) if isinstance(r, P.Reference)}
    assert (refs["A1"].col, refs["A1"].row) == (3, 4)
    assert (refs["B$1"].col, refs["B$1"].row) == (4, 1)  # 行锁定
    assert (refs["$C1"].col, refs["$C1"].row) == (3, 4)  # 列锁定


def test_row_absolute_marker_parsed_correctly():
    # 回归：C$2 必须识别为行绝对（之前 $ 被误吞导致行号解析失败）
    ref = P.parse_reference("C$2")
    assert (ref.col, ref.row) == (3, 2)
    assert ref.abs_col is False and ref.abs_row is True
    shifted = P.shift(ref, 5, 7)
    assert (shifted.col, shifted.row) == (8, 2)  # 行锁定不动


def test_shift_out_of_sheet_bounds_is_unsupported():
    master = P.parse_formula("A1")
    with pytest.raises(FormulaError) as exc:
        P.shift(master, 0, P.MAX_ROW)
    assert exc.value.code == UNSUPPORTED
    assert "越界" in exc.value.reason


def test_render_normalizes_numbers():
    assert P.render(P.parse_formula("1+2.50")) == "1+2.5"
    assert P.render(P.parse_formula("1.0")) == "1"


def test_function_arity_validation():
    with pytest.raises(FormulaError):
        P.parse_formula("IFERROR(1)")  # 缺第二个参数
    with pytest.raises(FormulaError):
        P.parse_formula("IF(1,2,3,4)")  # 参数过多
