# 计划对比报告（离线分析）

- before：`fixtures/before.json`
- after：`fixtures/after.json`
- 采集清单：`fixtures/capture.json`

## 1. 可比性结论

**可直接比较（SQL 身份、参数、数据快照一致）**

| 检查项 | 结果 | 依据 |
| --- | --- | --- |
| sql_identity | same_manifest | 同一清单覆盖两侧，sql_sha256=4ba802827edac7212557d014f97d8f46b608a92e93298543547599bc3cfd4d18 |
| parameters | same_manifest | `{}` |
| data_snapshot | same_manifest | `a034677a8e8453dcaa4e78c0fc8b1725369faf8af334b6d4bdacc7a378252b91` |
| capture_settings | differ | before: `SET max_parallel_workers_per_gather=0; SET jit=off; SET enable_nestloop=off; SET enable_mergejoin=off; SET enable_hashjoin=on; SET enable_hashagg=on;`<br>after: `SET max_parallel_workers_per_gather=0; SET jit=off; SET enable_nestloop=on; SET enable_mergejoin=off; SET enable_hashjoin=off; SET enable_hashagg=off;` |

> 注意：当前条件下不能把差异断言为“计划本身造成回归”（见下方保留项）。

- 保留项：两次采集的执行设置不同：计划结构变化可能由设置驱动，不能单独归因于数据或优化器
- 保留项：清单注明：Single small local run; not a statistically supported performance benchmark. Settings intentionally differ to produce plan structure changes.

## 2. 口径与边界（先读）

- `Actual Rows` / `Actual Total Time` 为**每次循环**值；列名含 `×Loops` 的为乘上 `Actual Loops` 的合计。
- 父节点时间/缓冲**包含**子节点；总耗时以根节点或 `Execution Time` 为准，**不能**把全部节点时间相加。
- 自耗时 = 自身×Loops 总时间 − 子节点×Loops 总时间；字段缺失或存在并行 Worker 时不推算，标记原因。
- `Total Cost` 是优化器估计单位，**不是毫秒**。
- 本工具只做离线对比：不连接数据库、不推荐索引、不改写 SQL。

## 3. 总体观测

| 指标 | before | after | 变化 | 依据 |
| --- | --- | --- | --- | --- |
| Execution Time (ms) | 3.537 | 3.936 | +0.399 ms（+11.3%） | `/0/Execution Time` |
| Planning Time (ms) | 82.010 | 0.758 | -81.252 ms（-99.1%） | `/0/Planning Time` |
| 根节点类型 | Sort | Aggregate | 变化 | `/0/Plan/Node Type` |
| 结果行数（根 ×Loops） | 6 | 6 | 不变 | `/0/Plan/Actual Rows` |

## 4. 结构对应关系

配对依据：关系/别名、子树参与表集合、Sort/Aggregate 键与父上下文；不使用数组下标，不强行配对。

