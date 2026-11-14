"""投影执行测试：顺序、来源、提升、default 隔离、不可变性、fail-fast。"""

import copy
import unittest

from avro_compat.projection import ProjectionError, project_records
from avro_compat.resolver import build_plan
from tests.helpers import arr, field, rec


def make(writer, reader, records):
    report = build_plan(writer, reader)
    assert report.compatible, report.errors
    return report, project_records(report, records)


class TestProjectionBasics(unittest.TestCase):
    def test_reader_field_order_and_rename(self):
        writer = rec("Event", [field("id", "long"), field("old_name", "string")])
        reader = rec("Event", [field("name", "string", aliases=["old_name"]),
                               field("id", "long")])
        _, out = make(writer, reader, [{"id": 7, "old_name": "x"}])
        self.assertEqual(list(out[0].keys()), ["name", "id"])
        self.assertEqual(out[0], {"name": "x", "id": 7})

    def test_dropped_fields_absent(self):
        writer = rec("Event", [field("id", "long"), field("secret", "string")])
        reader = rec("Event", [field("id", "long")])
        _, out = make(writer, reader, [{"id": 1, "secret": "s"}])
        self.assertNotIn("secret", out[0])

    def test_numeric_promotion_int_to_long_and_double(self):
        writer = rec("R", [field("a", "int"), field("b", "int")])
        reader = rec("R", [field("a", "long"), field("b", "double")])
        _, out = make(writer, reader, [{"a": 5, "b": 5}])
        self.assertEqual(out[0]["a"], 5)
        self.assertEqual(out[0]["b"], 5.0)
        self.assertIsInstance(out[0]["b"], float)

    def test_long_to_double(self):
        writer = rec("R", [field("a", "long")])
        reader = rec("R", [field("a", "double")])
        _, out = make(writer, reader, [{"a": 9}])
        self.assertEqual(out[0]["a"], 9.0)

    def test_nested_record_and_array(self):
        writer = rec("Event", [
            field("tags", arr("string")),
            field("p", rec("Profile", [field("score", "int")])),
        ])
        reader = rec("Event", [
            field("tags", arr("string")),
            field("p", rec("Profile", [field("score", "long")])),
        ])
        _, out = make(writer, reader, [
            {"tags": ["a", "b"], "p": {"score": 3}}])
        self.assertEqual(out[0], {"tags": ["a", "b"], "p": {"score": 3}})


class TestUnionProjection(unittest.TestCase):
    def test_union_branches_selected_at_runtime(self):
        writer = rec("R", [field("contact", ["null", "string"])])
        reader = rec("R", [field("contact", ["null", "string"])])
        report, out = make(writer, reader, [
            {"contact": None}, {"contact": "a@b.c"}, {"contact": None}])
        self.assertEqual([r["contact"] for r in out], [None, "a@b.c", None])

    def test_writer_union_promoted_branch(self):
        writer = rec("R", [field("v", ["int", "long"])])
        reader = rec("R", [field("v", "long")])
        _, out = make(writer, reader, [{"v": 1}, {"v": 2**40}])
        self.assertEqual(out, [{"v": 1}, {"v": 2**40}])

    def test_array_union_branch(self):
        writer = rec("R", [field("f", ["null", arr("string")])])
        reader = rec("R", [field("f", ["null", arr("string")])])
        _, out = make(writer, reader, [{"f": None}, {"f": ["x"]}])
        self.assertEqual(out, [{"f": None}, {"f": ["x"]}])


class TestDefaultsAndNullSemantics(unittest.TestCase):
    def test_new_field_uses_default_every_row(self):
        writer = rec("Event", [field("id", "long")])
        reader = rec("Event", [field("id", "long"),
                               field("region", "string", default="unknown")])
        _, out = make(writer, reader, [{"id": 1}, {"id": 2}])
        self.assertEqual([r["region"] for r in out], ["unknown", "unknown"])

    def test_array_default_not_shared_between_records(self):
        writer = rec("Event", [field("id", "long")])
        reader = rec("Event", [field("id", "long"),
                               field("flags", arr("string"), default=[])])
        report = build_plan(writer, reader)
        out = project_records(report, [{"id": 1}, {"id": 2}])
        out[0]["flags"].append("mutated")
        self.assertEqual(out[1]["flags"], [], "数组 default 在记录间被共享了")

    def test_record_default_not_shared_between_records(self):
        inner = rec("Meta", [field("level", "int", default=0)], namespace=None)
        writer = rec("Event", [field("id", "long")])
        reader = rec("Event", [
            field("id", "long"),
            field("meta", inner, default={"level": 0}),
        ])
        report = build_plan(writer, reader)
        self.assertTrue(report.compatible, report.errors)
        out = project_records(report, [{"id": 1}, {"id": 2}])
        out[0]["meta"]["level"] = 99
        self.assertEqual(out[1]["meta"]["level"], 0, "record default 在记录间被共享了")

    def test_explicit_null_distinct_from_missing_field(self):
        # writer 字段是 union 且显式为 null：必须按 null 分支投影，
        # 绝不能因为值是 None 就误用 reader default
        writer = rec("R", [field("note", ["null", "string"])])
        reader = rec("R", [field("note", ["null", "string"], default=None)])
        _, out = make(writer, reader, [{"note": None}])
        self.assertIn("note", out[0])
        self.assertIsNone(out[0]["note"])

    def test_default_deep_copied_from_plan(self):
        writer = rec("R", [field("id", "long")])
        reader = rec("R", [field("id", "long"),
                           field("flags", arr("string"), default=["x"])])
        report = build_plan(writer, reader)
        out1 = project_records(report, [{"id": 1}])
        out2 = project_records(report, [{"id": 1}])
        out1[0]["flags"].append("y")
        self.assertEqual(out2[0]["flags"], ["x"], "计划里的 default 被投影污染了")


class TestImmutabilityAndFailFast(unittest.TestCase):
    def test_source_record_not_mutated(self):
        writer = rec("R", [field("a", "int"), field("tags", arr("string"))])
        reader = rec("R", [field("a", "long"), field("tags", arr("string"))])
        src = [{"a": 1, "tags": ["x"]}]
        snapshot = copy.deepcopy(src)
        report = build_plan(writer, reader)
        project_records(report, src)
        self.assertEqual(src, snapshot)

    def test_incompatible_report_never_projects(self):
        writer = rec("R", [field("f", "string")])
        reader = rec("R", [field("f", "int")])
        report = build_plan(writer, reader)
        self.assertFalse(report.compatible)
        with self.assertRaises(Exception):
            project_records(report, [{"f": "1"}])

    def test_runtime_value_violating_plan_raises(self):
        writer = rec("R", [field("f", "long")])
        reader = rec("R", [field("f", "long")])
        report = build_plan(writer, reader)
        with self.assertRaises(ProjectionError):
            project_records(report, [{"f": "not-a-number"}])

    def test_missing_writer_field_raises_instead_of_default_substitution(self):
        writer = rec("R", [field("f", "string")])
        reader = rec("R", [field("f", "string")])
        report = build_plan(writer, reader)
        with self.assertRaises(ProjectionError):
            project_records(report, [{}])


if __name__ == "__main__":
    unittest.main()
