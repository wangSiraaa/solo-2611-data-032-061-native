"""delta 生成：只读签名与新文件，不读取旧文件正文。

确定性贪心规则（题面固定，不追求全局最小补丁）：

  从新文件当前位置 pos 开始，维护长度为 B 的滑动窗口；
  窗口每移动一个字节，用 O(1) 滚动公式更新 Adler32（见 checksums.py）；
  弱校验在签名 SQLite 索引中命中后，必须再用 zlib.adler32 独立复核弱值、
  用 SHA256 复核强摘要，二者都通过才允许复用，不能只凭弱值相等复用；
  同一窗口有多个候选块时，选择旧偏移最小者（候选按 offset 升序返回）；
  命中 -> 输出 COPY 并前进 B 字节；未命中 -> 当前字节并入 ADD 并前进 1 字节；
  末尾不足 B 的字节全部作为 ADD。

内存：新文件按 IO_CHUNK 流式读入，缓冲内只保留未消费数据（达到上限时
整体压缩一次），不把新文件整体装入内存；旧文件正文完全不参与本阶段。
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass

from . import IO_CHUNK
from .checksums import RollingAdler, adler32
from .frozen import FrozenFile
from .patch import PatchWriter
from .signature import open_signature, read_meta, validate_block_size


@dataclass
class DeltaStats:
    new_size: int
    new_sha256: str
    block_size: int
    bytes_scanned: int          # 实际处理的新文件字节数
    strong_checks: int          # 候选强校验（SHA256）次数
    weak_hits: int              # 弱校验在签名索引中的命中次数
    copy_bytes: int             # 复用字节数
    add_bytes: int              # ADD 字节数
    copy_instructions: int      # 落盘 COPY 指令数（合并后）
    add_chunks: int             # 落盘物理 ADD 块数


class StreamWindow:
    """新文件上的定长滑动窗口。

    使用 ``bytearray + 消费偏移`` 而不是 ``del buf[0]``：消费数据只移动
    偏移，已消费前缀累积到一个 IO 块大小时一次性压缩，避免 O(n²) 搬移。
    缓冲上界约为 2 * IO_CHUNK + B。
    """

    def __init__(self, ff: FrozenFile, block_size: int) -> None:
        self._ff = ff
        self._b = block_size
        self._buf = bytearray()
        self._start = 0          # 窗口起点在 _buf 中的下标
        self._eof = False

    def _fill(self) -> None:
        if self._eof:
            return
        chunk = self._ff.read(IO_CHUNK)
        if not chunk:
            self._eof = True
            return
        self._buf.extend(chunk)
        if self._start >= IO_CHUNK:
            del self._buf[: self._start]
            self._start = 0

    def ensure_window(self) -> bool:
        """保证存在完整 B 字节窗口；EOF 前不足 B 时返回 False。"""
        while len(self._buf) - self._start < self._b:
            if self._eof:
                return False
            self._fill()
        return True

    def window(self) -> bytes:
        s, b = self._start, self._b
        return bytes(self._buf[s : s + b])

    def first(self) -> int:
        return self._buf[self._start]

    def last(self) -> int:
        return self._buf[self._start + self._b - 1]

    def advance_one(self) -> None:
        self._start += 1

    def consume_window(self) -> bytes:
        s, b = self._start, self._b
        w = bytes(self._buf[s : s + b])
        self._start += b
        return w

    def tail(self) -> bytes:
        """EOF 后剩余的不足 B 字节。"""
        return bytes(self._buf[self._start :])


def _hash_new_file(new_path: str) -> tuple[int, str]:
    """第一遍流式扫描：仅算新文件大小与整体 SHA256（同时做冻结检查）。"""
    hasher = hashlib.sha256()
    size = 0
    with FrozenFile(new_path) as ff:
        declared = ff.size
        while True:
            chunk = ff.read(IO_CHUNK)
            if not chunk:
                break
            hasher.update(chunk)
            size += len(chunk)
        if size != declared:
            raise RuntimeError("内部错误：新文件读取字节数与大小不一致")
    return size, hasher.hexdigest()


def make_delta(sig_path: str, new_path: str, patch_path: str) -> DeltaStats:
    new_size, new_sha256 = _hash_new_file(new_path)

    conn = open_signature(sig_path)
    tmp_patch = patch_path + ".tmp"
    if os.path.exists(tmp_patch):
        os.remove(tmp_patch)

    strong_checks = 0
    weak_hits = 0
    bytes_scanned = 0
    copy_bytes = 0
    add_bytes = 0

    try:
        meta = read_meta(conn)
        block_size = validate_block_size(meta["block_size"])

        with FrozenFile(new_path) as ff, open(tmp_patch, "wb") as patch_fh:
            if ff.size != new_size:
                raise RuntimeError("内部错误：两遍扫描间新文件大小不一致")
            sw = StreamWindow(ff, block_size)
            writer = PatchWriter(
                patch_fh,
                {
                    "version": 1,
                    "block_size": block_size,
                    "old_size": meta["old_size"],
                    "old_sha256": meta["old_sha256"],
                    "new_size": new_size,
                    "new_sha256": new_sha256,
                },
            )

            # 首个窗口全量计算；之后每移动一个字节用 O(1) roll 维护。
            if not sw.ensure_window():
                roller = None
            else:
                roller = RollingAdler(sw.window())

            while roller is not None:
                window = sw.window()
                weak = roller.value

                rows = conn.execute(
                    "SELECT offset, length, sha256 FROM blocks "
                    "WHERE adler32 = ? ORDER BY offset ASC",
                    (weak,),
                ).fetchall()

                matched_offset: int | None = None
                if rows:
                    weak_hits += 1
                    # 弱值独立复核：滚动实现必须与 zlib 的定义一致。
                    if adler32(window) == weak:
                        window_digest = hashlib.sha256(window).hexdigest()
                        for cand_off, cand_len, cand_sha in rows:
                            # 强校验：弱值相等绝不直接复用。
                            strong_checks += 1
                            if cand_len == block_size and cand_sha == window_digest:
                                matched_offset = cand_off  # rows 已按 offset 升序
                                break

                if matched_offset is not None:
                    writer.copy(matched_offset, block_size, bytes_scanned)
                    copy_bytes += block_size
                    bytes_scanned += block_size
                    sw.consume_window()
                    if sw.ensure_window():
                        # 前进 B 步后的新窗口：全量重算基准。逐字节移动
                        # 仍全部走 roll()（见未命中分支），此处每命中一次
                        # 才重建一次基准，单字节移动从不重算整窗。
                        roller = RollingAdler(sw.window())
                    else:
                        roller = None
                else:
                    outgoing = sw.first()
                    sw.advance_one()
                    writer.add_byte(outgoing, bytes_scanned)
                    add_bytes += 1
                    bytes_scanned += 1
                    if sw.ensure_window():
                        # O(1) 滚动：移出首字节、移入新窗口末字节。
                        roller.roll(outgoing, sw.last())
                    else:
                        roller = None

            # 末尾不足 B 的字节全部作为 ADD。
            tail = sw.tail()
            if tail:
                writer.add_data(tail, bytes_scanned)
                add_bytes += len(tail)
                bytes_scanned += len(tail)
            writer.close()

        if bytes_scanned != new_size:
            raise RuntimeError(
                f"内部错误：扫描字节数 {bytes_scanned} != 新文件大小 {new_size}"
            )
    finally:
        conn.close()

    os.replace(tmp_patch, patch_path)

    return DeltaStats(
        new_size=new_size,
        new_sha256=new_sha256,
        block_size=block_size,
        bytes_scanned=bytes_scanned,
        strong_checks=strong_checks,
        weak_hits=weak_hits,
        copy_bytes=copy_bytes,
        add_bytes=add_bytes,
        copy_instructions=writer.emitted_copy,
        add_chunks=writer.emitted_add_chunks,
    )
