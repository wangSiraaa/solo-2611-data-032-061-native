"""bdelta — 纯 Python 3.11 标准库离线差量工具。

子命令：
    generate   本地合成测试数据
    signature  扫描旧文件，生成含 Adler32/SHA256 的块签名（SQLite 落盘）
    delta      仅依据签名扫描新文件，生成 COPY/ADD 补丁（不读取旧文件正文）
    apply      验证旧文件身份后流式重建，原子发布
    verify     独立校验补丁、旧文件与重建结果

补丁只在确定性贪心规则下复用完整块，不保证全局最小。
"""

PATCH_VERSION = 1
SIGNATURE_VERSION = 1

MIN_BLOCK_SIZE = 256
MAX_BLOCK_SIZE = 65536
DEFAULT_BLOCK_SIZE = 4096

# 一条逻辑 ADD 允许的最大物理数据块（64 KiB），便于流式处理。
MAX_ADD_CHUNK = 64 * 1024

# 签名与 delta 的流式读取块。
IO_CHUNK = 1024 * 1024

__all__ = [
    "PATCH_VERSION",
    "SIGNATURE_VERSION",
    "MIN_BLOCK_SIZE",
    "MAX_BLOCK_SIZE",
    "DEFAULT_BLOCK_SIZE",
    "MAX_ADD_CHUNK",
    "IO_CHUNK",
]
