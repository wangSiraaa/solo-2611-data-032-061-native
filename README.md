# xbrlcheck — 本地 XBRL 事实核对工具

用 Python 3.11、`lxml` 和 `Decimal` 实现的本地事实核对工具，用于回答“两份报表为什么对不齐 / 计算为什么不平”。

只处理题面声明的子集：不下载 taxonomy，不做通用 DTS、presentation、仓库平台或 XSD 校验。

## 安装与运行

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m xbrlcheck <command> ...
```

也可直接 `./.venv/bin/python -m xbrlcheck`。测试：`.venv/bin/pip install pytest && .venv/bin/python -m pytest tests -q`。

## 命令

```sh
# 规范事实、context/unit 身份、重复事实、被拒绝事实与诊断
python -m xbrlcheck inspect fixtures/report_a.xbrl [--json]

# 逐事实比较两份报告（按规范身份，context id 不同也能对齐）
python -m xbrlcheck compare fixtures/report_a.xbrl fixtures/report_b.xbrl [--json]

# 结合精度区间核对计算关系（可一次传多份报告）
python -m xbrlcheck check fixtures/report_a.xbrl [report2.xbrl ...] [--json]
```

- `--taxonomy XSD`：忽略实例 `schemaRef`，显式指定本地模式（可重复）。
- `--search-dir DIR`：额外解析目录（可重复）。
- 退出码：`0` 全部通过；`1` 存在差异/不平/缺项/被拒绝事实；`2` 链接库超出支持子集或实例无法解析。

## 支持与拒绝的边界

| 支持 | 拒绝（显式诊断，不猜测） |
| --- | --- |
| `monetaryItemType` / `decimalItemType` | tuple、非数值/字符串事实 |
| instant / duration context | `forever`、瞬时与期间混用 |
| 简单单位（分子 measure 集合） | `divide` 单位 |
| 显式维度 `xbrldi:explicitMember` | typed dimension、非维度 segment/scenario 内容 |
| 整数 `decimals` 与 `INF` | `precision` 属性、非整数 decimals |
| 单角色、无环、带权重、无 priority/prohibited 的计算关系 | 多角色、环、priority、prohibited、重复弧、非数值概念 |

概念、轴、成员、context、unit 均在**本地模式**中声明并校验；未声明的引用一律报错。

## 关键语义

### 规范身份（与前缀、XML 顺序无关）

- 概念身份 = `(命名空间 URI, 名称)`；`ex:Revenue` 与 `p:Revenue` 是同一概念。
- context 身份 = `(实体 scheme, 实体标识, 期间, 显式维度集合)`。
  - 期间按 `instant` 或 `start/end` 规范化；带时区的值化为 UTC。
  - 维度成员按轴排序后比较，元素顺序不影响身份。
  - **不含** context 的 XML `id`，因此 `north` 与 `renamed_context` 可对齐。
- unit 身份 = 排序后的 measure 集合。
- 事实身份 = `(概念, context 身份, unit 身份)`。

### 精度与数值

数值一律用 `Decimal`，不使用二进制浮点。`decimals=n` 表示真值落在闭区间
`[v - 0.5·10⁻ⁿ, v + 0.5·10⁻ⁿ]`；`decimals="INF"` 是单点 `[v, v]`。负数 `n` 表示按十位/百位舍入。

### 重复事实（不覆盖）

同一事实身份出现多次时全部保留并分类：

| 状态 | 含义 |
| --- | --- |
| `single` / `identical` | 单条 / 完全相同的重复 |
| `compatible` | 值不同但精度区间相交（只是舍入相容，不等于相等） |
| `conflict` | 精度区间不相交（矛盾） |
| `nil` | 全部为 nil |

nil、零、缺事实是三种不同状态：nil ≠ 0，缺项不补零。

### 计算核对

对每个父项，按**相同 context 身份和相同 unit** 对齐子项，乘以权重后比较区间：

- `consistent`：父项区间与加权子项和区间相交（差值区间含 0）。
- `inconsistent`：两区间不相交。
- `incomplete`：缺子项、子项为 nil，或子项只存在于别的单位/context（不借用、不补零）。
- `ambiguous`：父项或子项存在互相矛盾的重复事实。
- `nil-parent`：父项为 nil，无可核对。

每个结论都会列出父项事实、每条弧的子项事实、权重、折算后区间，以及
`父项区间 / 加权子项和 / 父项−子项` 三个区间。多层级关系按 root 起的 `depth` 分别核对：
叶子层的错误不会被汇总层“看起来平衡”掩盖。

> 数值相容只说明该关系在声明精度内相容，**不是**财报整体正确的结论。

## 目录

```
xbrlcheck/
  qname.py      QName 解析与常量
  precision.py  decimals 区间与 Decimal 运算
  model.py      身份、事实、分组、诊断等值对象
  taxonomy.py   本地模式与计算链接库加载（子集校验）
  parser.py     实例解析（context/unit/fact，拒绝越界构造）
  compare.py    跨报告逐事实比较
  calc.py       计算关系核对
  cli.py        inspect / compare / check
examples/       小型示例语料（见 examples/README.md）
tests/          单元与 CLI 回归测试
```

## 示例（仓库内）

```sh
python -m xbrlcheck compare examples/order_a.xbrl examples/order_b.xbrl   # 前缀/顺序/context id 不同 -> 全部 identical
python -m xbrlcheck check examples/precision_boundary.xbrl                 # 手算精度边界：一个相符、一个不平
python -m xbrlcheck check examples/isolation.xbrl                          # 币种/维度不混用
python -m xbrlcheck check examples/nil_zero_absent.xbrl                    # nil / 零 / 缺项
python -m xbrlcheck check examples/layers.xbrl                             # 多层关系分别解释
```
