"""一次性工作簿计算工具（受限 OOXML 公式子集）。

模块划分：
- errors:      电子表格错误值与不支持特性的显式标记
- values:      空白 / 空字符串 / 数字 / 文本 / 布尔 / 错误 六类值模型
- lexer:       公式词法分析（不使用 Python eval/compile）
- parser:      递归下降语法分析 + 共享公式相对位移展开
- ooxml:       直接读取 ZIP/XML，解析共享公式主格与跟随格
- engine:      依赖求值、惰性分支、循环定位
- writer:      只更新 OOXML 值缓存与类型，保留公式与布局
- cli:         calculate / explain / verify 三个命令
"""

__all__ = ["errors", "values", "lexer", "parser", "ooxml", "engine", "writer", "cli"]
