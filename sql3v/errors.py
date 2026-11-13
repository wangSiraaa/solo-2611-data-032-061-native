"""统一错误类型。所有用户可恢复错误都带行号与列号（从 1 开始）。"""


class Sql3vError(Exception):
    """领域/词法/语法/语义/运行阶段的受控错误。"""

    def __init__(self, message: str, line: int | None = None, col: int | None = None):
        super().__init__(message)
        self.message = message
        self.line = line
        self.col = col

    def with_location(self, line: int, col: int) -> "Sql3vError":
        if self.line is None:
            self.line = line
            self.col = col
        return self

    def located(self) -> str:
        if self.line is None:
            return self.message
        return f"第 {self.line} 行第 {self.col} 列: {self.message}"


class DomainError(Sql3vError):
    """声明文件（有限域定义）不合法。"""


class LexError(Sql3vError):
    """词法错误：非法字符、整数越界等。"""


class ParseError(Sql3vError):
    """语法错误：缺操作数、括号不匹配、不支持的语法等。"""


class VerifyError(Sql3vError):
    """与 SQLite 交叉核对不一致（正常情况下不应发生）。"""
