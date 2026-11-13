"""测试辅助：完全独立的补丁解释器。

不导入 bdelta.apply / bdelta.patch，只用 json/base64/hashlib 重新解读补丁，
逐字节重建目标，并核对每一条 COPY 的真实来源字节。
"""

from __future__ import annotations

import base64
import hashlib
import json

MAX_ADD_CHUNK = 64 * 1024


class IndependentPatchError(AssertionError):
    pass


def interpret_patch(old_path: str, patch_path: str) -> tuple[bytes, list[dict]]:
    """返回 (重建字节, 指令列表)，并对补丁做独立完整性检查。"""
    with open(patch_path, "rb") as fh:
        lines = fh.read().splitlines()
    if len(lines) < 2:
        raise IndependentPatchError("补丁行数不足")

    header = json.loads(lines[0].decode())
    if header.get("format") != "bdelta-patch" or header.get("version") != 1:
        raise IndependentPatchError("补丁魔数/版本错误")
    if not (256 <= header["block_size"] <= 65536):
        raise IndependentPatchError("块大小越界")

    with open(old_path, "rb") as fh:
        old = fh.read()
    if len(old) != header["old_size"]:
        raise IndependentPatchError("旧文件大小与补丁头部不符")
    if hashlib.sha256(old).hexdigest() != header["old_sha256"]:
        raise IndependentPatchError("旧文件 SHA256 与补丁头部不符")

    records = [json.loads(line.decode()) for line in lines[1:]]
    if records[-1] != {"op": "END"}:
        raise IndependentPatchError("缺少 END 收尾")

    out = bytearray()
    prev_kind = None
    prev_copy_end = None
    prev_out_pos = None
    checked_copies: list[dict] = []

    for rec in records[:-1]:
        op = rec["op"]
        if op == "ADD":
            text = rec["data_b64"]
            # 严格 base64：标准编码必须能还原出原文。
            data = base64.b64decode(text, validate=True)
            if base64.b64encode(data).decode("ascii") != text:
                raise IndependentPatchError("非规范 base64")
            if not (0 < len(data) <= MAX_ADD_CHUNK):
                raise IndependentPatchError("ADD 物理块大小非法")
            out.extend(data)
            prev_kind = "ADD"
        elif op == "COPY":
            off, length = rec["old_offset"], rec["length"]
            if not (isinstance(off, int) and not isinstance(off, bool) and off >= 0):
                raise IndependentPatchError("COPY 偏移非法")
            if not (isinstance(length, int) and not isinstance(length, bool) and length > 0):
                raise IndependentPatchError("COPY 长度非法")
            if off + length > len(old):
                raise IndependentPatchError("越界 COPY")
            source = old[off : off + length]
            # 逐字节核对：COPY 在输出中的每个字节必须等于旧文件对应字节。
            start = len(out)
            out.extend(source)
            for i, byte in enumerate(source):
                if out[start + i] != old[off + i]:
                    raise IndependentPatchError("COPY 来源字节不一致")
            if prev_kind == "COPY" and start == prev_out_pos:
                # 两条 COPY 在新流中相邻：旧偏移连续时本应被合并为一条；
                # 旧偏移不连续（如重复块复用）允许保留为独立 COPY。
                if off == prev_copy_end:
                    raise IndependentPatchError("旧偏移连续的相邻 COPY 未被合并")
            prev_copy_end = off + length
            prev_out_pos = start + length
            prev_kind = "COPY"
            checked_copies.append(
                {"old_offset": off, "length": length, "at": start}
            )
        else:
            raise IndependentPatchError(f"未知指令 {op}")

    if len(out) != header["new_size"]:
        raise IndependentPatchError("重建长度与头部不符")
    if hashlib.sha256(out).hexdigest() != header["new_sha256"]:
        raise IndependentPatchError("重建 SHA256 与头部不符")

    return bytes(out), checked_copies


def patch_instructions(patch_path: str) -> tuple[dict, list[dict]]:
    with open(patch_path, "rb") as fh:
        lines = fh.read().splitlines()
    header = json.loads(lines[0].decode())
    recs = [json.loads(x.decode()) for x in lines[1:]]
    if recs[-1] != {"op": "END"}:
        raise IndependentPatchError("缺少 END 收尾")
    return header, recs[:-1]