| ID | 状态 | before | after | 依据 / 结构变化 |
| --- | --- | --- | --- | --- |
| c01 | 对应 | Sort key(c.region, p.region)<br>`/0/Plan` | Sort key(c.region, p.region)<br>`/0/Plan/Plans/0` | Sort 按排序键 ['c.region', 'p.region'] + 输入表集合 {c,o,p} 对应，两侧均唯一；树中位置变化：before 位于 Sort，after 位于 Aggregate>Sort |
| c02 | 对应 | Aggregate[Hashed] group(c.region, p.region)<br>`/0/Plan/Plans/0` | Aggregate[Sorted] group(c.region, p.region)<br>`/0/Plan` | Aggregate 按分组键 ['c.region', 'p.region'] + 输入表集合 {c,o,p} 对应，两侧均唯一；Aggregate 策略变化：Hashed → Sorted；树中位置变化：before 位于 Sort>Aggregate，after 位于 Aggregate |
| c03 | 对应 | Hash Join {c,o,p}<br>`/0/Plan/Plans/0/Plans/0` | Nested Loop {c,o,p}<br>`/0/Plan/Plans/0/Plans/0` | 连接节点按参与表集合 {c,o,p} 对应，两侧均唯一；节点类型变化：Hash Join → Nested Loop；Inner Unique变化：True → False；树中位置变化：before 位于 Sort>Aggregate>Hash Join，after 位于 Aggregate>Sort>Nested Loop |
| c04 | 对应 | Seq Scan orders o<br>`/0/Plan/Plans/0/Plans/0/Plans/0/Plans/0` | Index Scan orders o<br>`/0/Plan/Plans/0/Plans/0/Plans/1` | 扫描节点按 关系=orders 别名=o 对应，两侧均唯一；节点类型变化：Seq Scan → Index Scan；树中位置变化：before 位于 Sort>Aggregate>Hash Join>Hash Join>Seq Scan，after 位于 Aggregate>Sort>Nested Loop>Index Scan |
| c05 | 对应 | Seq Scan customers c<br>`/0/Plan/Plans/0/Plans/0/Plans/0/Plans/1/Plans/0` | Seq Scan customers c<br>`/0/Plan/Plans/0/Plans/0/Plans/0/Plans/0` | 扫描节点按 关系=customers 别名=c 对应，两侧均唯一；树中位置变化：before 位于 Sort>Aggregate>Hash Join>Hash Join>Hash>Seq Scan，after 位于 Aggregate>Sort>Nested Loop>Nested Loop>Seq Scan |
| c06 | 对应 | Seq Scan customers p<br>`/0/Plan/Plans/0/Plans/0/Plans/1/Plans/0` | Index Scan customers p<br>`/0/Plan/Plans/0/Plans/0/Plans/0/Plans/1/Plans/0` | 扫描节点按 关系=customers 别名=p 对应，两侧均唯一；节点类型变化：Seq Scan → Index Scan；树中位置变化：before 位于 Sort>Aggregate>Hash Join>Hash>Seq Scan，after 位于 Aggregate>Sort>Nested Loop>Nested Loop>Memoize>Index Scan |
| c07 | 消失 | Hash Join {c,o}<br>`/0/Plan/Plans/0/Plans/0/Plans/0` | — | after 中不存在相同签名的节点（join 参与表={c,o}）（行数×Loops=2124，时间×Loops=1.829 ms） |
| c08 | 消失 | Hash {c}<br>`/0/Plan/Plans/0/Plans/0/Plans/0/Plans/1` | — | after 中不存在相同签名的节点（Hash 输入表={c}）（行数×Loops=133，时间×Loops=0.125 ms） |
| c09 | 消失 | Hash {p}<br>`/0/Plan/Plans/0/Plans/0/Plans/1` | — | after 中不存在相同签名的节点（Hash 输入表={p}）（行数×Loops=200，时间×Loops=0.141 ms） |
| c10 | 新增 | — | Nested Loop {c,p}<br>`/0/Plan/Plans/0/Plans/0/Plans/0` | before 中不存在相同签名的节点（join 参与表={c,p}）（行数×Loops=133，时间×Loops=0.243 ms） |
| c11 | 新增 | — | Memoize (unsupported)<br>`/0/Plan/Plans/0/Plans/0/Plans/0/Plans/1` | before 中不存在相同签名的节点（节点类型 Memoize（不在支持集内，仅按父上下文匹配））（行数×Loops=133，时间×Loops=0.133 ms） |

## 5. 对应节点指标对比

### c01 Sort key(c.region, p.region) ↔ Sort key(c.region, p.region)

- before：`/0/Plan` · after：`/0/Plan/Plans/0`
- 结构变化：树中位置变化：before 位于 Sort，after 位于 Aggregate>Sort

| 指标 | before | after | 变化 |
| --- | --- | --- | --- |
| Plan Rows（估计） | 9 | 2125 | +2116.000（+23511.1%） |
| Actual Rows（每循环） | 6 | 2124 | +2118.000（+35300.0%） |
| Actual Loops | 1 | 1 | 不变 |
| Actual Rows ×Loops | 6 | 2124 | +2118.000（+35300.0%） |
| 估计偏差（每循环 实际/估计） | 0.67 | 1.00 | +0.333（+49.9%） |
| 时间 ×Loops ms（含子节点） | 3.400 | 3.369 | -0.031（-0.9%） |
| Total Cost（估计单位，非毫秒） | 120.44 | 365.75 | +245.310（+203.7%） |
| 自耗时 ×Loops ms（推算） | 0.103 | 1.165 | +1.062 |
| Shared Hit Blocks（含子节点） | 29 | 3006 | +2977.000（+10265.5%） |
| Shared Hit Blocks（仅自身，推算） | 3 | 3 | 不变 |
| Shared Read Blocks（含子节点） | 0 | 5 | +5.000 |
| Shared Read Blocks（仅自身，推算） | 0 | 0 | 不变 |

属性变化：

- `Sort Space Used`：`25` → `179`（`/0/Plan/Sort Space Used`）

### c02 Aggregate[Hashed] group(c.region, p.region) ↔ Aggregate[Sorted] group(c.region, p.region)

