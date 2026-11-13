"""有限域声明校验测试。"""

import unittest

from sql3v.domain import MAX_ASSIGNMENTS, load_spec
from sql3v.errors import DomainError


def base(**overrides):
    data = {
        "columns": [{"name": "x", "values": [1, None, 2]}],
        "predicate_a": "x = 1",
        "predicate_b": "x IS NULL",
    }
    data.update(overrides)
    return data


class DomainTest(unittest.TestCase):
    def test_canonical_ordering_null_first_then_ints(self):
        spec = load_spec(base())
        self.assertEqual(spec.columns[0].values, (None, 1, 2))

    def test_duplicate_value_rejected(self):
        with self.assertRaises(DomainError):
            load_spec(base(columns=[{"name": "x", "values": [1, 1]}]))
        with self.assertRaises(DomainError):
            load_spec(base(columns=[{"name": "x", "values": [None, None]}]))

    def test_bad_value_types(self):
        for bad in (["1"], [1.5], [True], [{}]):
            with self.subTest(bad=bad):
                with self.assertRaises(DomainError):
                    load_spec(base(columns=[{"name": "x", "values": bad}]))

    def test_column_name_rules(self):
        with self.assertRaises(DomainError):
            load_spec(base(columns=[{"name": "1x", "values": [1]}]))
        with self.assertRaises(DomainError):
            load_spec(base(columns=[{"name": "and", "values": [1]}]))  # 保留字
        with self.assertRaises(DomainError):
            load_spec(base(columns=[{"name": "a-b", "values": [1]}]))

    def test_duplicate_column_rejected(self):
        with self.assertRaises(DomainError):
            load_spec(base(columns=[
                {"name": "x", "values": [1]},
                {"name": "x", "values": [2]},
            ]))
        # 大小写不同视为不同列
        spec = load_spec(base(columns=[
            {"name": "x", "values": [1]},
            {"name": "X", "values": [2]},
        ]))
        self.assertEqual(len(spec.columns), 2)

    def test_column_and_value_count_limits(self):
        with self.assertRaises(DomainError):
            load_spec(base(columns=[{"name": f"c{i}", "values": [1]}
                                    for i in range(7)]))
        with self.assertRaises(DomainError):
            load_spec(base(columns=[{"name": "x", "values": [1, 2, 3, 4, 5, 6]}]))
        with self.assertRaises(DomainError):
            load_spec(base(columns=[{"name": "x", "values": []}]))

    def test_assignment_cap(self):
        # 6 列 × 每列 5 值 = 15625，合法
        ok = load_spec(base(columns=[
            {"name": f"c{i}", "values": [1, 2, 3, 4, 5]} for i in range(6)
        ]))
        self.assertEqual(ok.assignment_count, 5**6)
        self.assertLessEqual(ok.assignment_count, MAX_ASSIGNMENTS)
        # 直接构造超限规格，验证保护逻辑（声明入口在每列 5 值约束下到不了这里，
        # 但保护必须独立存在，防止约束调整后静默放行）
        from sql3v.domain import DomainColumn
        inflated = type(ok)(
            columns=tuple(DomainColumn(f"c{i}", tuple(range(8))) for i in range(5)),
            predicate_a=ok.predicate_a, predicate_b=ok.predicate_b, raw={},
        )
        self.assertGreater(inflated.assignment_count, MAX_ASSIGNMENTS)
        with self.assertRaises(DomainError):
            load_spec({
                "columns": [{"name": f"c{i}",
                             "values": list(range(8))} for i in range(5)],
                "predicate_a": "c0 = 1",
                "predicate_b": "c0 = 2",
            })

    def test_missing_fields(self):
        with self.assertRaises(DomainError):
            load_spec({"columns": []})
        with self.assertRaises(DomainError):
            load_spec(base(predicate_a="   "))

    def test_enumeration_canonical(self):
        spec = load_spec(base(columns=[
            {"name": "b", "values": [2, 1]},
            {"name": "a", "values": [None, 1]},
        ]))
        envs = spec.enumerate_assignments()
        # 列序按名字：a 先 b 后；a 的域 NULL 在前
        self.assertEqual([list(e.items())[0][0] for e in envs[:1]], ["a"])
        self.assertEqual(envs[0], {"a": None, "b": 1})
        self.assertEqual(envs[-1], {"a": 1, "b": 2})


if __name__ == "__main__":
    unittest.main()
