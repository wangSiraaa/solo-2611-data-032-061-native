"""可信根清单。

清单由验证者单独指定，属于已知可信输入：证明文件里不携带根摘要，
也不允许用证明替换清单。清单本身没有签名机制，其信任来自带外分发。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from . import FORMAT_VERSION
from .errors import ManifestError
from .hashing import DIGEST_SIZE, EMPTY_ROOT

MANIFEST_TYPE = "ordered-merkle-manifest"


@dataclass(frozen=True)
class Manifest:
    version: int
    record_count: int
    root: bytes  # 32 字节根摘要

    def to_dict(self) -> dict:
        return {
            "type": MANIFEST_TYPE,
            "format_version": self.version,
            "record_count": self.record_count,
            "root_hex": self.root.hex(),
        }


def build_manifest(record_count: int, root: bytes) -> Manifest:
    if len(root) != DIGEST_SIZE:
        raise ManifestError("根摘要必须为 32 字节")
    if record_count < 0:
        raise ManifestError("记录数不能为负")
    if record_count == 0 and root != EMPTY_ROOT:
        raise ManifestError("空数据集的根必须为 SHA256(0x02)")
    return Manifest(version=FORMAT_VERSION, record_count=record_count, root=root)


def write_manifest(manifest: Manifest, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest.to_dict(), f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def read_manifest(path: str) -> Manifest:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError as exc:
        raise ManifestError(f"根清单不存在: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ManifestError(f"根清单不是合法 JSON: {exc}") from exc

    if not isinstance(data, dict) or data.get("type") != MANIFEST_TYPE:
        raise ManifestError("根清单类型标识缺失或不符")
    version = data.get("format_version")
    if version != FORMAT_VERSION:
        raise ManifestError(f"不支持的清单格式版本: {version!r}")
    count = data.get("record_count")
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        raise ManifestError("清单记录数非法")
    root_hex = data.get("root_hex")
    if not isinstance(root_hex, str):
        raise ManifestError("清单根摘要缺失或类型错误")
    try:
        root = bytes.fromhex(root_hex)
    except ValueError as exc:
        raise ManifestError("根摘要不是合法十六进制") from exc
    if len(root) != DIGEST_SIZE:
        raise ManifestError("根摘要长度必须为 32 字节")
    if count == 0 and root != EMPTY_ROOT:
        raise ManifestError("空数据集清单的根必须为 SHA256(0x02)")
    return Manifest(version=version, record_count=count, root=root)