- before：`/0/Plan/Plans/0` · after：`/0/Plan`
- 结构变化：Aggregate 策略变化：Hashed → Sorted
- 结构变化：树中位置变化：before 位于 Sort>Aggregate，after 位于 Aggregate

| 指标 | before | after | 变化 |
| --- | --- | --- | --- |
| Plan Rows（估计） | 9 | 9 | 不变 |
| Actual Rows（每循环） | 6 | 6 | 不变 |
| Actual Loops | 1 | 1 | 不变 |
| Actual Rows ×Loops | 6 | 6 | 不变 |
| 估计偏差（每循环 实际/估计） | 0.67 | 0.67 | 不变 |
| 时间 ×Loops ms（含子节点） | 3.297 | 3.818 | +0.521（+15.8%） |
| Total Cost（估计单位，非毫秒） | 120.27 | 387.09 | +266.820（+221.9%） |
| 自耗时 ×Loops ms（推算） | 0.731 | 0.449 | -0.282 |
| Shared Hit Blocks（含子节点） | 26 | 3006 | +2980.000（+11461.5%） |
| Shared Hit Blocks（仅自身，推算） | 0 | 0 | 不变 |
| Shared Read Blocks（含子节点） | 0 | 5 | +5.000 |
| Shared Read Blocks（仅自身，推算） | 0 | 0 | 不变 |

属性变化：

- `Strategy`：`Hashed` → `Sorted`（`/0/Plan/Plans/0/Strategy`）

### c03 Hash Join {c,o,p} ↔ Nested Loop {c,o,p}

- before：`/0/Plan/Plans/0/Plans/0` · after：`/0/Plan/Plans/0/Plans/0`
- 结构变化：节点类型变化：Hash Join → Nested Loop
- 结构变化：Inner Unique变化：True → False
- 结构变化：树中位置变化：before 位于 Sort>Aggregate>Hash Join，after 位于 Aggregate>Sort>Nested Loop

| 指标 | before | after | 变化 |
| --- | --- | --- | --- |
| Plan Rows（估计） | 2125 | 2125 | 不变 |
| Actual Rows（每循环） | 2124 | 2124 | 不变 |
| Actual Loops | 1 | 1 | 不变 |
| Actual Rows ×Loops | 2124 | 2124 | 不变 |
| 估计偏差（每循环 实际/估计） | 1.00 | 1.00 | 不变 |
| 时间 ×Loops ms（含子节点） | 2.566 | 2.204 | -0.362（-14.1%） |
| Total Cost（估计单位，非毫秒） | 98.93 | 242.99 | +144.060（+145.6%） |
| 自耗时 ×Loops ms（推算） | 0.596 | 0.365 | -0.231 |
| Shared Hit Blocks（含子节点） | 26 | 3003 | +2977.000（+11450.0%） |
| Shared Hit Blocks（仅自身，推算） | 0 | 0 | 不变 |
| Shared Read Blocks（含子节点） | 0 | 5 | +5.000 |
| Shared Read Blocks（仅自身，推算） | 0 | 0 | 不变 |

属性变化：

- `Hash Cond`：`(c.parent_id = p.id)` → `None`（`/0/Plan/Plans/0/Plans/0/Hash Cond`）
- `Inner Unique`：`True` → `False`（`/0/Plan/Plans/0/Plans/0/Inner Unique`）

### c04 Seq Scan orders o ↔ Index Scan orders o

- before：`/0/Plan/Plans/0/Plans/0/Plans/0/Plans/0` · after：`/0/Plan/Plans/0/Plans/0/Plans/1`
- 结构变化：节点类型变化：Seq Scan → Index Scan
- 结构变化：树中位置变化：before 位于 Sort>Aggregate>Hash Join>Hash Join>Seq Scan，after 位于 Aggregate>Sort>Nested Loop>Index Scan

| 指标 | before | after | 变化 |
| --- | --- | --- | --- |
| Plan Rows（估计） | 3196 | 16 | -3180.000（-99.5%） |
| Actual Rows（每循环） | 3196 | 16 | -3180.000（-99.5%） |
| Actual Loops | 1 | 133 | +132.000（+13200.0%） |
| Actual Rows ×Loops | 3196 | 2128 | -1068.000（-33.4%） |
| 估计偏差（每循环 实际/估计） | 1.00 | 1.00 | 不变 |
| 时间 ×Loops ms（含子节点） | 0.910 | 1.596 | +0.686（+75.4%） |
| Total Cost（估计单位，非毫秒） | 72 | 1.52 | -70.480（-97.9%） |
| 自耗时 ×Loops ms（推算） | 0.910 | 1.596 | +0.686 |
| Shared Hit Blocks（含子节点） | 22 | 2921 | +2899.000（+13177.3%） |
| Shared Hit Blocks（仅自身，推算） | 22 | 2921 | +2899.000（+13177.3%） |
| Shared Read Blocks（含子节点） | 0 | 5 | +5.000 |
| Shared Read Blocks（仅自身，推算） | 0 | 5 | +5.000 |

