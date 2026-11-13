"""电子表格错误值。

错误值是工作簿里的一类“值”（与数字、文本平级），不是 Python 异常：
错误引用会沿依赖链传播，但独立单元格不受影响。错误码尽量沿用 Excel
约定，另加两个显式前缀，便于在 explain 中与真实电子表格错误区分。
"""

from __future__ import annotations

# Excel 原生错误码（拼写保持 Excel 形态）
DIV0 = "#DIV/0!"        # 除零
VALUE = "#VALUE!"       # 类型不匹配，如文本直接参与算术
REF = "#REF!"           # 无效引用（表/区域缺失、越界）
NAME = "#NAME?"         # 无法识别的名称或函数
NA = "#N/A"             # 值不可用
NUM = "#NUM!"           # 数值越界或非法数值运算
NUL = "#NULL!"          # 交集为空

# 本工具显式新增的两类，必须带可读原因，禁止用旧缓存冒充
CIRCULAR = "#CIRC!"     # 跨表/表内循环引用
UNSUPPORTED = "#UNSUPPORTED!"  # 命名区域、外部链接、数组公式、其他函数等

# 全部合法错误码集合
ERROR_CODES = frozenset(
    {DIV0, VALUE, REF, NAME, NA, NUM, NUL, CIRCULAR, UNSUPPORTED}
)


class FormulaError(Exception):
    """公式求值过程中抛出的内部信号，最终会落盘为错误值。

    reason 为人类可读的具体原因（中文），用于 explain 定位失败来源。
    """

    def __init__(self, code: str, reason: str):
        if code not in ERROR_CODES:
            raise ValueError(f"未知错误码: {code!r}")
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason
