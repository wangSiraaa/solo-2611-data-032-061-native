"""补丁文件格式（JSON Lines, UTF-8）。

第 1 行是头部：
    {"format":"bdelta-patch","version":1,"block_size":B,
     "old_size":..,"old_sha256":"..","new_size":..,"new_sha256":".."}

随后是指令序列，每条一行：
    {"op":"COPY","old_offset":int,"length":int}   # 从旧文件复制
    {"op":"ADD","data_b64":"严格base64"}          # 逻辑 ADD 的一个物理数据块
最后一行必须是 {"op":"END"}。

规则：
  * 一条逻辑 ADD 可拆成多个物理块，每个物理块解码后不超过 64 KiB；
  * COPY 的偏移/长度为非负整数且整体落在旧文件内，零长指令拒绝；
  * 相邻 ADD 必须合并；相邻 COPY 仅当它们在新流中位置相邻、且引用的
    旧偏移也连续时才合并（重复块复用导致旧偏移不连续时保留独立 COPY），
    因此补丁序列完全确定。
"""

from __future__ import annotations

import base64
import binascii
import json
from typing import BinaryIO, Iterator

from . import MAX_ADD_CHUNK, PATCH_VERSION


class PatchFormatError(ValueError):
    """补丁格式不合法。"""


def _strict_b64_decode(text: str) -> bytes:
    if not isinstance(text, str):
        raise PatchFormatError("ADD 数据必须是 base64 字符串")
    try:
        raw = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise PatchFormatError(f"非法 base64: {exc}") from exc
    # 拒绝非规范编码（如可省略的填充写法、混入另类字符）。
    if base64.b64encode(raw).decode("ascii") != text:
        raise PatchFormatError("非规范 base64 编码")
    return raw


def _strict_b64_encode(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


class PatchWriter:
    """增量写出补丁，自动合并相邻 ADD/连续 COPY，控制物理块大小。

    最后一条逻辑指令挂起在内存中：新指令到达时先尝试与挂起指令合并，
    无法合并才落盘，因此不需要回退/截断文件，全程只保留 O(1) 条挂起状态。
    """

    def __init__(self, fh: BinaryIO, header: dict) -> None:
        if header["version"] != PATCH_VERSION:
            raise PatchFormatError("不支持的补丁版本")
        head = {
            "format": "bdelta-patch",
            "version": PATCH_VERSION,
            "block_size": header["block_size"],
            "old_size": header["old_size"],
            "old_sha256": header["old_sha256"],
            "new_size": header["new_size"],
            "new_sha256": header["new_sha256"],
        }
        fh.write((json.dumps(head, ensure_ascii=False, sort_keys=True) + "\n").encode())
        self._fh = fh
        self._add_buf = bytearray()        # 挂起的逻辑 ADD（未写出部分）
        self._pending_copy: tuple[int, int] | None = None  # (offset, length)
        self._stream_pos = 0               # 已挂起/写出指令覆盖的新流长度
        self.emitted_add_chunks = 0        # 已落盘的物理 ADD 块数
        self.emitted_copy = 0              # 已落盘的 COPY 指令数

    def add_byte(self, value: int, new_position: int) -> None:
        """并入单个 ADD 字节，避免每字节构造一次 bytes 对象。"""
        if new_position != self._stream_pos:
            raise PatchFormatError("ADD 新流位置不连续，补丁序列不确定")
        if self._pending_copy is not None:
            self._emit_copy(*self._pending_copy)
            self._pending_copy = None
        self._add_buf.append(value)
        self._stream_pos = new_position + 1
        if len(self._add_buf) >= MAX_ADD_CHUNK:
            self._emit_add(bytes(self._add_buf[:MAX_ADD_CHUNK]))
            del self._add_buf[:MAX_ADD_CHUNK]

    def add_data(self, data: bytes, new_position: int) -> None:
        """把字节并入逻辑 ADD。``new_position`` 为本批字节在新流中的起点。"""
        if not data:
            return
        if new_position != self._stream_pos:
            raise PatchFormatError("ADD 新流位置不连续，补丁序列不确定")
        if self._pending_copy is not None:
            # COPY -> ADD：相邻指令类型变化，挂起 COPY 落盘。
            self._emit_copy(*self._pending_copy)
            self._pending_copy = None
        self._add_buf.extend(data)
        self._stream_pos = new_position + len(data)
        while len(self._add_buf) >= MAX_ADD_CHUNK:
            self._emit_add(bytes(self._add_buf[:MAX_ADD_CHUNK]))
            del self._add_buf[:MAX_ADD_CHUNK]

    def copy(self, old_offset: int, length: int, new_position: int) -> None:
        """输出一条 COPY。

        ``new_position`` 是该 COPY 在新文件流中的起始位置；只有与上一条
        COPY 在新流中相邻（位置连续）且旧偏移也连续时才合并。
        """
        if length <= 0:
            raise PatchFormatError("拒绝零长 COPY")
        if old_offset < 0:
            raise PatchFormatError("COPY 偏移不能为负")
        if new_position != self._stream_pos:
            raise PatchFormatError("COPY 新流位置不连续，补丁序列不确定")
        if self._add_buf:
            # ADD -> COPY：先冲刷挂起 ADD。
            self._emit_add(bytes(self._add_buf))
            self._add_buf.clear()
        # 流位置只取决于本条 COPY 在新流中的起点与原始长度，
        # 必须在 length 被合并改写之前记录。
        self._stream_pos = new_position + length
        if self._pending_copy is not None:
            prev_off, prev_len = self._pending_copy
            # 新流必然相邻（位置已校验）；仅当旧偏移也连续时合并。
            if prev_off + prev_len == old_offset:
                old_offset = prev_off
                length = prev_len + length
            else:
                self._emit_copy(prev_off, prev_len)
        self._pending_copy = (old_offset, length)

    def _emit_add(self, chunk: bytes) -> None:
        if not chunk:
            raise PatchFormatError("拒绝零长 ADD")
        if len(chunk) > MAX_ADD_CHUNK:
            raise PatchFormatError("ADD 物理块超过 64KiB")
        self._write_line({"op": "ADD", "data_b64": _strict_b64_encode(chunk)})
        self.emitted_add_chunks += 1

    def _emit_copy(self, old_offset: int, length: int) -> None:
        if length <= 0:
            raise PatchFormatError("拒绝零长 COPY")
        self._write_line(
            {"op": "COPY", "old_offset": old_offset, "length": length}
        )
        self.emitted_copy += 1

    def _write_line(self, obj: dict) -> None:
        self._fh.write(
            (json.dumps(obj, ensure_ascii=False, sort_keys=True) + "\n").encode()
        )

    def close(self) -> None:
        if self._add_buf:
            self._emit_add(bytes(self._add_buf))
            self._add_buf.clear()
        if self._pending_copy is not None:
            self._emit_copy(*self._pending_copy)
            self._pending_copy = None
        self._write_line({"op": "END"})


class PatchReader(Iterator):
    """逐条读回并校验补丁指令。

    校验：头部完整性、严格 base64、物理块 64KiB 上限、非负整数、
    COPY 必须落在旧文件内、零长指令拒绝、END 必须收尾。
    """

    def __init__(self, fh: BinaryIO, expected_old_size: int | None = None) -> None:
        self._fh = fh
        self._line_no = 0
        header_line = fh.readline()
        self._line_no = 1
        if not header_line:
            raise PatchFormatError("空补丁")
        try:
            header = json.loads(header_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PatchFormatError(f"头部不是合法 JSON: {exc}") from exc
        if not isinstance(header, dict) or header.get("format") != "bdelta-patch":
            raise PatchFormatError("补丁魔数缺失或错误")
        required = {
            "version",
            "block_size",
            "old_size",
            "old_sha256",
            "new_size",
            "new_sha256",
        }
        if not required.issubset(header):
            raise PatchFormatError("补丁头部字段不完整")
        if header["version"] != PATCH_VERSION:
            raise PatchFormatError(
                f"不支持的补丁版本: {header['version']}"
            )
        for key in ("block_size", "old_size", "new_size"):
            if not isinstance(header[key], int) or isinstance(header[key], bool):
                raise PatchFormatError(f"头部 {key} 必须是整数")
            if header[key] < 0:
                raise PatchFormatError(f"头部 {key} 不能为负")
        for key in ("old_sha256", "new_sha256"):
            if not isinstance(header[key], str) or len(header[key]) != 64:
                raise PatchFormatError(f"头部 {key} 必须是 64 位十六进制摘要")
        self.header = header
        self._old_size = (
            expected_old_size
            if expected_old_size is not None
            else header["old_size"]
        )
        self._ended = False

    def __iter__(self) -> "PatchReader":
        return self

    def __next__(self) -> tuple[str, dict]:
        if self._ended:
            raise StopIteration
        line = self._fh.readline()
        self._line_no += 1
        if not line:
            raise PatchFormatError("补丁缺少 END 收尾")
        try:
            rec = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PatchFormatError(f"第 {self._line_no} 行不是合法 JSON: {exc}") from exc
        if not isinstance(rec, dict) or "op" not in rec:
            raise PatchFormatError(f"第 {self._line_no} 行指令格式错误")
        op = rec["op"]
        if op == "END":
            extra = self._fh.read(1)
            if extra:
                raise PatchFormatError("END 之后仍有内容")
            self._ended = True
            raise StopIteration
        if op == "ADD":
            data = _strict_b64_decode(rec.get("data_b64", ""))
            if len(data) == 0:
                raise PatchFormatError("拒绝零长 ADD")
            if len(data) > MAX_ADD_CHUNK:
                raise PatchFormatError("ADD 物理块超过 64KiB")
            return "ADD", {"data": data}
        if op == "COPY":
            off = rec.get("old_offset")
            length = rec.get("length")
            if not isinstance(off, int) or isinstance(off, bool) or off < 0:
                raise PatchFormatError("COPY 偏移必须是非负整数")
            if (
                not isinstance(length, int)
                or isinstance(length, bool)
                or length <= 0
            ):
                raise PatchFormatError("COPY 长度必须是正整数")
            if off + length > self._old_size:
                raise PatchFormatError(
                    f"越界 COPY: offset={off} length={length} old_size={self._old_size}"
                )
            return "COPY", {"old_offset": off, "length": length}
        raise PatchFormatError(f"未知指令: {op}")
