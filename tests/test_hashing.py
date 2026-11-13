"""哈希规则的已知答案向量与边界计算。"""

import hashlib
import unittest

from merkle_dataset.hashing import (
    EMPTY_ROOT,
    PAD_LEAF,
    internal_digest,
    leaf_digest,
    next_power_of_two,
    tree_height,
    u32be,
)


class HashingVectorTest(unittest.TestCase):
    def test_leaf_digest_matches_manual_construction(self):
        key = "苹果".encode("utf-8")
        value = "汁".encode("utf-8")
        expected = hashlib.sha256(
            b"\x00"
            + u32be(len(key)) + key
            + u32be(len(value)) + value
        ).digest()
        self.assertEqual(leaf_digest(key, value), expected)

    def test_leaf_digest_ascii_vector(self):
        # 固定向量：任何实现都必须复算一致。
        digest = leaf_digest(b"k", b"v")
        self.assertEqual(
            digest.hex(),
            hashlib.sha256(b"\x00\x00\x00\x00\x01k\x00\x00\x00\x01v").hexdigest(),
        )

    def test_internal_digest_vector(self):
        left = bytes([1] * 32)
        right = bytes([2] * 32)
        self.assertEqual(
            internal_digest(left, right),
            hashlib.sha256(b"\x01" + left + right).digest(),
        )
        # 左右次序敏感：交换后摘要不同。
        self.assertNotEqual(internal_digest(left, right), internal_digest(right, left))

    def test_fixed_constants(self):
        self.assertEqual(EMPTY_ROOT, hashlib.sha256(b"\x02").digest())
        self.assertEqual(PAD_LEAF, hashlib.sha256(b"\x03").digest())
        self.assertNotEqual(EMPTY_ROOT, PAD_LEAF)

    def test_power_of_two_and_height(self):
        self.assertEqual(next_power_of_two(1), 1)
        self.assertEqual(next_power_of_two(2), 2)
        self.assertEqual(next_power_of_two(5), 8)
        self.assertEqual(next_power_of_two(8), 8)
        self.assertEqual(next_power_of_two(9), 16)
        self.assertEqual(tree_height(0), 0)
        self.assertEqual(tree_height(1), 0)
        self.assertEqual(tree_height(2), 1)
        self.assertEqual(tree_height(5), 3)
        self.assertEqual(tree_height(8), 3)
        self.assertEqual(tree_height(9), 4)

    def test_u32be_rejects_out_of_range(self):
        with self.assertRaises(ValueError):
            u32be(-1)
        with self.assertRaises(ValueError):
            u32be(2**32)


if __name__ == "__main__":
    unittest.main()
