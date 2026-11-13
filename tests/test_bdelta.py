"""bdelta 完整测试套件（unittest，仅标准库）。

覆盖：
  * 头部插入一字节、中间插入与删除、重复旧块并列、完全不同、
    空文件、B 边界与尾块不足；
  * 独立补丁解释器逐字节重建并核对所有 COPY 来源；
  * 构造 Adler32 碰撞证明强校验会拒绝错误复用；
  * 破坏 ADD、越界 COPY、旧版本替换必须失败；
  * 扫描期间输入大小/mtime 变化必须失败；
  * 确定性（同输入两次补丁字节相同）与零长指令拒绝。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import threading
import time
import unittest
import zlib
from pathlib import Path

from bdelta.apply import ApplyError, apply_patch, verify_patch
from bdelta.checksums import RollingAdler, adler32
from bdelta.delta import make_delta
from bdelta.frozen import InputChangedError
from bdelta.generator import make_corpus
from bdelta.patch import PatchFormatError, PatchReader
from bdelta.signature import build_signature

from tests.independent import interpret_patch, patch_instructions

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = ROOT / ".runtime" / "tests"
B = 1024


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


class PipelineMixin:
    def _pipeline(self, old: bytes, new: bytes, block_size: int = B):
        d = self.tmp
        old_p, new_p = d / "old.bin", d / "new.bin"
        sig_p, patch_p, out_p = d / "f.sig", d / "f.patch", d / "out.bin"
        old_p.write_bytes(old)
        new_p.write_bytes(new)
        build_signature(str(old_p), str(sig_p), block_size)
        stats = make_delta(str(sig_p), str(new_p), str(patch_p))
        result = apply_patch(str(old_p), str(patch_p), str(out_p))
        self.assertEqual(out_p.read_bytes(), new)
        self.assertEqual(result["new_sha256"], hashlib.sha256(new).hexdigest())
        # 独立解释器 + COPY 来源逐字节核对。
        rebuilt, copies = interpret_patch(str(old_p), str(patch_p))
        self.assertEqual(rebuilt, new)
        verify_patch(str(old_p), str(patch_p), str(new_p))
        return stats, patch_p, old_p, new_p, out_p, copies


class BdeltaTest(PipelineMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = RUNTIME / self.id().split(".")[-1]
        self.tmp.mkdir(parents=True, exist_ok=True)

    # ---------- 基础功能 ----------

    def test_rolling_adler_matches_zlib(self) -> None:
        data = bytes((i * 37 + 11) % 256 for i in range(5000))
        block = 512
        r = RollingAdler(data[:block])
        self.assertEqual(r.value, adler32(data[:block]))
        for i in range(1, 4000):
            r.roll(data[i - 1], data[i + block - 1])
            self.assertEqual(r.value, adler32(data[i : i + block]))
        # 全 0xFF 制造反复取模回绕。
        data2 = b"\xff" * (block + 1000)
        r = RollingAdler(data2[:block])
        for i in range(1, 1000):
            r.roll(255, 255)
            self.assertEqual(r.value, adler32(data2[i : i + block]))

    def test_head_insert_one_byte(self) -> None:
        old = bytes((i * 7) % 256 for i in range(6 * B))
        new = b"X" + old
        stats, patch_p, *_ = self._pipeline(old, new)
        self.assertEqual(stats.copy_bytes, 6 * B)
        self.assertEqual(stats.add_bytes, 1)
        self.assertEqual(stats.bytes_scanned, len(new))
        _, recs = patch_instructions(str(patch_p))
        # 第一条是 1 字节 ADD，之后每个完整块各一条 COPY。
        adds = [r for r in recs if r["op"] == "ADD"]
        copies = [r for r in recs if r["op"] == "COPY"]
        self.assertEqual(len(base64.b64decode(adds[0]["data_b64"])), 1)
        self.assertEqual(len(adds), 1)
        self.assertEqual(len(copies), 6)
        # 贪心只按完整块对齐复用：新流位置 1+kB 的窗口内容等于旧块 k
        # （窗口与旧块边界错位 1 字节），因此每条 COPY 都引用旧偏移 0。
        self.assertEqual([c["old_offset"] for c in copies], [0] * 6)
        self.assertTrue(all(c["length"] == B for c in copies))

    def test_consecutive_copies_merge_when_old_offsets_contiguous(self) -> None:
        # 旧文件与新文件完全相同且为多块：新流相邻、旧偏移连续 -> 合并为一条。
        # 用哈希流避免块内容周期性重复（否则各块只会命中同一个旧偏移）。
        old = b"".join(
            hashlib.sha256(f"block-{k}".encode()).digest() * (B // 32)
            for k in range(6)
        )
        self.assertEqual(len(old), 6 * B)
        stats, patch_p, *_ = self._pipeline(old, old)
        _, recs = patch_instructions(str(patch_p))
        copies = [r for r in recs if r["op"] == "COPY"]
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0], {"op": "COPY", "old_offset": 0, "length": 6 * B})
        self.assertNotIn("ADD", [r["op"] for r in recs])
        self.assertEqual(stats.copy_bytes, 6 * B)

    def test_noncontiguous_copies_remain_separate(self) -> None:
        # 新流中相邻的两个窗口命中同一旧块（重复块）：旧偏移不连续，
        # 必须保留为两条独立 COPY，不能错误合并。
        pattern = hashlib.sha256(b"pat").digest() * (B // 32)
        other = hashlib.sha256(b"oth").digest() * (B // 32)
        old = pattern + other + pattern  # pattern 出现在偏移 0 与 2B
        new = pattern + pattern           # 新流相邻的两个窗口都命中偏移 0
        stats, patch_p, *_ = self._pipeline(old, new)
        _, recs = patch_instructions(str(patch_p))
        copies = [r for r in recs if r["op"] == "COPY"]
        self.assertEqual(len(copies), 2)
        self.assertEqual([c["old_offset"] for c in copies], [0, 0])
        self.assertEqual(stats.copy_bytes, 2 * B)

    def test_middle_insert_and_delete(self) -> None:
        old = bytes((i * 13) % 256 for i in range(8 * B))
        mid = 3 * B
        new = old[:mid] + b"Z" * (B + 33) + old[mid + B // 2 :]
        stats, *_ = self._pipeline(old, new)
        # 中点之前全部复用；之后整体错位半块，贪心继续逐字节滚动寻找。
        self.assertGreaterEqual(stats.copy_bytes, 3 * B)
        self.assertEqual(stats.copy_bytes + stats.add_bytes, len(new))

    def test_duplicate_blocks_tie_picks_min_offset(self) -> None:
        pattern = bytes(range(256)) * (B // 256)
        self.assertEqual(len(pattern), B)
        old = pattern + pattern + pattern + b"q" * B
        new = b"!" * 20 + pattern + b"?" * 10
        stats, patch_p, *_ = self._pipeline(old, new)
        _, recs = patch_instructions(str(patch_p))
        copies = [r for r in recs if r["op"] == "COPY"]
        self.assertEqual(len(copies), 1)
        # 并列候选必须选择最小旧偏移 0。
        self.assertEqual(copies[0]["old_offset"], 0)
        self.assertEqual(copies[0]["length"], B)
        self.assertGreaterEqual(stats.strong_checks, 1)

    def test_totally_different(self) -> None:
        old = bytes((i * 3) % 256 for i in range(4 * B))
        new = bytes((i * 5 + 1) % 256 for i in range(4 * B + 7))
        stats, *_ = self._pipeline(old, new)
        self.assertEqual(stats.copy_bytes, 0)
        self.assertEqual(stats.add_bytes, len(new))

    def test_empty_variants(self) -> None:
        for old, new in ((b"", b"abc" * 100), (b"abc" * 100, b""), (b"", b"")):
            stats, patch_p, old_p, new_p, out_p, copies = self._pipeline(old, new)
            self.assertEqual(stats.copy_bytes, 0)
            self.assertEqual(stats.add_bytes, len(new))
            self.assertEqual(copies, [])
            _, recs = patch_instructions(str(patch_p))
            ops = [r["op"] for r in recs]
            if not new:
                self.assertNotIn("ADD", ops)
                self.assertNotIn("COPY", ops)

    def test_block_boundaries_and_tail(self) -> None:
        cases = {
            "exact": (B, B),
            "one_short": (B - 1, B - 1),
            "one_over": (B + 1, B + 1),
            "two_plus_tail": (2 * B + 37, 2 * B + 37),
            "tail_to_full": (2 * B + 10, 3 * B),
        }
        for idx, (olen, nlen) in enumerate(cases.values()):
            data = bytes((i * 17 + idx) % 256 for i in range(max(olen, nlen)))
            old, new = data[:olen], data[:nlen]
            stats, patch_p, *_ = self._pipeline(old, new)
            self.assertEqual(stats.copy_bytes + stats.add_bytes, nlen)
            # 末块不足 B 永远不能作为 COPY 复用。
            _, recs = patch_instructions(str(patch_p))
            for r in recs:
                if r["op"] == "COPY":
                    self.assertEqual(r["length"] % B, 0)

    def test_full_corpus_cases(self) -> None:
        info = make_corpus(str(self.tmp / "corpus"), B)
        for name, (old_p, new_p, _desc) in info["paths"].items():
            sig = self.tmp / f"{name}.sig"
            patch = self.tmp / f"{name}.patch"
            out = self.tmp / f"{name}.out"
            build_signature(old_p, str(sig), B)
            make_delta(str(sig), new_p, str(patch))
            apply_patch(old_p, str(patch), str(out))
            self.assertEqual(
                Path(out).read_bytes(), Path(new_p).read_bytes(), msg=name
            )
            rebuilt, _ = interpret_patch(old_p, str(patch))
            self.assertEqual(rebuilt, Path(new_p).read_bytes(), msg=name)

    def test_tail_block_not_reuse_candidate(self) -> None:
        # 旧文件只有一个完整块 + 短尾；新文件把短尾内容放到完整窗口位置，
        # 尾块内容即使窗口匹配也不在候选中。
        old = b"A" * B + b"TAIL"
        new = b"TAIL" + b"A" * (B - 4)
        stats, *_ = self._pipeline(old, new)
        self.assertEqual(stats.copy_bytes, 0)
        self.assertEqual(stats.add_bytes, len(new))

    def test_deterministic_patch_output(self) -> None:
        old = bytes((i * 11) % 256 for i in range(5 * B))
        new = b"!" + old[: 2 * B] + b"#" + old[2 * B :]
        d = self.tmp
        (d / "o").write_bytes(old)
        (d / "n").write_bytes(new)
        build_signature(str(d / "o"), str(d / "s1"), B)
        build_signature(str(d / "o"), str(d / "s2"), B)
        make_delta(str(d / "s1"), str(d / "n"), str(d / "p1"))
        make_delta(str(d / "s2"), str(d / "n"), str(d / "p2"))
        self.assertEqual((d / "p1").read_bytes(), (d / "p2").read_bytes())

    # ---------- Adler32 碰撞：强校验必须拒绝错误复用 ----------

    def test_adler32_collision_rejected_by_strong_hash(self) -> None:
        block = 256
        # 构造两个不同的 B 字节块 A、C，Adler32 相同、SHA256 不同。
        # Adler 差量条件：sum(d)=0 且 sum(i*d_i)=0（i 从 0 起）。
        # 取位置 (10,31) 各 +1、位置 (20,21) 各 -1：
        #   sum(d)=1+1-1-1=0；sum(i*d_i)=10+31-20-21=0。
        A = bytearray(b"k" * block)
        C = bytearray(A)
        p_plus = (10, 31)
        p_minus = (20, 21)
        for p in p_plus:
            C[p] = (C[p] + 1) % 256
        for p in p_minus:
            C[p] = (C[p] - 1) % 256
        self.assertNotEqual(bytes(A), bytes(C))
        self.assertEqual(zlib.adler32(bytes(A)), zlib.adler32(bytes(C)))
        self.assertNotEqual(
            hashlib.sha256(bytes(A)).hexdigest(),
            hashlib.sha256(bytes(C)).hexdigest(),
        )

        # 旧文件：三个完整块 A，后面再补一个完整块保证长度为 B 的整数倍。
        tail = b"v" * block
        old = bytes(A) + bytes(A) + bytes(A) + tail
        # 新文件：碰撞块 C 出现在完整窗口位置（恰好 B 对齐），后接 tail。
        new = bytes(C) + tail
        stats, patch_p, old_p, new_p, out_p, copies = self._pipeline(
            old, new, block_size=block
        )
        # C 的弱值命中（A 是候选），至少发生一次强校验；强摘要不同 -> 拒绝。
        self.assertGreaterEqual(stats.weak_hits, 1)
        self.assertGreaterEqual(stats.strong_checks, 1)
        # C 与任何旧完整块都不相同，因此 C 区域不能产生 COPY。
        _, recs = patch_instructions(str(patch_p))
        self.assertTrue(all(r["op"] != "COPY" or r["old_offset"] >= 3 * block for r in recs))
        # tail 仍可正常复用（旧偏移 3B）。
        self.assertEqual(stats.copy_bytes, block)
        self.assertEqual(stats.add_bytes, block)

    def test_adler_collision_search_variant(self) -> None:
        # 用结构化搜索得到一个天然碰撞（生日悖论）。取 3 字节变化后缀，
        # 弱值的 a、b 两部分都随候选变化，弱值空间约 2^24，数万样本内
        # 即可稳定出现两个不同块拥有相同 Adler32。
        block = 256
        prefix = b"z" * (block - 3)
        seen: dict[int, bytes] = {}
        collision = None
        for v in range(120000):
            suffix = bytes(((v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF))
            cand = prefix + suffix
            weak = zlib.adler32(cand) & 0xFFFFFFFF
            if weak in seen and seen[weak] != cand:
                collision = (seen[weak], cand)
                break
            seen.setdefault(weak, cand)
        self.assertIsNotNone(collision, "搜索空间内应能找到 Adler32 碰撞")
        x, y = collision
        self.assertEqual(zlib.adler32(x), zlib.adler32(y))
        self.assertNotEqual(hashlib.sha256(x).digest(), hashlib.sha256(y).digest())

    # ---------- 恶意/损坏补丁必须失败 ----------

    def test_corrupt_add_must_fail(self) -> None:
        old = bytes((i * 7) % 256 for i in range(3 * B))
        new = b"!" + old
        stats, patch_p, old_p, new_p, out_p, _ = self._pipeline(old, new)
        lines = patch_p.read_bytes().splitlines()
        corrupted = list(lines)
        for idx, line in enumerate(corrupted):
            rec = json.loads(line)
            if rec.get("op") == "ADD":
                raw = bytearray(base64.b64decode(rec["data_b64"]))
                raw[0] ^= 0xFF
                rec["data_b64"] = base64.b64encode(bytes(raw)).decode()
                corrupted[idx] = json.dumps(rec, sort_keys=True).encode()
                break
        bad = self.tmp / "corrupt.patch"
        bad.write_bytes(b"\n".join(corrupted))
        with self.assertRaises((ApplyError, PatchFormatError)):
            apply_patch(str(old_p), str(bad), str(self.tmp / "should_not_exist"))
        self.assertFalse((self.tmp / "should_not_exist").exists())

    def test_out_of_bounds_copy_must_fail(self) -> None:
        old = b"a" * (2 * B)
        stats, patch_p, old_p, *_ = self._pipeline(old, b"a" * B)
        header, recs = patch_instructions(str(patch_p))
        bad_recs = [
            {"op": "COPY", "old_offset": 2 * B - 10, "length": 100}
        ]  # 末端越界
        text = [json.dumps(header, sort_keys=True)]
        text += [json.dumps(r, sort_keys=True) for r in bad_recs]
        text.append(json.dumps({"op": "END"}))
        bad = self.tmp / "oob.patch"
        bad.write_text("\n".join(text))
        with open(bad, "rb") as fh:
            with self.assertRaises(PatchFormatError):
                list(PatchReader(fh))
        with self.assertRaises((ApplyError, PatchFormatError)):
            apply_patch(str(old_p), str(bad), str(self.tmp / "no_oob"))

    def test_zero_length_instructions_rejected(self) -> None:
        old = b"m" * B
        stats, patch_p, old_p, *_ = self._pipeline(old, b"m" * B)
        header, _ = patch_instructions(str(patch_p))
        for bad_rec in (
            {"op": "COPY", "old_offset": 0, "length": 0},
            {"op": "ADD", "data_b64": ""},
        ):
            text = "\n".join(
                [
                    json.dumps(header, sort_keys=True),
                    json.dumps(bad_rec, sort_keys=True),
                    json.dumps({"op": "END"}),
                ]
            )
            bad = self.tmp / "zero.patch"
            bad.write_text(text)
            with open(bad, "rb") as fh:
                with self.assertRaises(PatchFormatError):
                    list(PatchReader(fh))

    def test_old_version_replacement_must_fail(self) -> None:
        old_v1 = bytes((i * 7) % 256 for i in range(3 * B))
        new = b"!" + old_v1
        stats, patch_p, old_p, new_p, out_p, _ = self._pipeline(old_v1, new)
        # 先成功发布一次。
        good_out = self.tmp / "good.bin"
        apply_patch(str(old_p), str(patch_p), str(good_out))
        good_bytes = good_out.read_bytes()
        # 用“另一个旧版本”替换旧文件（大小相同、内容不同）。
        old_v2 = bytes((i * 7 + 1) % 256 for i in range(3 * B))
        decoy = self.tmp / "decoy_old.bin"
        decoy.write_bytes(old_v2)
        self.assertEqual(decoy.stat().st_size, len(old_v1))
        self.assertNotEqual(sha256_file(str(decoy)), sha256_file(str(old_p)))
        failed_out = self.tmp / "failed.bin"
        with self.assertRaises(ApplyError):
            apply_patch(str(decoy), str(patch_p), str(failed_out))
        self.assertFalse(failed_out.exists())
        # 此前成功发布的文件完好无损。
        self.assertEqual(good_out.read_bytes(), good_bytes)

    def test_failed_apply_leaves_no_temp_and_keeps_good_file(self) -> None:
        old = b"".join(
            hashlib.sha256(f"b{k}".encode()).digest() * (B // 32) for k in range(4)
        )
        new = b"HEAD" + old
        stats, patch_p, old_p, new_p, good_out, _ = self._pipeline(old, new)
        good_bytes = good_out.read_bytes()
        # 解析阶段即失败的越界 COPY。
        header, _ = patch_instructions(str(patch_p))
        bad_text = "\n".join(
            [
                json.dumps(header, sort_keys=True),
                json.dumps(
                    {"op": "COPY", "old_offset": len(old) - 5, "length": 100},
                    sort_keys=True,
                ),
                json.dumps({"op": "END"}),
            ]
        )
        bad_patch = self.tmp / "oob2.patch"
        bad_patch.write_text(bad_text)
        before = set(os.listdir(self.tmp))
        with self.assertRaises((ApplyError, PatchFormatError)):
            apply_patch(str(old_p), str(bad_patch), str(good_out))
        after = set(os.listdir(self.tmp))
        leftovers = [n for n in (after - before) if n.endswith(".tmp")]
        self.assertEqual(leftovers, [])
        # 已发布的成功文件原样保留。
        self.assertEqual(good_out.read_bytes(), good_bytes)

    def test_bad_target_hash_in_header_must_fail(self) -> None:
        old = b"h" * (2 * B)
        stats, patch_p, old_p, *_ = self._pipeline(old, b"h" * (2 * B))
        header, recs = patch_instructions(str(patch_p))
        header["new_sha256"] = "f" * 64
        text = "\n".join(
            [json.dumps(header, sort_keys=True)]
            + [json.dumps(r, sort_keys=True) for r in recs]
            + [json.dumps({"op": "END"})]
        )
        bad = self.tmp / "badhash.patch"
        bad.write_text(text)
        with self.assertRaises(ApplyError):
            apply_patch(str(old_p), str(bad), str(self.tmp / "nohash"))

    # ---------- 冻结输入 ----------

    def test_signature_fails_when_input_grows(self) -> None:
        path = self.tmp / "changing.bin"
        path.write_bytes(b"0" * (32 * 1024 * 1024))
        stop = threading.Event()

        def grow() -> None:
            with open(path, "ab") as fh:
                while not stop.is_set():
                    fh.write(b"1" * (1024 * 1024))
                    fh.flush()
                    os.fsync(fh.fileno())
                    time.sleep(0.0005)

        t = threading.Thread(target=grow)
        t.start()
        try:
            with self.assertRaises(InputChangedError):
                build_signature(str(path), str(self.tmp / "x.sig"), B)
        finally:
            stop.set()
            t.join()

    def test_signature_fails_when_mtime_changes(self) -> None:
        path = self.tmp / "touched.bin"
        path.write_bytes(b"0" * (32 * 1024 * 1024))
        stop = threading.Event()

        def touch() -> None:
            while not stop.is_set():
                os.utime(path, None)
                time.sleep(0.0005)

        t = threading.Thread(target=touch)
        t.start()
        try:
            with self.assertRaises(InputChangedError):
                build_signature(str(path), str(self.tmp / "y.sig"), B)
        finally:
            stop.set()
            t.join()

    def test_delta_fails_when_new_file_grows(self) -> None:
        old_p = self.tmp / "old.bin"
        new_p = self.tmp / "new.bin"
        old_p.write_bytes(b"9" * (4 * B))
        new_p.write_bytes(b"9" * (32 * 1024 * 1024))
        build_signature(str(old_p), str(self.tmp / "s.sig"), B)
        stop = threading.Event()

        def grow() -> None:
            with open(new_p, "ab") as fh:
                while not stop.is_set():
                    fh.write(b"8" * (1024 * 1024))
                    fh.flush()
                    os.fsync(fh.fileno())
                    time.sleep(0.0005)

        t = threading.Thread(target=grow)
        t.start()
        try:
            with self.assertRaises(InputChangedError):
                make_delta(
                    str(self.tmp / "s.sig"),
                    str(new_p),
                    str(self.tmp / "p.patch"),
                )
        finally:
            stop.set()
            t.join()

    # ---------- 参数与签名格式 ----------

    def test_block_size_bounds(self) -> None:
        old_p = self.tmp / "o.bin"
        old_p.write_bytes(b"x" * 1000)
        for bad in (255, 65537, 0, -1):
            with self.assertRaises(ValueError):
                build_signature(str(old_p), str(self.tmp / "s.sig"), bad)
        for good in (256, 65536):
            build_signature(str(old_p), str(self.tmp / f"s{good}.sig"), good)

    def test_signature_records_full_blocks_and_file_identity(self) -> None:
        old_p = self.tmp / "o.bin"
        data = b"r" * (2 * B + 13)
        old_p.write_bytes(data)
        sig = self.tmp / "o.sig"
        info = build_signature(str(old_p), str(sig), B)
        self.assertEqual(info["block_count"], 2)
        self.assertEqual(info["tail_size"], 13)
        conn = sqlite3.connect(f"file:{sig}?mode=ro", uri=True)
        rows = conn.execute(
            "SELECT offset, length, adler32, sha256 FROM blocks ORDER BY offset"
        ).fetchall()
        self.assertEqual([r[0] for r in rows], [0, B])
        for off, length, weak, strong in rows:
            self.assertEqual(length, B)
            self.assertEqual(weak, adler32(data[off : off + B]))
            self.assertEqual(
                strong, hashlib.sha256(data[off : off + B]).hexdigest()
            )
        meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        self.assertEqual(meta["old_size"], len(data))
        text_meta = dict(
            conn.execute("SELECT key, value FROM meta_text").fetchall()
        )
        self.assertEqual(text_meta["old_sha256"], hashlib.sha256(data).hexdigest())
        conn.close()

    def test_delta_never_reads_old_file_body(self) -> None:
        # 签名生成后删除旧文件：delta 只依赖签名与新文件，必须照常成功。
        old = b"".join(
            hashlib.sha256(f"k{k}".encode()).digest() * (B // 32) for k in range(4)
        )
        new = b"!" + old
        old_p, new_p = self.tmp / "old.bin", self.tmp / "new.bin"
        sig, patch = self.tmp / "f.sig", self.tmp / "f.patch"
        old_p.write_bytes(old)
        new_p.write_bytes(new)
        build_signature(str(old_p), str(sig), B)
        os.remove(old_p)
        stats = make_delta(str(sig), str(new_p), str(patch))
        self.assertEqual(stats.bytes_scanned, len(new))
        header, _ = patch_instructions(str(patch))
        # 头部仍记录旧文件身份，供 apply 阶段核对。
        self.assertEqual(header["old_size"], len(old))
        self.assertEqual(header["old_sha256"], hashlib.sha256(old).hexdigest())

    def test_add_physical_chunks_capped_at_64kib(self) -> None:
        # 完全不同的大段新数据：逻辑 ADD 必须拆成 <=64KiB 的物理块。
        old = b"q" * B
        new = bytes((i * 31 + 5) % 256 for i in range(200_000))
        stats, patch_p, *_ = self._pipeline(old, new)
        header, recs = patch_instructions(str(patch_p))
        add_recs = [r for r in recs if r["op"] == "ADD"]
        self.assertTrue(len(add_recs) >= 3)
        for r in add_recs[:-1]:
            self.assertEqual(
                len(base64.b64decode(r["data_b64"])), 64 * 1024
            )
        total = sum(len(base64.b64decode(r["data_b64"])) for r in add_recs)
        self.assertEqual(total, len(new))
        self.assertEqual(stats.add_chunks, len(add_recs))

    def test_noncanonical_base64_rejected(self) -> None:
        old = b"w" * B
        stats, patch_p, old_p, *_ = self._pipeline(old, b"w" * B + b"tail")
        header, recs = patch_instructions(str(patch_p))
        # 手工构造带非规范 base64（省略填充）的 ADD。
        good = next(r for r in recs if r["op"] == "ADD")
        raw = base64.b64decode(good["data_b64"])
        noncanon = base64.b64encode(raw).decode().rstrip("=")
        text = "\n".join(
            [
                json.dumps(header, sort_keys=True),
                json.dumps({"op": "ADD", "data_b64": noncanon}, sort_keys=True),
                json.dumps({"op": "END"}),
            ]
        )
        bad = self.tmp / "noncanon.patch"
        bad.write_text(text)
        with open(bad, "rb") as fh:
            with self.assertRaises(PatchFormatError):
                list(PatchReader(fh))

    def test_unsupported_signature_version_rejected(self) -> None:
        old_p = self.tmp / "o.bin"
        old_p.write_bytes(b"u" * (2 * B))
        sig = self.tmp / "o.sig"
        build_signature(str(old_p), str(sig), B)
        conn = sqlite3.connect(str(sig))
        conn.execute("UPDATE meta SET value = 99 WHERE key = 'version'")
        conn.commit()
        conn.close()
        new_p = self.tmp / "n.bin"
        new_p.write_bytes(b"u" * (2 * B))
        with self.assertRaises(ValueError):
            make_delta(str(sig), str(new_p), str(self.tmp / "p.patch"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
