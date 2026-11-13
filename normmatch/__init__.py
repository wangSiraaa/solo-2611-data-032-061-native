"""normmatch —— 大型 UTF-8 文本的规范化关键词流式匹配工具（纯标准库）。

子命令：
    generate  生成测试语料与关键词配置
    match     流式匹配，结果 JSON Lines 原子落盘
    verify    全量正确性、分块一致性、证据核查与峰值 RSS 测量
    rules     打印规范化与定位规则说明
"""

__all__ = ["__version__"]
__version__ = "1.0.0"
