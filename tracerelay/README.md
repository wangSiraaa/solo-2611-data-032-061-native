# TraceRelay —— 离线合成追踪复盘工具

把并行下游、重试、异步任务、`all`/`any` 竞争等待混在一起时，**把所有子 span 耗时相加会大于
用户实际等待时间**，按父子树找最长链又可能把后台工作误判成瓶颈。TraceRelay 在本地用
SQLite + Python 标准库重建请求与执行片段的关系，产出两套**分别守恒**的账本和可操作的时间线。

> 数据边界：本工具只处理项目**合成**的追踪，不采集任何真实系统。所有时刻来自合成环境的同一
> 可信时钟（整数纳秒），不做主机时钟偏移反推。**本工具没有 CPU 观测，只解释记录到的墙钟活动
> 与等待，不能、也不会声称能证明任何服务的 CPU 使用率。**

## 1. 归因模型与两套守恒口径

### 口径 A：根响应时间如何被覆盖（coverage ledger）

对根 span 的有界窗口 `[root_start, root_end)` 做扫描线切分，得到四类**互斥且穷尽**的时间片：

| 类别 | 含义 | 证据要求 |
|------|------|----------|
| `self` 自身活动 | 根服务的内部计算、被显式 await 解释的客户端调用 | 具体 span 区间 |
| `await` 显式等待 | 显式 await 记录覆盖的窗口（`all`/`any`） | await 记录 |
| `queue` 排队等待 | 服务端 `queue_enter/enqueue → exec_start` 的权威排队区间 | 两个独立事件 |
| `gap` 不可解释空档 | 现有事件无法解释的区域（含缺服务端的未知传输，给上下界） | 无证据即 gap |

扫描线优先级（数字小者优先，归属确定不含糊）：

```
queue(0) > await(1) > self(2) > root_fallback(3) > gap(4)
```

- **并行等待按实际时间覆盖计入**：多个重叠 await/子请求先求区间并集，重叠部分永不重复相加。
- **嵌套 await 不重复计时**：内层 await 完全落在外层窗口内时，覆盖只按外层计一次；内层仅用于
  向下解释“真实阻塞在哪一层”（诊断 D5），外层已计的等待不再与下层组成部分相加。
- **根 span 只是兜底**：根 span 的存在不会把无证据区域伪装成 self；没有更细证据的兜底区域显式
  暴露为 `gap`。缺失时间戳全程保持 `NULL`，绝不伪造开始时刻、绝不把未知时间填零。
- 守恒不变量（每次分析自动核验，写 `conservation_ok`）：

  ```
  self_ns + await_ns + queue_ns + gap_ns == root_end - root_start
  ```

### 口径 B：各服务消耗了多少执行资源（resource ledger）

按 `(服务, 操作)` 汇总墙钟占用的**区间并集**：执行 / 客户端 span / 生产者 / 队列驻留 / 显式
等待 / 根结束后的后台工作。它允许跨并行分支累加（资源本来就被并行烧掉），因此：

> 资源账本的总和**不要求、也不应该**等于根响应窗口。两套账各自守恒，绝不混成一个总数。

- 异步消费者在根请求结束后继续运行 → 计 `background_after_root`（资源消耗），**不进**根覆盖、
  不算根响应瓶颈；开放区间（`end_ns` 缺失）只给下界，不给伪造上界。
- RPC 的 client span 与 server span 是**同一次调用的两面**：覆盖账本只经客户端/await 计一次，
  服务端执行进资源账本归属对应服务，绝不把两面当两份独立耗时相加。

### await 解除规则

- `all`：由**最后满足的必要结果**解除；用结果 span 末端确定最后满足者，再穿透 RPC 配对找真正
  干活的服务端，定位阻塞阶段（queue / execution / unknown_phase）。
- `any`：**只按实际胜出结果**（`resolved_by`）解释；未胜出分支与取消后残留工作另计 D6，进资源
  账本而不进根响应覆盖。
- 证据不足（无 `exec_start`/`queue_enter`、缺服务端）时阶段标 `unknown_phase`，结论降级。

## 2. 诊断（只在事件支持时下结论）

