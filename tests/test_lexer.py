"""词法分析器测试：合法词法单元、非法字符定位、整数范围、token 上限。"""

import unittest

from sql3v.errors import LexError
from sql3v.lexer import INT64_MAX, INT64_MIN, MAX_TOKENS, tokenize


class LexerTest(unittest.TestCase):
    def test_basic_tokens(self):
        toks = tokenize("x <> -5 AND y IS NOT NULL OR z IN (1, NULL)")
        kinds = [t.kind for t in toks]
        self.assertEqual(kinds, [
            "IDENT", "<>", "INT", "AND", "IDENT", "IS", "NOT", "NULL", "OR",
            "IDENT", "IN", "(", "INT", ",", "NULL", ")", "EOF",
        ])
        int_tok = next(t for t in toks if t.kind == "INT")
        self.assertEqual(int_tok.value, -5)
        self.assertEqual(int_tok.col, 6)

    def test_keywords_case_insensitive_identifiers_sensitive(self):
        toks = tokenize("null Null NULL or Or")
        self.assertEqual([t.kind for t in toks],
                         ["NULL", "NULL", "NULL", "OR", "OR", "EOF"])
        toks2 = tokenize("abc Abc ABC")
        self.assertEqual([t.text for t in toks2 if t.kind == "IDENT"],
                         ["abc", "Abc", "ABC"])

    def test_operators(self):
        toks = tokenize("a <= b >= c < d > e = f <> g")
        self.assertEqual([t.kind for t in toks if t.kind not in ("IDENT",)],
                         ["<=", ">=", "<", ">", "=", "<>", "EOF"])

    def test_negative_literal_vs_minus_operator(self):
        toks = tokenize("x = -3")
        self.assertEqual([t.kind for t in toks], ["IDENT", "=", "INT", "EOF"])
        with self.assertRaises(LexError) as ctx:
            tokenize("-x")
        self.assertEqual(ctx.exception.col, 1)
        with self.assertRaises(LexError):
            tokenize("(1) - 2")  # 前一个 token 是 ')'，'-' 视为不支持的算术

    def test_illegal_chars_are_located(self):
        for bad in ["a + b", "a * b", "a / b", "a % b", "a = 'x'", 'a = "x"',
                    "a = [b]", "a = 1;", "a = $1", "a = 1.5"]:
            with self.subTest(bad=bad):
                with self.assertRaises(LexError) as ctx:
                    tokenize(bad)
                self.assertIsNotNone(ctx.exception.line)
                self.assertIsNotNone(ctx.exception.col)

    def test_int64_bounds(self):
        tokenize(str(INT64_MAX))
        tokenize(str(INT64_MIN))
        with self.assertRaises(LexError):
            tokenize(str(INT64_MAX + 1))
        with self.assertRaises(LexError):
            tokenize(str(INT64_MIN - 1))

    def test_token_limit(self):
        # N 段 "x = 1" 以 OR 连接：token 数 = 3N + (N-1) = 4N-1
        text = " OR ".join(["x = 1"] * 68)  # 271 个 token，超限
        with self.assertRaises(LexError) as ctx:
            tokenize(text)
        self.assertIn(str(MAX_TOKENS), ctx.exception.message)

        # 精确 200：49 段比较 + 48 个 OR = 195，再接 " OR x IS NULL"(4) = 199；
        # 用 IS NOT NULL(5) 即 195 + 5 = 200
        exact = " OR ".join(["x = 1"] * 49) + " OR x IS NOT NULL"
        self.assertEqual(len(tokenize(exact)) - 1, MAX_TOKENS)

        # 201 个 token 必须被拒绝
        over = " OR ".join(["x = 1"] * 49) + " OR x IS NOT NULL OR x = 1"
        with self.assertRaises(LexError):
            tokenize(over)

    def test_multiline_positions(self):
        toks = tokenize("x = 1\nAND y <> 2")
        and_tok = next(t for t in toks if t.kind == "AND")
        self.assertEqual((and_tok.line, and_tok.col), (2, 1))


if __name__ == "__main__":
    unittest.main()
