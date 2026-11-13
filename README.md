# queueops — 从运营事件重建到容量方案仿真

一个完整的、可独立复算的工具链，用于：

1. **观测重建**：从运营事件日志还原每个任务阶段的排队、服务、下游阻塞和跨观察窗口状态；
2. **服务模型校准**：只使用训练期数据，比较多种候选分布，正确纳入右删失，诊断样本不足、类别过细、分布不可识别和跨班次漂移；
3. **留出期验证**：用留出期真实到达和班次配置驱动仿真，对照吞吐、排队分布、服务占位和阻塞时间，并明确列出模型无法解释的差异；
4. **容量方案仿真**：在相同到达与相同基础随机抽样下改变班次/缓冲，解释等待改善与代价转移；
5. **核验**：独立真值只用于报告模型恢复误差和预测偏差，绝不进入拟合路径。

全部数据由本项目生成，不连接任何真实排班系统。

---

## 安装

```sh
python3 -m venv .venv
.venv/bin/pip install numpy scipy matplotlib pytest
```

（`pytest` 仅用于测试；`matplotlib` 仅用于 HTML 报告中的占用图。）

## 快速开始

```sh
# 端到端：生成 → 预检 → 校准 → 留出验证 → 情景比较 → 核验 → 报告
.venv/bin/python -m queueops all --data-dir data

# 或使用包装脚本
./bin/queueops all --data-dir data
```

产物位于 `data/`：

| 文件 | 内容 |
|---|---|
| `config.json` | 班次、工位、任务路由（无真实服务参数） |
| `events.json` | 运营事件日志（含缺失事件、重复、删失、留出期分布变化） |
| `truth.json` | **真值，仅核验器读取** |
| `windows.json` | 训练/留出期划分 |
| `scenarios.json` | 容量情景定义 |
| `preflight.json` | 预检诊断 |
| `timeline.json` | 逐任务阶段重建时间线 |
| `events_reconstructed.csv` | 可独立复算的逐任务事件 |
| `calibration.json` | 服务模型、候选比较、片段来源、诊断 |
| `validation.json` | 留出对照与无法解释的差异 |
| `comparison_gate.json` | 不确定模型版本的确认门 |
| `comparison.json` | 容量情景比较与代价转移解释 |
| `verification.json` | 核验结果（含恢复误差） |
| `report.html` | 可浏览报告（时间线、诊断、留出对照、占用图） |

## 独立命令

```sh
python -m queueops generate    --data-dir data --seed 20240001
python -m queueops preflight   --config data/config.json --events data/events.json --out data/preflight.json
python -m queueops reconstruct --config data/config.json --events data/events.json \
                               --timeline data/timeline.json --events-csv data/events_reconstructed.csv
python -m queueops calibrate   --config data/config.json --events data/events.json \
                               --windows data/windows.json --out data/calibration.json
python -m queueops validate    --config data/config.json --events data/events.json \
                               --calibration data/calibration.json --windows data/windows.json \
                               --out data/validation.json
python -m queueops simulate    --config data/config.json --events data/events.json \
                               --calibration data/calibration.json --windows data/windows.json \
                               --shift holdout --end-policy cutoff \
                               --out data/simulation.json --events-csv data/simulation_events.csv
python -m queueops compare     --config data/config.json --events data/events.json \
                               --calibration data/calibration.json --windows data/windows.json \
                               --scenarios data/scenarios.json --seeds 1,2,3 \
                               --confirm-reason "reviewed drift diagnostics; accept"
python -m queueops verify      --data-dir data
python -m queueops report      --data-dir data
```

`compare` 在模型版本含 `drift_hold` 或 `reject` 组时会 **拒绝运行**，直到操作者通过
`--confirm-reason` 提供书面原因（或在校准时对该组提供 override）。这保证情景比较只在
完成校准诊断后才发生。

## 设计要点与语义

### 事件类型
`arrive`（外部到达）→ `enqueue`（进入等待区）→ `start_service`（占用席位开始服务）→
`service_complete`（完成）→ `leave_station`（释放上游席位）→ `exit`（最终离开）。
返工可回到此前工位。

