"""Aho-Corasick 多模式匹配自动机（规范化字符串上的字符级自动机）。

* 一次扫描即可报告全部模式的全部重叠命中；
* 不同 pattern_id 规范化为同一词时，节点上挂多个 id，分别报告；
* 失败链接在匹配时沿链输出（等价 KMP 的摊还 O(n) 次回退），
  不预计算输出闭包，避免“所有模式互为后缀”时的二次方内存；
* 自动机大小受关键词规范化总长度（<= 10 万码点）约束，是有限状态结构。
"""

from __future__ import annotations

from collections import deque


class AhoCorasick:
    __slots__ = ("next", "fail", "out", "max_len", "pattern_length")

    def __init__(self, patterns: list[tuple[str, str]]) -> None:
        """patterns: [(pattern_id, 规范化后的非空关键词), ...]"""
        # 节点 0 为根；每个节点一张转移表、一条失败链接、一组结束模式 id
        self.next: list[dict[str, int]] = [{}]
        self.fail: list[int] = [0]
        self.out: list[list[str]] = [[]]
        self.max_len: int = 0
        # pattern_id -> 规范化词长（同一 id 只允许配置一次）
        self.pattern_length: dict[str, int] = {}

        for pattern_id, word in patterns:
            if pattern_id in self.pattern_length:
                raise ValueError(f"pattern_id 重复：{pattern_id!r}")
            self.pattern_length[pattern_id] = len(word)
            node = 0
            for ch in word:
                nxt = self.next[node].get(ch)
                if nxt is None:
                    nxt = len(self.next)
                    self.next[node][ch] = nxt
                    self.next.append({})
                    self.fail.append(0)
                    self.out.append([])
                node = nxt
            self.out[node].append(pattern_id)
            if len(word) > self.max_len:
                self.max_len = len(word)

        self._build_failure_links()

    def _build_failure_links(self) -> None:
        queue: deque[int] = deque()
        # 根的直接子节点失败链接指向根
        for child in self.next[0].values():
            queue.append(child)
        while queue:
            node = queue.popleft()
            for ch, child in self.next[node].items():
                f = self.fail[node]
                while f != 0 and ch not in self.next[f]:
                    f = self.fail[f]
                self.fail[child] = self.next[f].get(ch, 0)
                # 极端情况下指向自身（根的同名边），修正为根
                if self.fail[child] == child:
                    self.fail[child] = 0
                queue.append(child)

    def step(self, node: int, ch: str) -> int:
        """字符转移（含失败回退），返回新状态。"""
        next_table = self.next
        fail = self.fail
        while node != 0 and ch not in next_table[node]:
            node = fail[node]
        return next_table[node].get(ch, 0)

    def outputs(self, node: int):
        """当前状态上的全部命中模式 id（沿失败链，含节点自身）。"""
        out = self.out
        fail = self.fail
        while node != 0:
            if out[node]:
                yield from out[node]
            node = fail[node]
