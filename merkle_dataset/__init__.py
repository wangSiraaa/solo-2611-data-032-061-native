"""本地有序 Merkle 数据集（仅使用 Python 标准库）。

模块划分：
- errors:   领域异常
- hashing:  定长编码与 SHA256 叶/内部节点/补齐/空根摘要
- manifest: 可信根清单读写
- storage:  基于 SQLite 外排的可随机访问树
- proving:  存在/缺失证明生成（节点去重共享）
- verifier: 不访问数据的独立验证器
- cli:      命令行入口（generate/build/prove/verify/inspect）
"""

FORMAT_VERSION = 1