### 服务完成 ≠ 离开工位
下游等待区满时，已完成任务继续占用上游席位，状态为 `blocked`；
`blocking = leave_station - service_complete`。

### 时间与同刻顺序
统一整数毫秒时钟。同刻事件顺序：`service_complete` → 容量变更 → 外部到达，
之后逐工位确定性推进：开始排队任务 → 接收下游阻塞任务 → 接收外部到达。
等待区按 (优先级, 进入时间, 任务编号) 排序；阻塞转移按 (阻塞开始时间, 任务编号)。

### 有限缓冲与席位
- 等待区容量 **只计排队任务**，不计正在服务者或上游阻塞者；
- 工位可多席位，容量随班次变化；减员**不抢占**正在服务的任务，
  多余席位在任务离开后释放；
- 首站不能接收的到达留在独立的无界外部等待区，绝不丢弃。

### 截断/排空语义
- `cutoff`：到 `window_end` 停止；未完成服务是 **右删失**，不当作较短完成时长；
- `drain`：`window_end` 后不再接收新到达，冻结最后班次容量继续服务，
  直到空闲或无法推进；无法推进时报告 **阻塞链**（`deadlock`），不无限等待。

### 删失与未知（绝不填零）
- `exact`：直接观测；
- `right_censored`：观察期结束仍在服务，仅有下界；
- `interval_upper`：缺少 `service_complete` 但有离开/最终离开，仅有上界 `(0,U]`；
- `unknown`：无法界定。

右删失片段通过生存项（参数模型）或 Kaplan–Meier（经验分布）进入估计。
`interval_upper` 与 `unknown` 只报告、不当作完整样本；缺失值绝不写成 0。

### 校准方法
- 候选：`lognormal`、`gamma`（参数模型）与 `empirical_km`（Kaplan–Meier）；
- 参数模型用 `exact + right_censored` 的最大似然，按 AIC 选择；
- 类别过细或样本不足时合并到工位级，否则拒绝；
- 跨班次 KS 漂移检验（α=0.001）；检测到漂移时版本置为 `drift_hold`，
  需操作者书面确认才能用于情景比较；
- 每个参数记录其完整/删失片段来源与有效样本量（`provenance`、`used_fragments`）。

### 随机抽样绑定
服务抽样绑定 `(seed, task_id, stage_index, station, category)`，
由稳定的 BLAKE2b 哈希得到基础均匀数 `u`，再经模型逆 CDF 变换。
**改变容量不改变任何基础随机样本**（`invariance` 逐 seed 校验）。

## 核验内容

```sh
.venv/bin/python -m pytest tests -q
```

测试覆盖：

- 微型日志的 **手算** 服务/阻塞时长；
- 删失不被当成完成、未知不被当成零；
- 改变留出日志不改变冻结参数（摘要一致）；
- 重建不变量（单一席位、时间覆盖、数量闭合）；
- 容量变化时同一任务阶段基础抽样不变；
- 有限缓冲导致上游阻塞、减员不抢占；
- 无法推进时报告阻塞链而非永久等待；
- 右删失进入估计（拟合均值高于仅用完整样本的朴素均值）；
- `interval_upper` 不进入拟合；
- 真值隔离：建模模块静态上不引用 `truth.json`。

## 数据生成场景

- 3 工位 S1→S2→S3，S3 单席位为瓶颈；含返工路由（回到 S2）；
- 两个训练班次 `train_a`、`train_b`，其中一个包含 S2 服务漂移；
- 留出班次 `holdout` 负荷更高、S2 漂移更强、尾部到达突发以产生右删失；
- 已知真实服务分布、类别差异（`standard`/`heavy`）、有限缓冲阻塞；
- 训练日志注入缺失 `enqueue`/`service_complete`、重复与逆序；留出日志轻度损坏。

真值 `truth.json` 只被 `queueops/verifier.py` 读取，用于报告恢复误差与预测偏差；
**不要求随机样本每次完全恢复真实参数**，只作统计层面的核对。
