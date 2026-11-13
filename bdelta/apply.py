"""apply / verify：按补丁流式重建目标文件。

流程：
  1. 打开补丁并校验头部/全部指令格式（PatchReader 已拒绝零长指令、
     非规范 base64、超出旧文件范围的 COPY、超大 ADD 物理块）；
  2. 先流式验证旧文件大小与 SHA256 身份（旧版本替换在此被拒绝）；
  3. 指令逐条写入与输出同目录的临时文件，COPY 直接从旧文件流式复制；
  4. 写完后核对目标长度与 SHA256，全部成功才 os.replace 原子发布；
  任何一步失败都删除临时文件，绝不会覆盖此前已成功发布的文件。
"""

from __future__ import annotations

import hashlib
import os

from . import IO_CHUNK
from .checksums import StreamingHash
from .frozen import FrozenFile
from .patch import PatchReader


class ApplyError(RuntimeError):
    """补丁无法应用。"""


def _verify_old_identity(old_path: str, exp_size: int, exp_sha256: str) -> None:
    hasher = hashlib.sha256()
    size = 0
    with FrozenFile(old_path) as ff:
        if ff.size != exp_size:
            raise ApplyError(
                f"旧文件身份不匹配：大小 {ff.size} != 补丁记录 {exp_size}"
            )
        while True:
            chunk = ff.read(IO_CHUNK)
            if not chunk:
                break
            hasher.update(chunk)
            size += len(chunk)
    if size != exp_size:
        raise ApplyError("旧文件读取字节数与大小不一致")
    if hasher.hexdigest() != exp_sha256:
        raise ApplyError("旧文件 SHA256 不匹配（旧版本/被替换的旧文件）")


def apply_patch(old_path: str, patch_path: str, out_path: str) -> dict:
    # 先完整解析补丁到临时输出，任何错误都在发布前抛出。
    tmp_out = os.path.join(
        os.path.dirname(os.path.abspath(out_path)) or ".",
        f".{os.path.basename(out_path)}.bdelta.tmp",
    )
    if os.path.exists(tmp_out):
        os.remove(tmp_out)

    published = False
    try:
        with open(patch_path, "rb") as pf:
            reader = PatchReader(pf)
            header = reader.header
            _verify_old_identity(
                old_path, header["old_size"], header["old_sha256"]
            )

            out_hash = StreamingHash()
            produced = 0
            copy_bytes = 0
            add_bytes = 0
            n_copy = 0
            n_add = 0

            old_fh = FrozenFile(old_path)
            try:
                with open(tmp_out, "wb") as out_fh:
                    for op, rec in reader:
                        if op == "ADD":
                            data = rec["data"]
                            out_fh.write(data)
                            out_hash.update(data)
                            add_bytes += len(data)
                            n_add += 1
                            produced += len(data)
                        else:
                            off = rec["old_offset"]
                            length = rec["length"]
                            if off + length > header["old_size"]:
                                raise ApplyError("越界 COPY")
                            old_fh.seek(off)
                            remaining = length
                            while remaining:
                                chunk = old_fh.read(min(IO_CHUNK, remaining))
                                if not chunk:
                                    raise ApplyError("旧文件提前结束")
                                out_fh.write(chunk)
                                out_hash.update(chunk)
                                remaining -= len(chunk)
                            copy_bytes += length
                            n_copy += 1
                            produced += length

                        if produced > header["new_size"]:
                            raise ApplyError("重建长度超过补丁声明的目标大小")
            finally:
                old_fh.close()

            if produced != header["new_size"]:
                raise ApplyError(
                    f"目标长度不匹配：重建 {produced} != 声明 {header['new_size']}"
                )
            if out_hash.sha256 != header["new_sha256"]:
                raise ApplyError("目标 SHA256 不匹配")

        # 全部验证通过后才原子发布；此前的成功文件不会被破坏。
        os.replace(tmp_out, out_path)
        published = True
        return {
            "out_path": out_path,
            "new_size": produced,
            "new_sha256": out_hash.sha256,
            "copy_bytes": copy_bytes,
            "add_bytes": add_bytes,
            "copy_instructions": n_copy,
            "add_chunks": n_add,
        }
    finally:
        # 任何阶段失败（含补丁解析阶段）都清理临时文件，绝不残留。
        if not published:
            _safe_remove(tmp_out)


def verify_patch(old_path: str, patch_path: str, expected_new_path: str) -> dict:
    """校验补丁与旧文件身份，并把重建结果与给定目标文件逐字节比较。"""
    if not os.path.exists(expected_new_path):
        raise ApplyError(f"目标文件不存在: {expected_new_path}")

    with open(patch_path, "rb") as pf:
        reader = PatchReader(pf)
        header = reader.header
    _verify_old_identity(old_path, header["old_size"], header["old_sha256"])

    exp_hasher = hashlib.sha256()
    exp_size = 0
    with FrozenFile(expected_new_path) as ff:
        if ff.size != header["new_size"]:
            raise ApplyError(
                f"目标大小不匹配：实际 {ff.size} != 补丁 {header['new_size']}"
            )
        while True:
            chunk = ff.read(IO_CHUNK)
            if not chunk:
                break
            exp_hasher.update(chunk)
            exp_size += len(chunk)
    if exp_hasher.hexdigest() != header["new_sha256"]:
        raise ApplyError("目标 SHA256 与补丁记录不一致")

    # 重建到临时文件再逐字节比对，不影响任何既有文件。
    tmp_out = expected_new_path + ".verify.tmp"
    if os.path.exists(tmp_out):
        os.remove(tmp_out)
    try:
        result = apply_patch(old_path, patch_path, tmp_out)
        with open(tmp_out, "rb") as a, open(expected_new_path, "rb") as b:
            while True:
                ca, cb = a.read(IO_CHUNK), b.read(IO_CHUNK)
                if ca != cb:
                    raise ApplyError("重建结果与目标文件逐字节不一致")
                if not ca:
                    break
        return {
            "ok": True,
            "new_size": result["new_size"],
            "new_sha256": result["new_sha256"],
        }
    finally:
        _safe_remove(tmp_out)


def _safe_remove(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
