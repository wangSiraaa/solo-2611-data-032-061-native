"""统一的错误类型。CLI 捕获后以退出码 2 报告输入/用户顺序错误。"""


class PlanoptError(Exception):
    """本包内所有可预期错误的基类。"""


class InputValidationError(PlanoptError):
    """输入 JSON 不满足规范（表、条件、选择率非法等）。"""


class UserOrderError(PlanoptError):
    """用户给定顺序不是合法排列。"""
