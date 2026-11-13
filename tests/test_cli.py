"""CLI 子命令的进程级测试，检查退出码与产物。"""

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout

from merkle_dataset.cli import main

from .helpers import WorkDir


class CliTest(unittest.TestCase):
    def setUp(self):
        self.wd = WorkDir()

    def _run(self, argv):
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_full_lifecycle(self):
        jsonl = self.wd.path("d.jsonl")
        db = self.wd.path("d.db")
        manifest = self.wd.path("m.json")
        queries = self.wd.path("q.json")
        proof = self.wd.path("p.json")

        code, out, _ = self._run(["generate", "--count", "20", "--output", jsonl])
        self.assertEqual(code, 0)
        with open(jsonl, encoding="utf-8") as f:
            self.assertEqual(sum(1 for _ in f), 20)

        code, out, _ = self._run(
            ["build", "--input", jsonl, "--db", db, "--manifest", manifest]
        )
        self.assertEqual(code, 0)
        self.assertIn("root", out)

        keys = [json.loads(line)["key"] for line in open(jsonl, encoding="utf-8")]
        self.wd.write_queries("q.json", [keys[0], keys[-1], "不存在的键"])
        code, out, _ = self._run(
            ["prove", "--db", db, "--manifest", manifest,
             "--queries", queries, "--output", proof]
        )
        self.assertEqual(code, 0)
        self.assertIn("证明体积", out)
        self.assertIn("[存在]", out)
        self.assertIn("[缺失]", out)

        code, out, _ = self._run(
            ["verify", "--manifest", manifest, "--queries", queries, "--proof", proof]
        )
        self.assertEqual(code, 0)
        self.assertIn("全部核对通过", out)

        for target in (
            ["inspect", "--manifest", manifest],
            ["inspect", "--db", db, "--manifest", manifest],
            ["inspect", "--proof", proof],
            ["inspect", "--db", db, "--json"],
        ):
            code, out, _ = self._run(target)
            self.assertEqual(code, 0, target)

    def test_verify_failure_exit_code(self):
        jsonl = self.wd.write_jsonl("d.jsonl", [("a", "1"), ("b", "2")])
        db = self.wd.path("d.db")
        manifest = self.wd.path("m.json")
        self.assertEqual(
            self._run(["build", "--input", jsonl, "--db", db, "--manifest", manifest])[0], 0
        )
        queries = self.wd.write_queries("q.json", ["a"])
        proof = self.wd.path("p.json")
        self.assertEqual(
            self._run(["prove", "--db", db, "--manifest", manifest,
                       "--queries", queries, "--output", proof])[0], 0
        )
        data = json.load(open(proof, encoding="utf-8"))
        data["answers"][0]["value"] = "tampered"
        with open(proof, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        code, _, err = self._run(
            ["verify", "--manifest", manifest, "--queries", queries, "--proof", proof]
        )
        self.assertEqual(code, 1)
        self.assertIn("错误", err)

    def test_build_duplicate_exit_code(self):
        jsonl = self.wd.write_jsonl("dup.jsonl", [("a", "1"), ("a", "2")])
        code, _, err = self._run(
            ["build", "--input", jsonl, "--db", self.wd.path("x.db"),
             "--manifest", self.wd.path("x.json")]
        )
        self.assertEqual(code, 1)
        self.assertIn("重复", err)


if __name__ == "__main__":
    unittest.main()
