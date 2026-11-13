"""流式匹配引擎：一次扫描产出全局有序命中，内存有界。

输出排序键为 (规范化起点, 规范化终点, pattern_id 码点序)。
Aho-Corasick 的命中按“终点”到达，起点 = 终点 - 词长，因此起点
最多落后当前位置 max_len（最长关键词，<= 4000）。按起点分桶缓存，
任何起点 <= pos - max_len 的桶都不可能再收到新命中，可以安全刷盘——
缓冲的命中数与来源缓冲都只与 max_len 有关，与文件大小无关。

字节区间：命中取第一个匹配输出码点的来源起点、最后一个输出码点
的来源终点。规范化位置单调推进，其来源区间同样单调，因此首尾即
覆盖构成命中的全部原始码点；夹在中间被删除的重音、被折叠的整段
空白都自然落入这个连续区间，而命中前后的删除字符不会扩张区间。
"""

from __future__ import annotations

import json
import os
import tempfile

from .automaton import AhoCorasick
from .codec import iter_codepoints
from .normalize import IncrementalNormalizer


class _SourceRing:
    """最近 max_len+1 个规范化位置的原始字节来源（定长环形数组）。

    封口刷盘时最老位置为 pos-max_len，必须仍在缓冲内，故容量比
    最长关键词多 1；槽位按规范化位置取模复用，内存与文件大小无关。
    """

    __slots__ = ("_starts", "_ends", "_cap")

    def __init__(self, capacity: int) -> None:
        self._cap = capacity
        self._starts = [0] * capacity
        self._ends = [0] * capacity

    def add(self, pos: int, byte_start: int, byte_end: int) -> None:
        slot = pos % self._cap
        self._starts[slot] = byte_start
        self._ends[slot] = byte_end

    def range_of(self, start_pos: int, end_pos: int) -> tuple[int, int]:
        """返回规范化区间 [start_pos, end_pos) 对应的原字节 [start, end)。"""
        return (
            self._starts[start_pos % self._cap],
            self._ends[(end_pos - 1) % self._cap],
        )


def stream_match(
    fileobj,
    automaton: AhoCorasick,
    output_path: str,
    chunk_size: int,
) -> int:
    """执行流式匹配，原子发布 JSON Lines 结果，返回命中条数。

    每行字段：pattern_id, norm_start, norm_end, byte_start, byte_end。
    任何异常都会清理临时文件，不会留下半成品输出。
    """
    max_len = automaton.max_len
    sources = _SourceRing(max_len + 1)
    # 起点桶：norm_start -> [(norm_end, pattern_id), ...]
    buckets: dict[int, list[tuple[int, str]]] = {}
    flushed_to = -1  # 已刷盘到哪个起点（含）
    state = {"pos": -1, "node": 0}

    directory = os.path.dirname(os.path.abspath(output_path))
    tmp_fd, tmp_name = tempfile.mkstemp(prefix=".normmatch.", suffix=".tmp", dir=directory)
    hit_count = 0
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8", newline="\n") as out:

            def write_bucket(start_pos: int) -> None:
                nonlocal hit_count
                entries = buckets.pop(start_pos, None)
                if not entries:
                    return
                # 同起点内按 (终点, pattern_id 码点序) 排序
                entries.sort()
                for end_pos, pattern_id in entries:
                    byte_start, byte_end = sources.range_of(start_pos, end_pos)
                    rec = {
                        "pattern_id": pattern_id,
                        "norm_start": start_pos,
                        "norm_end": end_pos,
                        "byte_start": byte_start,
                        "byte_end": byte_end,
                    }
                    out.write(json.dumps(rec, ensure_ascii=False))
                    out.write("\n")
                    hit_count += 1

            def on_norm_char(ch: str, byte_start: int, byte_end: int) -> None:
                nonlocal flushed_to
                pos = state["pos"] + 1
                state["pos"] = pos
                node = automaton.step(state["node"], ch)
                state["node"] = node
                sources.add(pos, byte_start, byte_end)

                for pattern_id in automaton.outputs(node):
                    end_pos = pos + 1
                    start_pos = end_pos - automaton.pattern_length[pattern_id]
                    buckets.setdefault(start_pos, []).append((end_pos, pattern_id))

                # 新命中的最小起点是 (pos+1)-max_len，故 <= pos-max_len
                # 的起点桶已经封口，按起点顺序刷盘
                seal = pos - max_len
                while flushed_to < seal:
                    flushed_to += 1
                    write_bucket(flushed_to)

            normalizer = IncrementalNormalizer(on_norm_char)
            for ch, byte_start, byte_end in iter_codepoints(fileobj, chunk_size):
                normalizer.feed(ch, byte_start, byte_end)
            normalizer.finish()

            # 输入结束：按起点顺序刷掉剩余桶（含只有部分位置无命中的空洞）
            pos = state["pos"]
            for start_pos in range(flushed_to + 1, pos + 1):
                write_bucket(start_pos)

        os.replace(tmp_name, output_path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise
    return hit_count
