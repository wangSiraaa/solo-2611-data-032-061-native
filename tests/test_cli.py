"""CLI 端到端测试：check/project 退出码、JSONL 序号、解码失败不留结果、
以及“样例未出现的 union 分支”完整验证流程。"""

import json
import os
import tempfile
import unittest

from fastavro import reader as avro_reader

from avro_compat.cli import main
from tests.helpers import arr, field, rec, write_avro, write_avsc


class CliTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.writer = rec("Event", [
            field("id", "long"),
            field("old_name", "string"),
            field("tags", arr("string")),
            field("contact", ["null", "string"]),
            field("profile", rec("Profile", [field("score", "int"),
                                             field("active", "boolean")])),
        ])
        # 与 fixtures/reader-ok 同构：改名 alias、int->long 提升、新字段带 default
        self.reader_ok = rec("Event", [
            field("id", "long"),
            field("name", "string", aliases=["old_name"]),
            field("tags", arr("string")),
            field("contact", ["null", "string"]),
            field("profile", rec("Profile", [field("score", "long"),
                                             field("active", "boolean")])),
            field("region", "string", default="unknown"),
        ])
        # string 分支改成 int：样例全是 null 时“碰巧可读”，但 schema 级不兼容
        self.reader_bad = json.loads(json.dumps(self.reader_ok))
        self.reader_bad["fields"][3]["type"] = ["null", "int"]

    def _paths(self, schema_w, schema_r, records=None, name="m"):
        wp = write_avsc(self.tmp, "writer.avsc", schema_w)
        rp = write_avsc(self.tmp, "reader.avsc", schema_r)
        if records is None:
            return wp, rp, None
        mp = write_avro(self.tmp, f"{name}.avro", schema_w, records)
        return wp, rp, mp

    def test_check_ok_exit_zero(self):
        wp, rp, _ = self._paths(self.writer, self.reader_ok)
        plan = os.path.join(self.tmp, "plan.json")
        self.assertEqual(main(["check", wp, rp, "--plan", plan]), 0)
        payload = json.load(open(plan, encoding="utf-8"))
        self.assertTrue(payload["compatible"])

    def test_check_bad_detects_unobserved_union_branch(self):
        wp, rp, _ = self._paths(self.writer, self.reader_bad)
        self.assertEqual(main(["check", wp, rp]), 1)

    def test_check_invalid_schema_exit_two(self):
        bad = rec("Event", [field("f", {"type": "enum", "name": "E",
                                        "symbols": ["A"]})])
        wp = write_avsc(self.tmp, "w.avsc", self.writer)
        rp = write_avsc(self.tmp, "r.avsc", bad)
        self.assertEqual(main(["check", wp, rp]), 2)

    def test_project_refuses_when_incompatible(self):
        records = [
            {"id": 1, "old_name": "a", "tags": [], "contact": None,
             "profile": {"score": 1, "active": True}},
        ]
        wp, rp, mp = self._paths(self.writer, self.reader_bad, records)
        out = os.path.join(self.tmp, "out.jsonl")
        self.assertEqual(main(["project", wp, rp, mp, "--out", out]), 1)
        self.assertFalse(os.path.exists(out), "不兼容时不得产生投影输出")

    def test_project_jsonl_preserves_order_and_index(self):
        records = [
            {"id": 1, "old_name": "alpha", "tags": ["a"], "contact": None,
             "profile": {"score": 5, "active": True}},
            {"id": 2, "old_name": "beta", "tags": [], "contact": None,
             "profile": {"score": 8, "active": False}},
        ]
        wp, rp, mp = self._paths(self.writer, self.reader_ok, records)
        out = os.path.join(self.tmp, "out.jsonl")
        plan = os.path.join(self.tmp, "plan.json")
        self.assertEqual(main(["project", wp, rp, mp, "--out", out,
                               "--plan", plan]), 0)
        rows = [json.loads(line) for line in open(out, encoding="utf-8")]
        self.assertEqual([r["index"] for r in rows], [0, 1])
        self.assertEqual(list(rows[0]["record"].keys()),
                         ["id", "name", "tags", "contact", "profile", "region"])
        self.assertEqual(rows[0]["record"]["name"], "alpha")
        self.assertEqual(rows[0]["record"]["region"], "unknown")
        self.assertEqual(rows[1]["record"]["profile"]["score"], 8)
        plan_payload = json.load(open(plan, encoding="utf-8"))
        self.assertTrue(plan_payload["compatible"])

    def test_add_record_for_previously_unobserved_branch(self):
        """完整验证流程：先证明缺分支样例时也能检出不兼容；
        再为该 union 分支补一条记录，核对预检结论与实际读取一致。"""
        null_only = [
            {"id": 1, "old_name": "a", "tags": [], "contact": None,
             "profile": {"score": 1, "active": True}},
        ]
        wp, rp_bad, mp_null = self._paths(self.writer, self.reader_bad,
                                          null_only, name="nullonly")
        # 1) 即便没有任何 string 分支样例，预检仍然失败
        self.assertEqual(main(["check", wp, rp_bad]), 1)

        # 2) fastavro 交叉检查：它读 null_only 不报错（样例碰巧可读），
        #    证明“样例成功”不能代替全模式兼容证明
        with open(mp_null, "rb") as fh:
            decoded = list(avro_reader(fh, reader_schema=self.reader_bad))
        self.assertEqual(decoded[0]["contact"], None)
        self.assertEqual(len(decoded), 1)

        # 3) 补一条 string 分支记录：bad reader 下实际读取也会暴露问题
        with_branch = null_only + [
            {"id": 2, "old_name": "b", "tags": [], "contact": "mail@x.io",
             "profile": {"score": 2, "active": False}},
        ]
        mp_full = write_avro(self.tmp, "full.avro", self.writer, with_branch)
        out_bad = os.path.join(self.tmp, "bad.jsonl")
        self.assertEqual(main(["project", wp, rp_bad, mp_full,
                               "--out", out_bad]), 1)
        self.assertFalse(os.path.exists(out_bad))

        # 4) 同样的数据在 ok reader 下预检通过且投影正确，string 分支被保留
        rp_ok = write_avsc(self.tmp, "reader_ok.avsc", self.reader_ok)
        out_ok = os.path.join(self.tmp, "ok.jsonl")
        self.assertEqual(main(["project", wp, rp_ok, mp_full,
                               "--out", out_ok]), 0)
        rows = [json.loads(line) for line in open(out_ok, encoding="utf-8")]
        self.assertEqual([r["record"]["contact"] for r in rows],
                         [None, "mail@x.io"])

    def test_corrupt_avro_leaves_no_output(self):
        wp, rp, _ = self._paths(self.writer, self.reader_ok)
        corrupt = os.path.join(self.tmp, "corrupt.avro")
        with open(corrupt, "wb") as fh:
            fh.write(b"this is not an avro file at all")
        out = os.path.join(self.tmp, "out.jsonl")
        self.assertEqual(main(["project", wp, rp, corrupt, "--out", out]), 3)
        self.assertFalse(os.path.exists(out))

    def test_output_failure_leaves_no_orphan_plan(self):
        records = [
            {"id": 1, "old_name": "a", "tags": [], "contact": None,
             "profile": {"score": 1, "active": True}},
        ]
        wp, rp, mp = self._paths(self.writer, self.reader_ok, records)
        plan = os.path.join(self.tmp, "plan.json")
        # out 指向一个已存在的目录，文件写入必然失败
        out_dir = os.path.join(self.tmp, "adirectory")
        os.makedirs(out_dir)
        rc = main(["project", wp, rp, mp, "--out", out_dir, "--plan", plan])
        self.assertEqual(rc, 3)
        self.assertFalse(os.path.exists(plan), "不得留下孤立的计划文件")

    def test_writer_schema_mismatch_with_file_rejected(self):
        records = [{"id": 1, "old_name": "a", "tags": [], "contact": None,
                    "profile": {"score": 1, "active": True}}]
        mp = write_avro(self.tmp, "m.avro", self.writer, records)
        other_writer = rec("Event", [field("id", "long"), field("other", "string")])
        wp = write_avsc(self.tmp, "other.avsc", other_writer)
        rp = write_avsc(self.tmp, "r.avsc", self.reader_ok)
        out = os.path.join(self.tmp, "out.jsonl")
        # 给定 writer 与 reader 在 schema 级即不兼容（reader 字段无来源），
        # 预检先失败、退出码 1，不会走到内嵌 schema 比对
        self.assertEqual(main(["project", wp, rp, mp, "--out", out]), 1)
        self.assertFalse(os.path.exists(out))

    def test_embedded_schema_normalization_mismatch_exit_three(self):
        # 与文件同构兼容、但字段类型被悄悄改写的 writer schema：
        # 预检能过，内嵌 schema 交叉核对必须拦下
        records = [{"id": 1, "old_name": "a", "tags": [], "contact": None,
                    "profile": {"score": 1, "active": True}}]
        mp = write_avro(self.tmp, "m.avro", self.writer, records)
        tweaked = json.loads(json.dumps(self.writer))
        tweaked["fields"][0]["type"] = "int"  # long -> int
        wp = write_avsc(self.tmp, "tweaked.avsc", tweaked)
        rp = write_avsc(self.tmp, "r.avsc", self.reader_ok)
        out = os.path.join(self.tmp, "out.jsonl")
        self.assertEqual(main(["project", wp, rp, mp, "--out", out]), 3)
        self.assertFalse(os.path.exists(out))


if __name__ == "__main__":
    unittest.main()
