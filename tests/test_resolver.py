"""writer->reader 解析计划与兼容性判定测试。

重点：兼容性是 schema 级证明，union 中样例从未出现的分支也必须被检出。
"""

import unittest

from avro_compat.resolver import build_plan
from avro_compat.schema import SchemaError
from tests.helpers import arr, field, rec


def plan_of(writer, reader):
    return build_plan(writer, reader, "writer.avsc", "reader.avsc")


class TestPrimitivesAndPromotion(unittest.TestCase):
    def test_identical_primitives_compatible(self):
        for t in ("null", "boolean", "int", "long", "double", "string"):
            report = plan_of(rec("R", [field("f", t)]), rec("R", [field("f", t)]))
            self.assertTrue(report.compatible, report.errors)

    def test_numeric_promotions(self):
        cases = [("int", "long"), ("int", "double"), ("long", "double")]
        for w, r in cases:
            report = plan_of(rec("R", [field("f", w)]), rec("R", [field("f", r)]))
            self.assertTrue(report.compatible, f"{w}->{r}: {report.errors}")
            node = report.plan["fields"][0]["resolution"]
            self.assertTrue(node["promoted"])
            self.assertEqual((node["writer_type"], node["reader_type"]), (w, r))

    def test_string_to_number_is_incompatible(self):
        report = plan_of(rec("R", [field("f", "string")]),
                         rec("R", [field("f", "int")]))
        self.assertFalse(report.compatible)
        self.assertIn("不能按 Avro 规则", report.errors[0]["reason"])
        # 双方 schema 路径都要给出
        self.assertTrue(report.errors[0]["writer_path"].startswith("$.fields"))
        self.assertTrue(report.errors[0]["reader_path"].startswith("$.fields"))

    def test_long_to_int_is_incompatible(self):
        report = plan_of(rec("R", [field("f", "long")]),
                         rec("R", [field("f", "int")]))
        self.assertFalse(report.compatible)

    def test_boolean_cannot_promote(self):
        report = plan_of(rec("R", [field("f", "boolean")]),
                         rec("R", [field("f", "int")]))
        self.assertFalse(report.compatible)

    def test_structural_mismatch_array_vs_string(self):
        report = plan_of(rec("R", [field("f", arr("string"))]),
                         rec("R", [field("f", "string")]))
        self.assertFalse(report.compatible)
        self.assertIn("结构不同", report.errors[0]["reason"])


class TestRecordFields(unittest.TestCase):
    def setUp(self):
        self.profile_w = rec("Profile", [field("score", "int"),
                                         field("active", "boolean")])
        self.profile_r = rec("Profile", [field("score", "long"),
                                         field("active", "boolean")])

    def test_reader_drops_writer_fields(self):
        writer = rec("Event", [field("id", "long"), field("secret", "string")])
        reader = rec("Event", [field("id", "long")])
        report = plan_of(writer, reader)
        self.assertTrue(report.compatible, report.errors)
        self.assertEqual(report.plan["dropped_writer_fields"], ["secret"])

    def test_reader_new_field_requires_default(self):
        writer = rec("Event", [field("id", "long")])
        reader_bad = rec("Event", [field("id", "long"), field("region", "string")])
        report = plan_of(writer, reader_bad)
        self.assertFalse(report.compatible)
        self.assertIn("缺少合法 default", report.errors[0]["reason"])
        self.assertIn("region", report.errors[0]["reader_path"])

        reader_ok = rec("Event", [field("id", "long"),
                                  field("region", "string", default="unknown")])
        report = plan_of(writer, reader_ok)
        self.assertTrue(report.compatible, report.errors)
        entry = report.plan["fields"][1]
        self.assertEqual(entry["source"], "default")
        self.assertEqual(entry["default"], "unknown")

    def test_nested_record_field_resolution(self):
        writer = rec("Event", [field("p", self.profile_w)])
        reader = rec("Event", [field("p", self.profile_r)])
        report = plan_of(writer, reader)
        self.assertTrue(report.compatible, report.errors)
        sub = report.plan["fields"][0]["resolution"]
        self.assertEqual(sub["kind"], "record")
        score = sub["fields"][0]["resolution"]
        self.assertEqual((score["writer_type"], score["reader_type"]), ("int", "long"))

    def test_record_name_mismatch_rejected(self):
        writer = rec("Event", [field("p", rec("ProfileA", [field("x", "int")]))])
        reader = rec("Event", [field("p", rec("ProfileB", [field("x", "int")]))])
        report = plan_of(writer, reader)
        self.assertFalse(report.compatible)
        self.assertIn("record 名称不匹配", report.errors[0]["reason"])

    def test_record_alias_matches_old_name(self):
        writer = rec("Event", [field("p", rec("Profile", [field("x", "int")]))])
        reader = rec("Event", [field("p", rec("ProfileV2", [field("x", "int")],
                                             aliases=["demo.Profile"]))])
        report = plan_of(writer, reader)
        self.assertTrue(report.compatible, report.errors)


class TestAliases(unittest.TestCase):
    def test_field_alias_rename(self):
        writer = rec("Event", [field("old_name", "string")])
        reader = rec("Event", [field("name", "string", aliases=["old_name"])])
        report = plan_of(writer, reader)
        self.assertTrue(report.compatible, report.errors)
        entry = report.plan["fields"][0]
        self.assertEqual(entry["source"], "writer")
        self.assertEqual(entry["writer_name"], "old_name")
        self.assertTrue(entry["renamed"])

    def test_alias_conflict_between_two_writer_fields(self):
        writer = rec("Event", [field("a", "string"), field("b", "string")])
        reader = rec("Event", [field("a", "string", aliases=["b"])])
        report = plan_of(writer, reader)
        self.assertFalse(report.compatible)
        self.assertIn("alias 冲突", report.errors[0]["reason"])
        self.assertIn("['a', 'b']", report.errors[0]["writer_path"])

    def test_unknown_alias_means_new_field_needs_default(self):
        writer = rec("Event", [field("id", "long")])
        reader = rec("Event", [field("name", "string", aliases=["ghost"])])
        report = plan_of(writer, reader)
        self.assertFalse(report.compatible)
        self.assertIn("缺少合法 default", report.errors[0]["reason"])


