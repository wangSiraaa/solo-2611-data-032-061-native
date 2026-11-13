# 本地轨迹地图匹配 CLI

把带定位噪声的 GPS 采样点匹配到**有向道路网络**上真实可能经过的边，避免在
平行辅路之间反复跳转。纯本地运行，只依赖 Shapely、NetworkX、NumPy，不接
地图服务、不做路径缓存平台或地图界面；面向小路网与单车辆轨迹。

## 1. 安装与运行

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install pytest          # 仅自动验证需要

# 匹配（默认读取 fixtures/，写入 out/）
.venv/bin/python -m mapmatching match

# 内置冒烟自检（不依赖 pytest）
.venv/bin/python -m mapmatching selftest

# 完整自动验证
.venv/bin/python -m pytest tests/ -q
```

### match 参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--roads` | `fixtures/roads.geojson` | 有向道路 FeatureCollection（LineString） |
| `--nodes` | `fixtures/nodes.csv` | 节点坐标 `node_id,x,y` |
| `--gps` | `fixtures/gps.csv` | 采样点 `point_id,ts_ms,x,y,accuracy_m`（精度可空） |
| `--config` | `fixtures/config.json` | 半径、候选上限、速度松弛等配置 |
| `--out-dir` | `out` | 输出目录 |
| `--points-out` | `points.jsonl` | 逐点匹配结果文件名 |
| `--segments-out` | `segments.jsonl` | 连续路段序列文件名 |
| `--manifest-out` | `manifest.json` | 配置、计数与来源映射文件名 |
| `--set KEY=VALUE` | 无 | 覆盖任意数值配置项，可重复，如 `--set obs_weight=2.0` |

配置文件只要求给出需要的项，未给出的使用下表默认值；未知配置项会被拒绝。

| 配置键 | 默认值 | 含义 |
| --- | --- | --- |
| `candidate_radius_m` | 25.0 | 候选半径（米），投影距离超过即不产生候选 |
| `max_candidates` | 4 | 每点保留候选数上限 |
| `speed_slack_factor` | 1.3 | 允许平均速度 = 速度上限 × 该系数 |
| `obs_sigma_m` | 10.0 | 缺失/过精度声明时的观测尺度兜底（米） |
| `obs_weight` | 1.0 | 观测代价权重 `w_obs` |
| `transition_beta_m` | 10.0 | 转移绕路的指数尺度（米） |
| `transition_weight` | 1.0 | 转移代价权重 `w_trans` |
| `ambiguity_cost_gap` | 0.5 | 次优与最优总分差不超过该值即标记歧义 |

## 2. 输入约定

- **道路是有向 LineString**：`properties` 必须含 `edge_id`、`from`、`to`、
  `max_speed_mps`；`from`/`to` 必须引用 `nodes.csv` 中存在的节点。
- **只有共享节点才可转向，几何交叉不等于路口**。转向可行性完全由 NetworkX
  有向图的节点连通关系决定，LineString 仅负责投影、方位角与长度。fixture 中
  竖边 `gh` 与主干道在 `(100,0)`、`(100,12)` 几何相交但不共享节点，因此
  永远不能从主干道转上 `gh`。
- GPS 点含 `point_id`、整数毫秒 `ts_ms`、米制坐标 `x/y` 与可选 `accuracy_m`。
  加载后按时间戳稳定排序（同时间戳保持输入次序）。
- 全部坐标假定已在同一米制平面，程序不做投影变换。

## 3. 候选生成（不逐点选最近边）

每个采样点独立投影到半径内的**所有**有向边，按投影距离排序后保留
`max_candidates` 个。每个候选记录：

- `projection_distance_m`：到边几何的投影距离（米）；
- `along_m`：投影点沿**有向边**的里程 `[0, length]`；
- `projection_xy`、`bearing_deg`：投影点坐标与所在折线线段的有向方位角；
- `obs_cost`：该候选的观测代价；
- `source.feature_index`：来源 GeoJSON feature 行号（来源映射）。

候选排序键为 `(投影距离, edge_id)`，与输入 feature 顺序无关。

## 4. 联合序列决策与代价量纲

序列决策使用 Viterbi 动态规划，联合考虑**观测偏离**与**有向路径可行性**，
而不是逐点贪心取最近边。所有代价均为**无量纲加权分数，越小越好，不是真实
概率，不做 softmax 或归一化**。

### 4.1 观测代价

```
sigma   = max(accuracy_m, 0.25 * obs_sigma_m)
c_obs   = obs_weight * 0.5 * (projection_distance_m / sigma)^2
```

点自带精度优先；缺失或异常小的精度声明由 `obs_sigma_m` 设下限，避免低精度
声明把代价压到接近零。

### 4.2 转移可行性（硬约束）

相邻两个候选之间，先沿当前有向边走到其 `to` 节点，再经有向图最短路到下一
条边的 `from` 节点，最后沿下一条边走到投影位置。总路程包含两端在边内的
位置。下列任一情况转移被**硬禁止**，欧氏距离再近也不能放行：

1. `reverse_direction`：同一边上沿里程倒退，或图中不存在有向路径；
2. `no_directed_path`：两边之间没有共享节点连通的有向路径；
3. `zero_time_move`：`dt <= 0` 但有向路程 > 0（零时间只允许原地停留）；
4. `exceeds_speed_limit`：平均速度
   `有向路程 / dt > speed_slack_factor × 沿途最高速度上限`。

