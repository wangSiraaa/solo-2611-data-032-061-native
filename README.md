# planopt：可解释的左深连接顺序本地优化器

教学用关系查询连接顺序优化 CLI。**全排列全局穷举**求最优左深计划，
全程 `Fraction` 精确运算（不中间取整），独立选择率模型，纯 Python 3.11
标准库，SQLite 仅用于本地归档，不访问任何外部数据库或网络。

- 输入：1–8 张表（唯一非空 `table_id`、非负整数行数）+ 表间无向连接条件
  （精确十进制选择率字符串，取值 [0,1]，同一表对至多一条）。
- 输出：最优顺序、每步新应用条件、输入/输出基数、步骤成本、总成本、
  真实搜索量统计；可附带评估用户给定顺序及其与最优的差值。
- 并列裁决：总成本相同时，按**完整连接顺序**的 table_id Unicode 码点序取最小。
- 三套独立求解/校验路径：全排列穷举（主）、子集 DP（交叉核对）、
  手写 DFS 独立穷举 + 逐前缀重算（n≤6 核对全部并列候选）。

## 环境

- Linux、Python 3.11（仅用标准库，无需 pip 安装、无需 venv）。
- 本项目不使用 PostgreSQL；SQLite 随 Python 标准库提供，仅作本地结果归档。
- 依赖、数据库、运行日志及临时验证数据统一放在 `var/` 目录。

## 目录结构

```
planopt/            主包（无第三方依赖）
  decimalx.py       精确十进制 -> Fraction
  model.py          数据模型与输入校验
  evaluate.py       题面公式的权威求值 + 前缀重算
  optimizer.py      全排列穷举 / 子集 DP
  verifier.py       独立穷举、不变量、洗牌、结果复核
  presets.py        12 个合成预设 + 固定种子随机生成
  render.py         规范 JSON 与人类可读 explain
  archive.py        SQLite 归档
  cli.py            generate/optimize/explain/verify 子命令
tests/              unittest 测试（24 个用例）
demo.sh             一键端到端演示
REPORT.md           设计报告、全部假定、手算案例、实测数据
var/                本地产物（SQLite、JSON），运行时生成
```

## 快速开始

```bash
# 自测
python3 -m tests.run_tests

# 一键演示（生成→优化→解释→验证→八表计时→归档复核）
./demo.sh
```

## 命令用法

### 1. 生成合成配置

```bash
python3 -m planopt generate --preset greedy_trap_3 -o var/case.json
python3 -m planopt generate --random 8 --seed 42        # 固定种子随机 8 表
# 预设：chain_4 star_5 ring_4 disjoint_4 empty_3 high_sel_large_4
#       zero_sel_3 ties_no_cond_3 greedy_trap_3 greedy_trap_first_4
#       triangle_tie_3 ties_6 eight_8
```

### 2. 全局优化（可带用户顺序、SQLite 归档）

```bash
python3 -m planopt optimize -i var/case.json \
    --user-order S L M \
    --db var/archive.sqlite3 --preset-tag greedy_trap_3 \
    -o var/result.json
```

- 不带 `--input` 时从标准输入读取；不带 `--output` 时规范 JSON 写标准输出。
- `--db` 不给路径时默认 `var/planopt_archive.sqlite3`。
- 摘要（排列数、最优顺序、成本、耗时、run_id）写标准错误。

### 3. 人类可读解释

```bash
python3 -m planopt explain -i var/case.json --result var/result.json
# 不给 --result 则现场重算
```

### 4. 独立验证

```bash
# 文件对文件 + 洗牌不变性
python3 -m planopt verify -i var/case.json --result var/result.json --shuffle
# 从 SQLite 归档复核某次运行
python3 -m planopt verify --run-id 1 --db var/archive.sqlite3
```

检查项：搜索量等于 n!、穷举/DP/重放三方最优一致、逐步数值一致、
每条条件只在两端首次同时出现时应用一次、n≤6 独立穷举并列裁决、
用户顺序差值、洗牌（表序/条件序/端点方向）不变性。任一失败退出码为 1。

## 输入示例

```json
{
  "tables": [
    {"table_id": "S", "rows": 20},
    {"table_id": "M", "rows": 100},
    {"table_id": "L", "rows": 10000}
  ],
  "conditions": [
    {"left": "S", "right": "L", "selectivity": "0.5"},
    {"left": "M", "right": "L", "selectivity": "0.001"}
  ]
}
```

选择率必须是**字符串形式的精确十进制**；`0.1` 这样的 JSON 浮点会被拒绝。
基数与成本在结果中以精确分数字符串表示（如 `"1/1000"`、`"22220"`）。

## 公式（与 REPORT.md 一致）

- 初始基数 = 首表行数；加入 T：输出基数 = 此前基数 × T 行数 × T 与所有
  已加入表之间条件选择率之积（无条件为 1，即笛卡尔积）。
- 初始成本 = 首表行数；每步成本 = 此前基数 + T 行数 + 本步输出基数；
  总成本 = 各步成本之和。零行/零选择率严格按公式传播。

## 退出码

0 成功；1 验证存在失败项；2 输入或用户顺序非法（错误信息写标准错误）。