属性变化：

- `Rows Removed by Filter`：`804` → `4`（`/0/Plan/Plans/0/Plans/0/Plans/0/Plans/0/Rows Removed by Filter`）
- `Index Name`：`None` → `orders_customer_idx`（`/0/Plan/Plans/0/Plans/0/Plans/0/Plans/0/Index Name`）
- `Index Cond`：`None` → `(customer_id = c.id)`（`/0/Plan/Plans/0/Plans/0/Plans/0/Plans/0/Index Cond`）
- `Rows Removed by Index Recheck`：`None` → `0`（`/0/Plan/Plans/0/Plans/0/Plans/0/Plans/0/Rows Removed by Index Recheck`）
- `Scan Direction`：`None` → `Forward`（`/0/Plan/Plans/0/Plans/0/Plans/0/Plans/0/Scan Direction`）

### c05 Seq Scan customers c ↔ Seq Scan customers c

- before：`/0/Plan/Plans/0/Plans/0/Plans/0/Plans/1/Plans/0` · after：`/0/Plan/Plans/0/Plans/0/Plans/0/Plans/0`
- 结构变化：树中位置变化：before 位于 Sort>Aggregate>Hash Join>Hash Join>Hash>Seq Scan，after 位于 Aggregate>Sort>Nested Loop>Nested Loop>Seq Scan

| 指标 | before | after | 变化 |
| --- | --- | --- | --- |
| Plan Rows（估计） | 133 | 133 | 不变 |
| Actual Rows（每循环） | 133 | 133 | 不变 |
| Actual Loops | 1 | 1 | 不变 |
| Actual Rows ×Loops | 133 | 133 | 不变 |
| 估计偏差（每循环 实际/估计） | 1.00 | 1.00 | 不变 |
| 时间 ×Loops ms（含子节点） | 0.061 | 0.058 | -0.003（-4.9%） |
| Total Cost（估计单位，非毫秒） | 4.50 | 4.50 | 不变 |
| 自耗时 ×Loops ms（推算） | 0.061 | 0.058 | -0.003 |
| Shared Hit Blocks（含子节点） | 2 | 2 | 不变 |
| Shared Hit Blocks（仅自身，推算） | 2 | 2 | 不变 |
| Shared Read Blocks（含子节点） | 0 | 0 | 不变 |
| Shared Read Blocks（仅自身，推算） | 0 | 0 | 不变 |

### c06 Seq Scan customers p ↔ Index Scan customers p

- before：`/0/Plan/Plans/0/Plans/0/Plans/1/Plans/0` · after：`/0/Plan/Plans/0/Plans/0/Plans/0/Plans/1/Plans/0`
- 结构变化：节点类型变化：Seq Scan → Index Scan
- 结构变化：树中位置变化：before 位于 Sort>Aggregate>Hash Join>Hash>Seq Scan，after 位于 Aggregate>Sort>Nested Loop>Nested Loop>Memoize>Index Scan

| 指标 | before | after | 变化 |
| --- | --- | --- | --- |
| Plan Rows（估计） | 200 | 1 | -199.000（-99.5%） |
| Actual Rows（每循环） | 200 | 1 | -199.000（-99.5%） |
| Actual Loops | 1 | 40 | +39.000（+3900.0%） |
| Actual Rows ×Loops | 200 | 40 | -160.000（-80.0%） |
| 估计偏差（每循环 实际/估计） | 1.00 | 1.00 | 不变 |
| 时间 ×Loops ms（含子节点） | 0.089 | 0.040 | -0.049（-55.1%） |
| Total Cost（估计单位，非毫秒） | 4 | 0.28 | -3.720（-93.0%） |
| 自耗时 ×Loops ms（推算） | 0.089 | 0.040 | -0.049 |
| Shared Hit Blocks（含子节点） | 2 | 80 | +78.000（+3900.0%） |
| Shared Hit Blocks（仅自身，推算） | 2 | 80 | +78.000（+3900.0%） |
| Shared Read Blocks（含子节点） | 0 | 0 | 不变 |
| Shared Read Blocks（仅自身，推算） | 0 | 0 | 不变 |