| 代码 | 含义 | 确定性 |
|------|------|--------|
| D1 | 下游尚未开始执行造成的队列等待 | certain（需两端事件） |
| D2 | 客户端已获得结果，等待者却迟迟恢复执行 | certain |
| D3 | 重试各尝试保留身份，失败+成功都显示，量化后台放大 | certain |
| D4 | 缺服务端记录，未知传输区间，给 `[0, client窗口]` 上下界 | bounded |
| D5 | 嵌套 await 的真实阻塞在更深一层 | certain |
| D6 | any 未胜出分支/取消后残留工作 | certain |
| D7 | 超时/取消后仍记录到活动 | certain/unknown |
| D8 | 长空档（>根窗口 10%） | unknown/bounded |
| D9 | 因果事件顺序不可能 | certain |
| D10 | 循环链接 / 身份冲突 | certain |

确定性三档：`certain`（事件足以支持）/ `bounded`（只能给上下界）/ `unknown`（证据缺失）。
**主动删去队列或服务端事件后，确定结论会退化为未知，而不是沿用旧缓存**（有专门测试守护）。

## 3. 目录结构

```
tracerelay/
├── tracerelay/
│   ├── model.py       # 区间几何、扫描线覆盖、池化分位数（纯函数，易单测）
│   ├── storage.py     # SQLite schema/索引、JSONL 幂等导入、折叠与身份冲突
│   ├── validate.py    # 图加载、循环检测、因果顺序不可能性、悬挂引用
│   ├── rpc.py         # client/server 配对（支持共享 server 一对多）、重试归组
│   ├── analyze.py     # 两套账本、await 解除归因、诊断、批量按需预取
│   ├── report.py      # 按服务/操作/请求类型汇总，池化分位数
│   ├── generator.py   # 11 类合成案例 + 十万级批量合成（全部本地合成）
│   ├── verify.py      # 独立核验：守恒/不重计/微案例手工预期
│   ├── exporting.py   # 导出分析输入、诊断、可复算中间片段
│   ├── server.py      # 标准库 http.server 只读接口（分页，不推全量原文）
│   └── cli.py         # generate/import/analyze/report/view/verify/export/serve
├── web/               # 原生 HTML/CSS/JS，零构建、零第三方依赖
├── tests/test_pipeline.py  # 18 个 unittest（含退化、打乱、循环、十万级）
└── var/               # 运行产物（db、jsonl、export），可随时删除重建
```

零第三方 pip 依赖，仅用 Python 3.11 标准库 + SQLite。

## 4. 使用说明

所有命令在 `tracerelay/` 目录下执行；`--db` 默认 `var/tracerelay.db`。

```bash
# 1) 生成合成数据：11 个手工微案例 + （可选）十万级批量
python3 -m tracerelay.cli generate --what cases --data-dir var/data
python3 -m tracerelay.cli generate --what bulk  --bulk-path var/data/bulk.jsonl
#   默认 9500 trace / 约 10.4 万 span；--bulk-traces 可调

# 2) 导入 JSONL（幂等：完全相同记录折叠，同 ID 不同内容记身份冲突并保留首条）
python3 -m tracerelay.cli --db var/t.db import var/data/case_*.jsonl

# 3) 分析：重建 RPC/重试关系，计算两套账本与诊断（守恒失败会以退出码 2 报错）
python3 -m tracerelay.cli --db var/t.db analyze

# 4) 独立核验：时间片和==根窗口、互不重叠、RPC 两面不重复计、微案例手工预期
python3 -m tracerelay.cli --db var/t.db verify

# 5) 单 trace 时间线 / await 解除 / RPC 双方 / 诊断；--raw-ref 定位原始 JSONL 行
python3 -m tracerelay.cli --db var/t.db view T-sync
python3 -m tracerelay.cli --db var/t.db view T-sync --raw-ref case_sync.jsonl 2

# 6) 批量报告（文本或 JSON）
python3 -m tracerelay.cli --db var/t.db report
python3 -m tracerelay.cli --db var/t.db report --json

# 7) 导出可复算中间片段（timeline/await/resource/diagnostic/rpc + 原始输入 + manifest）
python3 -m tracerelay.cli --db var/t.db export --out-dir var/export --trace-id T-sync

# 8) 本地时间线界面（并行泳道、RPC、await 解除、点击定位原始事件）
python3 -m tracerelay.cli --db var/t.db serve --host 127.0.0.1 --port 8765
#   浏览器打开 http://127.0.0.1:8765/
```

