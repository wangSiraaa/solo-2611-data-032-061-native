# 示例语料

这些文件是**额外的**、自包含的小例子（自带 `taxonomy/`），用于演示工具语义；
原始输入仍在仓库根的 `fixtures/`。

| 文件 | 演示内容 |
| --- | --- |
| `order_a.xbrl`, `order_b.xbrl` | 同组事实：不同前缀、不同 context id、维度顺序相反、XML 顺序不同。`compare` 应为全部 `identical`。 |
| `isolation.xbrl` | CNY 父项有子项；USD 父项与 South 维度的父项都没有对应子项。后两者必须是 `incomplete`，不能借用 CNY/North 的子项。 |
| `nil_zero_absent.xbrl` | nil 子项、真实零子项、缺子项三种情形分别得到 `incomplete`、`consistent`、`incomplete`。 |
| `dup_identical.xbrl` | 同一事实重复两次且完全相同：保留两条，分组为 `identical`。 |
| `dup_compatible.xbrl` | 同一事实报为 `400` 与 `400.2`（decimals=0），精度区间相交于 `[399.7, 400.5]`：`compatible`。 |
| `dup_conflict.xbrl` | 同一事实报为 `400` 与 `410`（decimals=0），区间不相交：`conflict`，参与计算时该关系为 `ambiguous`。 |
| `precision_boundary.xbrl` | 手算精度边界。Q1 差值区间恰为 `[0, 3]`（含 0 → 相符）；Q2 为 `[1, 4]`（不含 0 → 不平）。 |
| `period_isolation.xbrl` | 子项只存在于另一个期间，父项为 `incomplete`。 |
| `layers.xbrl` | 两层计算树：Grand 层相符、SubA 层不平；两层分别报告，底层错误不会被掩盖。 |
| `unsupported.xbrl` | tuple、typed dimension、divide 单位、`precision`、未声明概念、期间类型不匹配、nil 带值等，全部显式拒绝。 |
| `taxonomy/` | 上述示例使用的本地模式、最小类型声明与计算链接库。 |