速度只按“两端边内位置 + 中间有向路径”的总里程与真实时间差做平均检查，
**不把没有直接观测的中间路段耗时分摊到任何采样点**。

### 4.3 转移软代价

仅对可行转移计算。绕路量取有向路程相对投影点欧氏距离的非负超出部分：

```
detour  = max(0, directed_path_length_m - euclidean_m)
c_trans = transition_weight * detour / transition_beta_m
```

### 4.4 总分与确定性

- 某点总分 = 链上各点 `c_obs` 与各可行转移 `c_trans` 之和。
- 平局规则：分数相同取候选 id 字典序最小者；图按 `edge_id` 排序构建，
  最短路等权结果由 NetworkX 的确定性遍历次序决定。**改变道路输入顺序不
  改变匹配结果**（由 `tests/test_order_invariance.py` 验证；只有来源映射
  里的 feature 行号会随输入行号变化）。

## 5. 断段规则与 UNKNOWN

不使用直线补路，也不生成任何不对应真实有向边的“虚拟路段”：

- 某点半径内无候选 → `status=UNKNOWN`，`unknown_reason=NO_CANDIDATE`；
- 某点候选与上一个已匹配点之间**所有**转移都不可行 →
  `status=UNKNOWN`，`unknown_reason=NO_FEASIBLE_TRANSITION`，并在
  `transition_failure_reasons` 与每个候选的 `dp_state.reasons` 中给出
  具体硬约束原因；该点若后续能重新续链则另起新 segment。
- segment 只覆盖真正连续匹配的点；其 `break_before`/`break_after` 给出
  断段位置与原因，`boundary_positions` 给出首尾在边内的里程。

fixture 中 `p7` 远离全部道路（孤立点）→ `NO_CANDIDATE`，把轨迹切成
`p1..p6` 与 `p8` 两个 segment；`p5/p6` 是同时间戳重复点，零时间零路程
停留被允许。

## 6. 歧义保留与“为什么没选最近道路”

- 每个点记录最优与次优**活状态**的总分差 `ambiguity_score_gap`；差距不
  超过 `ambiguity_cost_gap` 时 `ambiguous=true`。两条近乎等价的路线及其
  候选、续链分数都会保留，不会被静默合并。
- 当被选边不是几何最近边时，`nearest_edge_not_selected` 给出最近边及其
  转移失败原因；每个候选的 `dp_state` 还会标明 `reachable` 与不可达原因，
  可直接解释“某点为何没有选最近道路”。
- 输出中不出现 probability/posterior/likelihood 等概率字样。

### 噪声权重对歧义的影响

对现成 fixture 扫描 `obs_weight`（见 `tests/test_noise_weight.py` 的
平行走廊构造）：权重越低，绕路可行性越主导、轨迹越稳定留在主路；权重越
高，投影偏离越主导、越容易翻到几何更近的平行路；低权重区间次优差距很小
并被标记为歧义。示例（fixture 的 p2，最近候选是平行路 `de`）：

```
obs_weight=0.05  选择=ab  最近候选=de  gap=0.03125  ambiguous=True
obs_weight=0.5   选择=ab  最近候选=de  gap=0.3125   ambiguous=True
obs_weight=1.0   选择=ab  最近候选=de  gap=0.625    ambiguous=False
obs_weight=2.0   选择=ab  最近候选=de  gap=1.25     ambiguous=False
```

## 7. 输出契约（JSONL + 清单）

- `points.jsonl`：每行一个点，含原 `point_id`、全部候选（投影距离/沿边
  里程/方位角/观测代价/来源/DP 状态）、`selection` 或 `unknown_reason`、
  入边转移明细、歧义字段与最近边解释。
- `segments.jsonl`：每行一个连续路段，含有向边序列
  `directed_edge_sequence`（相邻重复边合并，回环重复保留）、逐点边/里程、
  断段位置与边界里程、观测与转移代价小计。
- `manifest.json`：生效配置、代价量纲说明、点数/段数计数，以及四个输入
  文件的绝对路径与 SHA-256（来源映射）。

## 8. 代码结构

```
mapmatching/
  config.py      配置加载与校验
  io.py          节点/道路/GPS 加载、来源哈希
  network.py     有向 MultiDiGraph 与确定性最短路
  candidates.py  投影候选与观测代价
  transitions.py 候选间有向转移的硬可行性与软代价
  matching.py    Viterbi 联合决策、断段、歧义与输出记录
  pipeline.py    端到端流水线
  cli.py         match / selftest 命令行
  selftest.py    不依赖 pytest 的冒烟自检
tests/           pytest 套件（含独立枚举、顺序不变性、权重敏感性）
```

## 9. 自动验证覆盖

- `test_enumeration.py`：独立枚举候选边的笛卡尔积，逐条序列用同一套转移
  规则判定可行性并累计代价，对照 Viterbi 的最优序列、边序列与总分；覆盖
  逆向、不可达、零时间跨边、超速四类硬禁止。
- `test_order_invariance.py`：多种确定性打乱道路 feature 次序，结果逐字段
  一致（来源行号除外），候选名次稳定。
- `test_noise_weight.py`：带共享中间节点的平行走廊，调节 `obs_weight`
  展示选择翻转与歧义保留；验证低精度声明会放松观测惩罚。
- `test_fixtures.py`：现成 fixture 的平行路、几何立交、单向回环、孤立点、
  零时间重复点、输出契约与输入校验。
