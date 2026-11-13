"""本地合成测试数据（全部文件由本模块确定性生成，不依赖外部数据）。"""

from __future__ import annotations

import os
import random

from . import IO_CHUNK

LARGE_SIZE = 70 * 1024 * 1024  # 大于 64 MiB


def write_random(path: str, size: int, seed: int) -> None:
    """用固定种子的 PRNG 流式生成伪随机文件。"""
    rng = random.Random(seed)
    remaining = size
    with open(path, "wb") as fh:
        while remaining:
            n = min(IO_CHUNK, remaining)
            fh.write(rng.randbytes(n))
            remaining -= n


def make_large_pair(dir_path: str, size: int = LARGE_SIZE) -> dict:
    """大文件对：旧文件为固定 PRNG 流，新文件仅在少量位置插入短字节串。"""
    old_path = os.path.join(dir_path, "old_large.bin")
    new_path = os.path.join(dir_path, "new_large.bin")
    rng = random.Random(20261113)

    # 插入点（绝对偏移）与插入内容，量很小且彼此相距很远。
    insertions = {
        size // 4: b"<<<INSERT-A>>>",
        size // 2: b"<<<INSERT-B-B-B>>>",
        (3 * size) // 4: b"<<<C>>>",
    }

    with open(old_path, "wb") as old_fh, open(new_path, "wb") as new_fh:
        remaining = size
        pos = 0
        while remaining:
            n = min(IO_CHUNK, remaining)
            chunk = rng.randbytes(n)
            old_fh.write(chunk)
            # 在 chunk 内部处理可能存在的插入点。
            cursor = 0
            for off in sorted(k for k in insertions if pos <= k < pos + n):
                rel = off - pos
                new_fh.write(chunk[cursor:rel])
                new_fh.write(insertions[off])
                cursor = rel
            new_fh.write(chunk[cursor:])
            pos += n
            remaining -= n

    return {
        "old_path": old_path,
        "new_path": new_path,
        "old_size": size,
        "new_size": size + sum(len(v) for v in insertions.values()),
        "inserted_bytes": sum(len(v) for v in insertions.values()),
    }


def make_corpus(dir_path: str, block_size: int = 1024) -> dict:
    """生成小测试用例集，返回 {用例名: (old_path, new_path, 说明)}。"""
    os.makedirs(dir_path, exist_ok=True)
    paths: dict[str, tuple[str, str, str]] = {}

    def _paths(name: str) -> tuple[str, str]:
        o = os.path.join(dir_path, f"{name}.old.bin")
        n = os.path.join(dir_path, f"{name}.new.bin")
        return o, n

    rng = random.Random(42)
    base = rng.randbytes(6 * block_size)

    # 1. 头部插入一个字节
    o, n = _paths("head_insert")
    with open(o, "wb") as fh:
        fh.write(base)
    with open(n, "wb") as fh:
        fh.write(b"X" + base)
    paths["head_insert"] = (o, n, "头部插入 1 字节")

    # 2. 中间插入与删除
    o, n = _paths("mid_edit")
    mid = 3 * block_size
    with open(o, "wb") as fh:
        fh.write(base)
    with open(n, "wb") as fh:
        fh.write(base[:mid])
        fh.write(b"INSERTED-MIDDLE-DATA" * 8)
        fh.write(base[mid + block_size // 2 :])  # 删除半块
    paths["mid_edit"] = (o, n, "中间插入并删除半块")

    # 3. 重复旧块（同一内容出现多次，制造并列候选）
    o, n = _paths("dup_blocks")
    pattern = bytes(range(256)) * (block_size // 256)  # 恰好 B 字节
    old = pattern + pattern + pattern + rng.randbytes(block_size)
    with open(o, "wb") as fh:
        fh.write(old)
    with open(n, "wb") as fh:
        fh.write(rng.randbytes(20) + pattern + rng.randbytes(10))
    paths["dup_blocks"] = (o, n, "重复旧块产生并列候选")

    # 4. 完全不同
    o, n = _paths("totally_different")
    with open(o, "wb") as fh:
        fh.write(rng.randbytes(4 * block_size))
    with open(n, "wb") as fh:
        fh.write(random.Random(99).randbytes(4 * block_size + 7))
    paths["totally_different"] = (o, n, "完全不同内容")

    # 5. 空文件（旧空 -> 新非空；新空 -> 旧非空；双空）
    for tag, old_data, new_data in (
        ("empty_old", b"", rng.randbytes(2 * block_size + 5)),
        ("empty_new", rng.randbytes(2 * block_size), b""),
        ("empty_both", b"", b""),
    ):
        o, n = _paths(tag)
        with open(o, "wb") as fh:
            fh.write(old_data)
        with open(n, "wb") as fh:
            fh.write(new_data)
        paths[tag] = (o, n, f"空文件场景 {tag}")

    # 6. B 边界与尾块不足
    for tag, old_len, new_len in (
        ("exact_block", block_size, block_size),
        ("one_short", block_size - 1, block_size - 1),
        ("one_over", block_size + 1, block_size + 1),
        ("two_blocks_plus_tail", 2 * block_size + 37, 2 * block_size + 37),
        ("tail_to_full", 2 * block_size + 10, 3 * block_size),
    ):
        o, n = _paths(tag)
        data = rng.randbytes(max(old_len, new_len))
        with open(o, "wb") as fh:
            fh.write(data[:old_len])
        with open(n, "wb") as fh:
            fh.write(data[:new_len])
        paths[tag] = (o, n, f"B 边界/尾块 {tag}")

    return {"paths": paths, "block_size": block_size}