### 输入记录格式（JSONL，每行一条）

```jsonc
{"kind":"span","trace_id":"T","span_id":"c1","service":"gateway","operation":"callX",
 "span_type":"client","start_ns":8000000,"end_ns":24000000,"parent_id":"r1",
 "links":[{"span_id":"s1","rel":"rpc"}],"retry_of":null,"attempt_no":null}
{"kind":"event","trace_id":"T","event_id":"e1","span_id":"s1","name":"exec_start","at_ns":12000000}
{"kind":"await","trace_id":"T","await_id":"w1","waiter_span_id":"r1","start_ns":8000000,
 "end_ns":24000000,"result_ids":["c1"],"mode":"all","resolved_by":["c1"]}
```

`span_type ∈ client/server/internal/producer/consumer`；`end_ns/at_ns` 缺失用 `null`。
事件 `dispatch / queue_enter / exec_start / enqueue / cancel / timeout` 各自独立、允许缺失。

## 5. 11 个合成案例与手工时间片归属

| 案例 | 设计意图 | 根窗口归属（已由 verify 锁定） |
|------|----------|-------------------------------|
| T-sync | 同步链 + 队列事件 | self 14 + await 14 + queue 2 = 30ms |
| T-parallel | 并行 all，重叠不重复 | self 16 + await 12 + queue 4 = 32ms |
| T-any | 竞争 any，只认胜出者 | self 13 + await 10 + queue 1 = 24ms |
| T-retry | 超时重试，两次尝试都保留 | self 8 + await 29 + queue 3 = 40ms |
| T-async | 根结束后的消费者尾任务 | self 18；后台 30ms 仅入资源账本 |
| T-shared | 两个调用方共享同一 server | self 16 + await 14 = 30ms |
| T-missing | 缺服务端，传输未知（D4 给界） | self 14 + await 10 = 24ms，标不完整 |
| T-queue | 无 await 的排队 + 未知传输 | self 12 + queue 4 + gap 10 = 26ms |
| T-gap | 纯无证据空档 | self 11 + gap 9 = 20ms（D8） |
| T-nested | 嵌套 await 不重复计时（D5） | self 8 + await 18 + queue 2 = 28ms |
| T-lag | 结果就绪却迟迟恢复（D2） | self 8 + await 15 + queue 1 = 24ms |

每行的时间片都能在 `view` 里看到 `ref_kind:ref_id` 证据引用并点击回查原始 JSONL。

## 6. 十万级规模：索引与按需读取

- 导入后建立 14 个索引（trace、parent、type、时间、服务操作、证据回查等），按 trace 的查询走
  `ix_spans_trace` 等索引而非全表扫描（测试用 `EXPLAIN QUERY PLAN` 断言）。
- 批量分析**按 trace_id 顺序扫描一次小表并在内存分组**，避免上万次索引往返；分析结果整批一次
  `commit`，避免逐 trace fsync。实测（本机，约 10.4 万 span / 9.5 万事件 / 9500 trace）：

  | 阶段 | 耗时 |
  |------|------|
  | 导入 | ≈ 7s |
  | 分析 | ≈ 7s（优化前 ≈ 37s） |
  | 独立核验 | < 1s |

- 浏览器端**永远不拿全量原文**：trace 列表、泳道、事件、诊断全部强制 `LIMIT` 分页（上限 500）；
  原始 JSONL 仅在点击证据时通过 `/api/ref-loc` 按 `(source_file, line_no)` 单条回查。

## 7. 分位数口径

合并不同 trace 时，分位数只对**同一总体内的逐 trace 观测值**池化后用线性插值求 p50/p95/p99。
报告里每个分布都带 `quantile_method="pooled_per_trace_observations_linear_interp"` 与
`forbidden="do_not_sum_per_service_quantiles"` 字段——**严禁把各服务分位数相加冒充总体分位数**。

## 8. 测试

```bash
python3 -m unittest tests.test_pipeline -v
```

覆盖：区间几何与不重计、11 个微案例守恒、any 只认胜者、重试身份保留、后台不入根覆盖、删队列/
服务端事件后退化、未知时间不填零、打乱输入结果不变、重复折叠/身份冲突、parent/link 成环、因果
顺序不可能、开放 await 只给界、重分析不沿用旧缓存，以及十万级规模与索引使用。
