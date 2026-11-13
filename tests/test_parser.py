"""解析器测试：优先级、结合性、AST 结构、各类语法错误定位与列名校验。"""

import unittest

from sql3v import ast_nodes as ast
from sql3v.errors import ParseError
from sql3v.parser import parse_predicate
from sql3v.render import sexpr

COLS = {"x", "y", "z"}


class ParserTest(unittest.TestCase):
    def test_precedence_or_and_not_predicate(self):
        _, tree = parse_predicate("NOT x = 1 OR y = 2 AND z IS NULL", COLS)
        # OR 在最外，AND 在右子树，NOT 只作用于 x = 1
        self.assertIsInstance(tree, ast.BinBool)
        self.assertEqual(tree.op, "OR")
        self.assertIsInstance(tree.left, ast.NotExpr)
        self.assertIsInstance(tree.right, ast.BinBool)
        self.assertEqual(tree.right.op, "AND")

    def test_parentheses_override(self):
        _, tree = parse_predicate("(x = 1 OR y = 2) AND z IS NOT NULL", COLS)
        self.assertIsInstance(tree, ast.BinBool)
        self.assertEqual(tree.op, "AND")
        self.assertIsInstance(tree.left, ast.BinBool)
        self.assertEqual(tree.left.op, "OR")

    def test_left_associativity(self):
        _, tree = parse_predicate("x = 1 AND y = 2 AND z = 3", COLS)
        self.assertIsInstance(tree, ast.BinBool)
        self.assertIsInstance(tree.left, ast.BinBool)  # 左结合
        _, tree2 = parse_predicate("x = 1 OR y = 2 OR z = 3", COLS)
        self.assertIsInstance(tree2.left, ast.BinBool)

    def test_double_negation_ast(self):
        _, tree = parse_predicate("NOT NOT x IS NULL", COLS)
        self.assertIsInstance(tree, ast.NotExpr)
        self.assertIsInstance(tree.operand, ast.NotExpr)
        self.assertEqual(sexpr(tree), "(not (not (is-null x)))")

    def test_in_and_not_in_ast(self):
        _, tree = parse_predicate("x IN (1, -2, NULL)", COLS)
        self.assertIsInstance(tree, ast.InList)
        self.assertFalse(tree.negate)
        self.assertEqual(len(tree.elements), 3)
        _, tree2 = parse_predicate("x NOT IN (1)", COLS)
        self.assertTrue(tree2.negate)
        self.assertEqual(sexpr(tree2), "(not-in x [1])")

    def test_compare_ops(self):
        for op in ("=", "<>", "<", "<=", ">", ">="):
            with self.subTest(op=op):
                _, tree = parse_predicate(f"x {op} 5", COLS)
                self.assertIsInstance(tree, ast.Compare)
                self.assertEqual(tree.op, op)

    def test_unknown_column_rejected(self):
        with self.assertRaises(ParseError) as ctx:
            parse_predicate("w = 1", COLS)
        self.assertIn("未知列名", ctx.exception.message)
        self.assertEqual(ctx.exception.col, 1)

    def test_column_name_case_sensitive(self):
        with self.assertRaises(ParseError):
            parse_predicate("X = 1", COLS)  # 声明的是小写 x

    def test_missing_operands_and_parens(self):
        bad = [
            "x =", "= 1", "x AND", "OR x = 1", "NOT", "(x = 1",
            "x = 1)", "x IN (", "x IN (1,)", "x IN ()", "x IN (1,",
            "x IS", "x IS NOT", "()", "",
        ]
        for text in bad:
            with self.subTest(text=text):
                with self.assertRaises(ParseError) as ctx:
                    parse_predicate(text, COLS)
                self.assertIsNotNone(ctx.exception.col)

    def test_chained_comparison_rejected(self):
        with self.assertRaises(ParseError):
            parse_predicate("x = 1 = 2", COLS)
        with self.assertRaises(ParseError):
            parse_predicate("x = 1 AND y = 2 >", COLS)

    def test_boolean_operand_rejected_in_compare(self):
        with self.assertRaises(ParseError):
            parse_predicate("(x = 1) = 1", COLS)
        with self.assertRaises(ParseError):
            parse_predicate("1 = (y = 2)", COLS)

    def test_bare_literal_not_boolean(self):
        with self.assertRaises(ParseError):
            parse_predicate("1", COLS)
        with self.assertRaises(ParseError):
            parse_predicate("NULL", COLS)
        with self.assertRaises(ParseError):
            parse_predicate("x", COLS)

    def test_in_list_restrictions(self):
        with self.assertRaises(ParseError):
            parse_predicate("x IN (y)", COLS)
        with self.assertRaises(ParseError):
            parse_predicate("x IN (x = 1)", COLS)
        with self.assertRaises(ParseError):
            parse_predicate("x NOT NOT IN (1)", COLS)

    def test_trailing_garbage(self):
        with self.assertRaises(ParseError) as ctx:
            parse_predicate("x = 1 2", COLS)
        self.assertIn("多余内容", ctx.exception.message)

    def test_null_literal_in_compare(self):
        _, tree = parse_predicate("NULL <> x", COLS)
        self.assertIsInstance(tree, ast.Compare)
        self.assertIsInstance(tree.left, ast.NullLit)


if __name__ == "__main__":
    unittest.main()
