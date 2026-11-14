"""schema 子集校验测试：支持范围、递归拒绝、default 类型约束。"""

import unittest

from avro_compat.schema import SchemaError, validate_schema
from tests.helpers import arr, field, rec


class TestSupportedSubset(unittest.TestCase):
    def _base(self, ftype):
        return rec("Root", [field("f", ftype)])

    def test_all_supported_primitives(self):
        for t in ("null", "boolean", "int", "long", "double", "string"):
            validate_schema(self._base(t))

    def test_array_of_primitives_and_records(self):
        validate_schema(rec("Root", [field("a", arr("string")),
                                     field("b", arr(rec("Inner", [field("x", "int")],
                                                        namespace=None)))]))

    def test_union_of_two_distinct_types(self):
        validate_schema(self._base(["null", "string"]))
        validate_schema(self._base(["int", "long"]))
        validate_schema(self._base([arr("string"), "null"]))

    def test_union_must_have_exactly_two_branches(self):
        with self.assertRaisesRegex(SchemaError, "union 必须恰好包含两种不同类型"):
            validate_schema(self._base(["null"]))
        with self.assertRaisesRegex(SchemaError, "union 必须恰好包含两种不同类型"):
            validate_schema(self._base(["null", "string", "int"]))

    def test_union_duplicate_branch_rejected(self):
        with self.assertRaisesRegex(SchemaError, "分支类型重复"):
            validate_schema(self._base(["string", "string"]))

    def test_enum_rejected(self):
        with self.assertRaisesRegex(SchemaError, "enum"):
            validate_schema(rec("Root", [field("f", {"type": "enum", "name": "E",
                                                      "symbols": ["A"]})]))

    def test_map_rejected(self):
        with self.assertRaisesRegex(SchemaError, "map"):
            validate_schema(rec("Root", [field("f", {"type": "map", "values": "string"})]))

    def test_fixed_rejected(self):
        with self.assertRaisesRegex(SchemaError, "fixed"):
            validate_schema(rec("Root", [field("f", {"type": "fixed", "name": "F",
                                                      "size": 4})]))

    def test_logical_type_rejected(self):
        with self.assertRaisesRegex(SchemaError, "logicalType"):
            validate_schema(rec("Root", [field("f", {"type": "long",
                                                      "logicalType": "timestamp-millis"})]))

    def test_root_must_be_record(self):
        with self.assertRaisesRegex(SchemaError, "根 schema 必须是 record"):
            validate_schema({"type": "array", "items": "string"})


class TestRecursionAndRefs(unittest.TestCase):
    def test_direct_recursion_rejected(self):
        # 字符串自引用：递归引用
        schema = {
            "type": "record", "name": "Node", "namespace": "demo",
            "fields": [{"name": "next", "type": "demo.Node"}],
        }
        with self.assertRaisesRegex(SchemaError, "递归"):
            validate_schema(schema)

    def test_forward_reference_rejected(self):
        schema = rec("Outer", [
            field("early", "demo.Later"),
            field("later", rec("Later", [field("x", "int")])),
        ])
        with self.assertRaisesRegex(SchemaError, "未定义的类型引用"):
            validate_schema(schema)

    def test_duplicate_inline_record_must_match(self):
        inner = rec("Inner", [field("x", "int")], namespace=None)
        good = rec("Root", [field("a", inner),
                            field("b", rec("Inner", [field("x", "int")], namespace=None))],
                   namespace=None)
        validate_schema(good)

        bad = rec("Root", [field("a", inner),
                           field("b", rec("Inner", [field("x", "string")], namespace=None))],
                  namespace=None)
        with self.assertRaisesRegex(SchemaError, "结构不一致"):
            validate_schema(bad)

    def test_namespace_inheritance(self):
        schema = rec("Event", [field("p", rec("Profile", [field("score", "int")],
                                              namespace=None))])
        validate_schema(schema)
        self.assertEqual(schema["fields"][0]["type"]["_fullname"], "demo.Profile")


class TestDefaults(unittest.TestCase):
    def test_valid_defaults(self):
        validate_schema(rec("Root", [
            field("s", "string", default="x"),
            field("n", "int", default=3),
            field("b", "boolean", default=False),
            field("d", "double", default=1.5),
            field("a", arr("string"), default=["z"]),
            field("u", ["null", "string"], default=None),
        ]))

    def test_string_default_must_be_string(self):
        with self.assertRaisesRegex(SchemaError, "string 字段的 default 必须是字符串"):
            validate_schema(rec("Root", [field("s", "string", default=123)]))

    def test_int_default_range(self):
        with self.assertRaisesRegex(SchemaError, "32 位整数"):
            validate_schema(rec("Root", [field("n", "int", default=2**31)]))

    def test_boolean_default_rejects_int(self):
        with self.assertRaisesRegex(SchemaError, "布尔值"):
            validate_schema(rec("Root", [field("b", "boolean", default=1)]))

    def test_union_default_uses_first_branch(self):
        # 首分支是 null，default 必须是 null
        with self.assertRaisesRegex(SchemaError, "null 字段的 default 必须是 null"):
            validate_schema(rec("Root", [field("u", ["null", "int"], default=1)]))
        # 首分支是 int 时 default 可以是整数
        validate_schema(rec("Root", [field("u", ["int", "null"], default=1)]))

    def test_array_default_element_type(self):
        with self.assertRaisesRegex(SchemaError, "string 字段的 default 必须是字符串"):
            validate_schema(rec("Root", [field("a", arr("string"), default=[1, 2])]))

    def test_nested_record_default(self):
        inner = rec("Meta", [field("level", "int"),
                             field("tag", "string", default="z")], namespace=None)
        # 子字段自带 default 时，外层 record default 可省略该子字段
        validate_schema(rec("Root", [field("m", inner, default={"level": 0})]))
        # 子字段值类型错误必须检出
        with self.assertRaisesRegex(SchemaError, "32 位整数"):
            validate_schema(rec("Root", [
                field("m", rec("Meta", [field("level", "int")], namespace=None),
                      default={"level": "oops"})]))
        # 数组内嵌 record 的 default
        validate_schema(rec("Root", [
            field("ms", arr(inner), default=[{"level": 1}])]))

    def test_logical_type_rejected_inside_array(self):
        with self.assertRaisesRegex(SchemaError, "logicalType"):
            validate_schema(rec("Root", [
                field("f", arr({"type": "long", "logicalType": "timestamp-millis"}))]))


if __name__ == "__main__":
    unittest.main()
