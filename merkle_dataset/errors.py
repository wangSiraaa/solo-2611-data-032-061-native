"""领域异常。CLI 捕获 MerkleError 后以非零状态退出。"""


class MerkleError(Exception):
    """所有可预期的构建、加载、证明与验证错误的基类。"""


class InputError(MerkleError):
    """输入 JSONL 非法：格式错误、字段不符、长度超限或键重复。"""


class ManifestError(MerkleError):
    """根清单缺失、格式错误或与树/证明不一致。"""


class TreeIntegrityError(MerkleError):
    """SQLite 树文件结构或摘要关系被破坏。"""


class ProofError(MerkleError):
    """证明结构矛盾、节点缺失/冗余或根重建失败。"""
