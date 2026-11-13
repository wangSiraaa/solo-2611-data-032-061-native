"""精确十进制 -> Fraction 的转换。

题目要求选择率是零到一之间的“精确十进制”。因此只接受普通十进制字符串
（如 "0.1"、"1"、"0.00001"），不接受科学计数法或二进制浮点字面量，
从源头避免 0.1 之类二进制近似进入分数运算。
"""

import re
from fractions import Fraction

from .errors import InputValidationError

# 可选符号（虽然选择率不允许负数，符号由范围检查负责），整数部分或小数部分至少有一侧带数字
_DECIMAL_RE = re.compile(r"^[+-]?(\d+\.\d*|\.\d+|\d+)$")


def decimal_to_fraction(value, *, field: str = "selectivity") -> Fraction:
    """把精确十进制字符串无损转换为 Fraction。

    数字（JSON number）也被拒绝：JSON 数字在 Python 端已经是二进制浮点，
    无法保证“精确十进制”，必须以字符串给出。
    """
    if not isinstance(value, str):
        raise InputValidationError(
            f"{field} 必须是精确十进制字符串（例如 '0.1'），收到 {type(value).__name__}: {value!r}"
        )
    text = value.strip()
    if not _DECIMAL_RE.match(text):
        raise InputValidationError(f"{field} 不是合法的精确十进制: {value!r}")

    sign = -1 if text.startswith("-") else 1
    body = text.lstrip("+-")
    if "." in body:
        int_part, frac_part = body.split(".", 1)
    else:
        int_part, frac_part = body, ""
    numerator = sign * (int(int_part or "0") * 10 ** len(frac_part) + int(frac_part or "0"))
    denominator = 10 ** len(frac_part)
    return Fraction(numerator, denominator)
