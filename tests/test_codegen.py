"""受控 SQL 生成与参数绑定测试。"""

import unittest

from sql3v import ast_nodes as ast
from sql3v.codegen import generate, parameterized_query
from sql3v.parser import parse_predicate


class CodegenTest(unittest.TestCase):
    def test_placeholder_count_matches_params(self):
        cases = [
            ("x NOT IN (1, NULL)", {"x": 2}),
            ("x NOT IN (1, 2)", {"x": 2}),
            ("x IN (1, 2, 3)", {"x": 1}),
            ("x = x", {"x": 7}),
            ("NOT (x = 1 OR y = 2) AND z IS NOT NULL",
             {"x": None, "y": 2, "z": None}),
            ("x <> -5 AND y <= 10", {"x": 3, "y": 4}),
        ]
        for text, env in cases:
            with self.subTest(text=text):
                _, tree = parse_predicate(text, set(env))
                sql, params = parameterized_query(tree, env)
                self.assertEqual(sql.count("?"), len(params),
                                 f"{text}: 占位符数与参数数不一致")

    def test_in_binding_order_is_operand_first(self):
        # 回归：IN 的参数顺序必须与占位符文本位置一致（操作数在前）
        _, tree = parse_predicate("x NOT IN (1, 2)", {"x"})
        sql, params = parameterized_query(tree, {"x": 2})
        self.assertEqual(sql, "(? NOT IN (?, ?))")
        self.assertEqual(params, (2, 1, 2))

        _, tree2 = parse_predicate("x IN (1, NULL, 3)", {"x"})
        sql2, params2 = parameterized_query(tree2, {"x": 3})
        self.assertEqual(params2, (3, 1, None, 3))

    def test_repeated_column_gets_repeated_params(self):
        _, tree = parse_predicate("x = x OR x <> x", {"x"})
        sql, params = parameterized_query(tree, {"x": 9})
        self.assertEqual(sql, "((? = ?) OR (? <> ?))")
        self.assertEqual(params, (9, 9, 9, 9))

    def test_parameterized_sql_contains_no_user_text(self):
        # 参数化 SQL 里既不能出现列名，也不能出现值，只能有结构与占位符
        _, tree = parse_predicate("secret_col IN (-7, NULL)", {"secret_col"})
        sql, params = parameterized_query(tree, {"secret_col": -7})
        self.assertNotIn("secret_col", sql)
        self.assertNotIn("-7", sql)
        self.assertNotIn("NULL", sql)
        self.assertEqual(params, (-7, -7, None))

    def test_template_uses_quoted_identifiers_and_literal_placeholders(self):
        _, tree = parse_predicate("x = 1 AND y IS NULL", {"x", "y"})
        gen = generate(tree)
        self.assertIn('"x"', gen.expression)
        self.assertIn('"y"', gen.expression)
        # IS NULL 中的 NULL 是语法关键字，不产生参数；只有整数 1 是字面量参数
        self.assertEqual(gen.literal_params, (1,))
        self.assertEqual(gen.expression.count("?"), len(gen.literal_params))

        # 但 IN 列表里的 NULL 是值，必须产生占位符与 None 参数
        _, tree2 = parse_predicate("x IN (1, NULL, 2)", {"x"})
        gen2 = generate(tree2)
        self.assertEqual(gen2.literal_params, (1, None, 2))
        self.assertEqual(gen2.expression.count("?"), 3)

    def test_identifier_double_quote_escaping(self):
        # 纵深防御：即便绕过声明校验，定界标识符内部双引号也会被翻倍
        col = ast.Column('a"b', 1, 1, 2)
        node = ast.Compare("=", col, ast.IntLit(1, 1, 5, 6), 1, 3, 6)
        gen = generate(node)
        self.assertEqual(gen.expression, '("a""b" = ?)')


if __name__ == "__main__":
    unittest.main()
