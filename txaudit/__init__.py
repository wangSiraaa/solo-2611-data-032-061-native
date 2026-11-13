"""离线事务历史审计工具（单版本读写语义，视图/冲突可串行化判定）。

模块划分：
- model:    输入加载、结构校验与单版本语义回放
- analysis: 视图串行等价全枚举、冲突先后图与环检测
- verify:   独立实现的复核器（递归枚举，不复用 analysis 的排列过程）
- generate: 本地合成历史（固定样例套件 + 可复现随机历史）
- storage:  SQLite 保存输入与报告
- report:   规范（canonical）JSON 报告
- cli:      命令行入口
"""

SCHEMA_VERSION = "1.0"
