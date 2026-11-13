"""三值逻辑 AND / OR / NOT 的手工真值表测试。"""

import unittest

from sql3v.trit import Trit, and3, not3, or3, where_accepts


class TritTableTest(unittest.TestCase):
    def test_not_table(self):
        self.assertEqual(not3(Trit.TRUE), Trit.FALSE)
        self.assertEqual(not3(Trit.FALSE), Trit.TRUE)
        self.assertEqual(not3(Trit.UNKNOWN), Trit.UNKNOWN)

    def test_and_table(self):
        cases = {
            (Trit.TRUE, Trit.TRUE): Trit.TRUE,
            (Trit.TRUE, Trit.FALSE): Trit.FALSE,
            (Trit.FALSE, Trit.TRUE): Trit.FALSE,
            (Trit.FALSE, Trit.FALSE): Trit.FALSE,
            (Trit.TRUE, Trit.UNKNOWN): Trit.UNKNOWN,
            (Trit.UNKNOWN, Trit.TRUE): Trit.UNKNOWN,
            (Trit.FALSE, Trit.UNKNOWN): Trit.FALSE,
            (Trit.UNKNOWN, Trit.FALSE): Trit.FALSE,
            (Trit.UNKNOWN, Trit.UNKNOWN): Trit.UNKNOWN,
        }
        for (a, b), expected in cases.items():
            with self.subTest(a=a, b=b):
                self.assertEqual(and3(a, b), expected)

    def test_or_table(self):
        cases = {
            (Trit.TRUE, Trit.TRUE): Trit.TRUE,
            (Trit.TRUE, Trit.FALSE): Trit.TRUE,
            (Trit.FALSE, Trit.TRUE): Trit.TRUE,
            (Trit.FALSE, Trit.FALSE): Trit.FALSE,
            (Trit.TRUE, Trit.UNKNOWN): Trit.TRUE,
            (Trit.UNKNOWN, Trit.TRUE): Trit.TRUE,
            (Trit.FALSE, Trit.UNKNOWN): Trit.UNKNOWN,
            (Trit.UNKNOWN, Trit.FALSE): Trit.UNKNOWN,
            (Trit.UNKNOWN, Trit.UNKNOWN): Trit.UNKNOWN,
        }
        for (a, b), expected in cases.items():
            with self.subTest(a=a, b=b):
                self.assertEqual(or3(a, b), expected)

    def test_where_only_accepts_true(self):
        self.assertTrue(where_accepts(Trit.TRUE))
        self.assertFalse(where_accepts(Trit.FALSE))
        self.assertFalse(where_accepts(Trit.UNKNOWN))


if __name__ == "__main__":
    unittest.main()