属性变化：

- `Index Name`：`None` → `customers_pkey`（`/0/Plan/Plans/0/Plans/0/Plans/1/Plans/0/Index Name`）
- `Index Cond`：`None` → `(id = c.parent_id)`（`/0/Plan/Plans/0/Plans/0/Plans/1/Plans/0/Index Cond`）
- `Rows Removed by Index Recheck`：`None` → `0`（`/0/Plan/Plans/0/Plans/0/Plans/1/Plans/0/Rows Removed by Index Recheck`）
- `Scan Direction`：`None` → `Forward`（`/0/Plan/Plans/0/Plans/0/Plans/1/Plans/0/Scan Direction`）

## 6. 观测到的变化（事实）

- 结果行数（根节点 ×Loops）：6 → 6，结果集行数一致（依据：`/0/Plan/Actual Rows`=6.0；`/0/Plan/Actual Rows`=6.0）
- Execution Time：3.537 ms → 3.936 ms（+0.399 ms（+11.3%））；单次运行，仅作观测（依据：`/0/Execution Time`=3.537；`/0/Execution Time`=3.936）
- Planning Time：82.01 ms → 0.758 ms（-81.252 ms）（依据：`/0/Planning Time`=82.01；`/0/Planning Time`=0.758）
- 节点对应：6 对对应（其中 3 对节点类型变化），2 个新增，3 个消失，0 组无法唯一对应
- Seq Scan orders o：Actual Loops 1 → 133（依据：`/0/Plan/Plans/0/Plans/0/Plans/0/Plans/0/Actual Loops`=1.0；`/0/Plan/Plans/0/Plans/0/Plans/1/Actual Loops`=133.0）
- Seq Scan orders o：Shared Read Blocks（仅自身，推算）0 → 5（依据：`/0/Plan/Plans/0/Plans/0/Plans/0/Plans/0/Shared Read Blocks`=0.0；`/0/Plan/Plans/0/Plans/0/Plans/1/Shared Read Blocks`=5.0）
- Seq Scan customers p：Actual Loops 1 → 40（依据：`/0/Plan/Plans/0/Plans/0/Plans/1/Plans/0/Actual Loops`=1.0；`/0/Plan/Plans/0/Plans/0/Plans/0/Plans/1/Plans/0/Actual Loops`=40.0）
- 自耗时（推算）增加：Sort key(c.region, p.region) → Sort key(c.region, p.region) 0.103 ms → 1.165 ms（+1.062 ms）；该对节点存在树中位置变化，口径需谨慎（依据：`/0/Plan/Actual Total Time`=3.4；`/0/Plan/Plans/0/Actual Total Time`=3.369）
- 自耗时（推算）增加：Seq Scan orders o → Index Scan orders o 0.91 ms → 1.596 ms（+0.686 ms）；该对节点存在树中位置变化，口径需谨慎（依据：`/0/Plan/Plans/0/Plans/0/Plans/0/Plans/0/Actual Total Time`=0.91；`/0/Plan/Plans/0/Plans/0/Plans/1/Actual Total Time`=1.596）

## 7. 可能的解释（假设，需人工确认）

- 两次采集的执行设置不同（before: 'SET max_parallel_workers_per_gather=0; SET jit=off; SET enable_nestloop=off; SET enable_mergejoin=off; SET enable_hashjoin=on; SET enable_hashagg=on;'；after: 'SET max_parallel_workers_per_gather=0; SET jit=off; SET enable_nestloop=on; SET enable_mergejoin=off; SET enable_hashjoin=off; SET enable_hashagg=off;'）：计划结构差异可能由设置驱动，需人工确认
- 本工具不判断因果；以上仅为待人工核实的假设

## 8. 限制

- Actual Rows / Actual Total Time 为每次循环值；报告以 ×Loops 标注合计
- 父节点时间与缓冲包含子节点；总耗时只取根节点 / Execution Time，未将各节点时间相加
- 自耗时为推算值（自身×Loops − 子节点×Loops）；字段缺失、节点未执行或存在并行 Worker 时不推算并保留原因
- Total Cost 为优化器估计单位，不能当作毫秒
- 本工具离线分析两份计划文件，不连接数据库、不推荐索引、不改写 SQL
- after 含未知边界节点（不做类型特定解释）：Memoize(/0/Plan/Plans/0/Plans/0/Plans/0/Plans/1)