class TestUnionCoverage(unittest.TestCase):
    def test_every_writer_branch_must_be_covered(self):
        # 核心需求：即使样例只出现 null，string 分支不兼容也必须在预检中检出
        writer = rec("Event", [field("contact", ["null", "string"])])
        reader_good = rec("Event", [field("contact", ["null", "string"])])
        self.assertTrue(plan_of(writer, reader_good).compatible)

        reader_bad = rec("Event", [field("contact", ["null", "int"])])
        report = plan_of(writer, reader_bad)
        self.assertFalse(report.compatible)
        # 错误必须指出 string 分支与双方 union 路径
        joined = " ".join(e["writer_path"] + e["reason"] for e in report.errors)
        self.assertIn("string", joined)
        self.assertIn("[1]", joined)

    def test_writer_union_into_reader_non_union(self):
        # writer ["int","long"] -> reader long：int 提升 long、long 直通，均兼容
        writer = rec("R", [field("f", ["int", "long"])])
        reader = rec("R", [field("f", "long")])
        report = plan_of(writer, reader)
        self.assertTrue(report.compatible, report.errors)
        node = report.plan["fields"][0]["resolution"]
        self.assertEqual(node["kind"], "union")
        self.assertEqual([b["writer_branch"] for b in node["branches"]], ["int", "long"])

    def test_writer_non_union_into_reader_union(self):
        writer = rec("R", [field("f", "int")])
        reader = rec("R", [field("f", ["null", "long"])])
        report = plan_of(writer, reader)
        self.assertTrue(report.compatible, report.errors)
        node = report.plan["fields"][0]["resolution"]
        self.assertEqual(node["kind"], "union_match")
        self.assertEqual(node["reader_index"], 1)

    def test_union_branch_with_array(self):
        writer = rec("R", [field("f", ["null", arr("string")])])
        reader = rec("R", [field("f", ["null", arr("string")])])
        self.assertTrue(plan_of(writer, reader).compatible)

        reader_bad = rec("R", [field("f", ["null", arr("int")])])
        report = plan_of(writer, reader_bad)
        self.assertFalse(report.compatible)

    def test_union_branch_with_inline_record_is_fully_resolved(self):
        inner_w = rec("Addr", [field("city", "string")])
        inner_r_bad = rec("Addr", [field("city", "int")])  # string->int 不兼容
        writer = rec("R", [field("f", ["null", inner_w])])
        reader = rec("R", [field("f", ["null", inner_r_bad])])
        report = plan_of(writer, reader)
        self.assertFalse(report.compatible)
        err = report.errors[0]
        # 错误必须深入到 union 内 record 的具体字段，并给出双方路径
        self.assertIn("city", err["reader_path"])
        self.assertIn("string", err["reason"])
        self.assertTrue(err["writer_path"].startswith("$.fields[0]"))


class TestErrorPaths(unittest.TestCase):
    def test_errors_include_both_schema_paths(self):
        writer = rec("Event", [field("deep", rec("P", [field("v", "string")]))])
        reader = rec("Event", [field("deep", rec("P", [field("v", "long")]))])
        report = plan_of(writer, reader)
        self.assertEqual(len(report.errors), 1)
        err = report.errors[0]
        self.assertTrue(err["writer_path"].startswith("$.fields[0]"))
        self.assertTrue(err["reader_path"].startswith("$.fields[0]"))
        self.assertTrue(err["reason"])

    def test_multiple_errors_collected_at_once(self):
        writer = rec("Event", [field("a", "string"), field("b", "long")])
        reader = rec("Event", [field("a", "int"), field("b", "int")])
        report = plan_of(writer, reader)
        self.assertEqual(len(report.errors), 2)

    def test_invalid_schema_raises_schema_error(self):
        with self.assertRaises(SchemaError):
            build_plan(rec("R", [field("f", {"type": "map", "values": "string"})]),
                       rec("R", [field("f", "string")]))

    def test_nested_record_new_field_requires_default(self):
        writer = rec("R", [field("p", rec("P", [field("a", "int")]))])
        reader = rec("R", [field("p", rec("P", [field("a", "int"),
                                                 field("b", "string")]))])
        report = plan_of(writer, reader)
        self.assertFalse(report.compatible)
        self.assertIn("缺少合法 default", report.errors[0]["reason"])

    def test_array_item_incompatibility(self):
        report = plan_of(rec("R", [field("f", arr("int"))]),
                         rec("R", [field("f", arr("string"))]))
        self.assertFalse(report.compatible)
        self.assertIn("items", report.errors[0]["writer_path"])

    def test_deep_nested_path_quality(self):
        writer = rec("R", [field("p", rec("P", [
            field("q", rec("Q", [field("v", "string")]))]))])
        reader = rec("R", [field("p", rec("P", [
            field("q", rec("Q", [field("v", "long")]))]))])
        report = plan_of(writer, reader)
        err = report.errors[0]
        self.assertIn("q", err["writer_path"])
        self.assertIn("v", err["reader_path"])


if __name__ == "__main__":
    unittest.main()
