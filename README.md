# 题 140 输入材料

此目录内容复制到题目仓库根。fixtures/ 下是小型合成输入及必要的输入生成材料，不含求解程序、标准输出或评分答案。题面所列相对路径以仓库根为起点。全部输入仅用于本地开发，不含真实个人或业务数据。

依赖可按 requirements.txt 安装；无需 Docker、管理员权限、外部账号或模型下载。生成材料只负责构造输入，不是需要实现的业务能力。不要把固定输入值写死在处理器中。

# plancompare：离线计划对比工具

`plancompare` 离线对比两份 PostgreSQL 16 `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` 计划，
面向"同一 SQL 两次采集变慢"的场景：先判断两次采集是否可比，再区分**计划结构变化**与
**实际数据量/指标变化**，证据不足处显式保留，不按总耗时差归咎于单个算子。

## 比较命令

```sh
python3 -m plancompare compare fixtures/before.json fixtures/after.json \
    --capture fixtures/capture.json \
    --json-out plan_compare.json --md-out plan_compare.md
```

- `before` / `after`：两份计划 JSON 文件（位置参数，必填）。
- `--capture`：覆盖两侧的采集清单（SQL 身份、参数、数据快照、采集设置）。
  也可用 `--before-capture` / `--after-capture` 分别提供两份清单，此时会核对
  `sql_sha256`、参数与数据快照是否一致。不提供清单则只做结构对比并标注限制。
- 输出：`plan_compare.json`（机器可读对应表，每项指标带 JSON Pointer 与原值）和
  `plan_compare.md`（简短 Markdown 报告）。退出码恒为 0（分析成功），
  可比性结论见 JSON 的 `.comparability` 字段。

测试：`python3 -m unittest discover`

## 支持边界

- **节点类型**：解释 Seq Scan、Index Scan、Sort、Aggregate、Hash 及常见 Join
  （Nested Loop、Hash Join、Merge Join）。其他类型（如 Memoize、Gather）保留为
  **未知边界**：子树照常遍历与配对，节点本身只做通用字段展示，不做类型特定解释。
- **配对方法**：不使用数组下标，也不只按 Node Type。扫描节点按（关系，别名）、
  Join 按子树参与表集合、Sort/Aggregate 按排序键/分组键+输入表集合、Hash 按输入表
  集合对应；父上下文仅作兜底。结果分为：对应 / 新增 / 消失 / 无法唯一对应（歧义显式
  列出候选，不强行配对）；不同子树（参与表集合不同）不做比较。交换子节点顺序、
  增加无关字段不改变关系身份。
- **指标口径**：`Actual Rows` / `Actual Total Time` 为每次循环值，报告以 `×Loops`
  标注合计；父节点时间与缓冲包含子节点，总耗时只取根节点 / `Execution Time`，
  不把各节点时间相加；自耗时=自身×Loops−子节点×Loops，字段缺失、节点未执行或存在
  并行 Worker 时不推算并保留原因；`Total Cost` 是优化器估计单位，不是毫秒。
- **可比性**：SQL 身份或参数不一致 → 报告"不可直接比较"；数据快照不同 → 仍展示
  差异，但不能断言计划本身造成回归；两次采集设置不同 → 作为"可能解释"列出。
  观测到的变化与可能的解释在报告中分栏，每条结论回到原 JSON Pointer 及原值。
- **不做的事**：不连接任何数据库、不推荐索引、不改写 SQL、不做仪表盘、
  不做多轮 benchmark；单次运行的耗时差仅为观测，不作统计结论。

## 输入复现

`fixtures/make_plans.sql` 仅复现小输入（合成确定数据 + 两次不同设置的 EXPLAIN），
分析器只读取计划 JSON 与采集清单，不读取任何预设答案。
