# xlcalc：一次性 OOXML 工作簿计算工具

面向“公式缓存过期、又不能依赖 Excel 重算”的场景，直接读取 OOXML
（`.xlsx` 即 ZIP+XML），在受限公式子集内独立求值，并生成**保留公式与
布局、只刷新值缓存**的结果副本。不提供增量服务、网页编辑器或常驻进程。

- 运行环境：Python 3.11，依赖仅 `openpyxl`（第二读取入口/测试造数）与
  `lxml`（OOXML 解析与回写）；见 `requirements.txt`。
- 求值核心（词法、语法、引用位移、依赖求值）**不调用 Python
  `eval`/`exec`/`compile`**，全部为手写递归下降与 AST 解释器。
- 源文件始终只读打开，绝不原地写回；`verify` 会核对源文件 sha256 未变。

## 三个命令

```sh
python -m xlcalc calculate fixtures/model.xlsx --outdir out
python -m xlcalc explain   fixtures/model.xlsx "'Sales Detail'!D2:D5"
python -m xlcalc verify    fixtures/model.xlsx out/model.calculated.xlsx out/model.values.json
```

`calculate` 一次产出四份文件（默认在 `out/`）：

| 产物 | 内容 |
| --- | --- |
| `*.calculated.xlsx` | 结果副本：工作表顺序、合并区域、样式/数字格式、原公式文本全部保留，仅公式格的 `<v>`/`t` 缓存被刷新 |
| `*.values.json` | 值视图：每个单元格的六类值之一 |
| `*.explain.json` | 单元格解释：依赖、计算值或失败来源（错误码+原因）、共享跟随格的展开公式 |
| `*.verify.json` | 独立双入口验证报告（ZIP/XML + openpyxl） |

`explain` 支持 `Sheet!A1`、`'含空格 表名'!A1:B2` 矩形范围（逐格输出），
省略表名时取第一张表。

## 支持的公式子集

- 引用：`A1`、`$A$1`、`A$1`、`$A1`、矩形 `A1:B2`；本表或跨表；
  表名含空格/特殊字符时用单引号包裹（`'Sales Detail'!D2`）。
- 运算：括号、`+ - * /`、比较 `= <> < <= > >=`、一元正负号。
- 函数：仅 `SUM`、`IF`、`IFERROR`。
- 字面量：数字（含小数、科学计数法）、双引号字符串（`""` 转义）、
  `TRUE`/`FALSE`。

显式不支持并落为 `#UNSUPPORTED!`（带可读原因，绝不用旧缓存冒充）：

- 命名区域/裸名称、外部链接（`[1]...`）、数组公式（`t="array"`）、
  模拟运算表（`t="dataTable"`）；
- 三个之外的任何函数（如 `VLOOKUP`、`POWER`、`CONCAT`）；
- 交集/并集运算符、文本连接 `&`、百分号 `%`、乘方 `^` 等子集外语法；
- 共享公式位移后越界、引用不存在的工作表（后者为 `#REF!`）。

## 六类值与受限语义

严格区分：**空白 BLANK、空字符串 EMPTY_STRING、数字 NUMBER、
文本 TEXT、布尔 BOOLEAN、错误 ERROR**。空字符串是文本的一种，与空白
不是一回事；以 `=` 开头的**普通文本**永远按文本保存，不会被执行。

两套容易混淆的语义刻意分开，不可混用：

| 语境 | 空白 | 布尔 | 文本（含空串） |
| --- | --- | --- | --- |
| 直接算术（`+ - * /`、一元负号） | 按 0 | TRUE=1/FALSE=0 | `#VALUE!` |
| `SUM(范围)` 聚合 | 跳过 | 跳过 | 跳过 |
| `SUM(标量表达式)` 标量参数 | 按 0 | 按 1/0 | `#VALUE!` |

其余语义：

- 比较排序遵循 Excel：数字（空白按 0）< 文本（含空串）< 布尔；
  错误参与运算或比较时直接传播。
- `IF` 只求值被选分支：未选分支里的除零等错误不会发生，也不会进入
  依赖列表。两参 `IF` 条件为假且无 else 时返回 `FALSE`。
- `IFERROR` 只捕获公式错误（内部 `FormulaError` 或引用到的错误值），
  不吞工具自身的 Python 缺陷；非错误结果原样返回。
- 除零为 `#DIV/0!`；错误沿依赖链传播，但**独立单元格不受影响**，
  仍可正常求值。

## 共享公式（OOXML 直读，不复用主格公式）

OOXML 中共享公式主格是
`<f t="shared" si="0" ref="D2:D5">B2*C2</f>`，跟随格只有
`<f t="shared" si="0"/>` 而**没有公式文本**。本工具：

1. 按 `si` 索引每个工作表的主格并解析一次；
2. 对跟随格计算相对主格的行列位移 `(dcol, drow)`；
3. 仅平移引用中未被 `$` 锁定的维度，还原跟随格自己的公式
   （如 `D3 -> B3*C3`，而 `$B$1` 保持不动）。

跟随格绝不被当成与主格相同的公式；explain 同时给出主格原文
`formula` 与位移后的 `expanded_formula`。

## 循环与无效引用

求值用显式状态机（未访问/求值中/完成）检测循环，命中时错误码
`#CIRC!`，原因里给出完整引用链（含跨表），可定位到具体单元格；
循环之外的单元格照常求值。引用不存在的工作表给出 `#REF!` 并在原因中
写明表名；位移越界给出 `#UNSUPPORTED!`。

## 结果副本的文件语义

- 用 ZIP 逐部件复制，**非工作表部件字节完全不变**（styles、theme、
  docProps、Content_Types 等），不经过 openpyxl 重新保存，因此不存在
  “重新保存清空缓存/重排部件”的问题。
- 工作表 XML 中：`<f>` 公式节点（含共享主格文本、跟随格空节点、
  `si`/`ref` 属性）、合并区域、维度、单元格坐标与样式 `s` 全部原样；
  只改写公式格的 `<v>` 与 `t`：
  数字去 `t` 写 `<v>`、布尔 `t="b"`、文本 `t="str"`、错误 `t="e"`。
- 公式永远不会被替换成常数。

## 验证（两条独立读取入口）

`verify` / `calculate` 内置的验证同时做：

1. **原始 ZIP/XML（lxml）**：部件清单、非工作表部件字节、公式节点
   逐格、合并区域、样式、坐标、dimension 一致；新缓存与引擎结果逐格
   相等；旧缓存（如 fixture 里的 9999）不残留；源文件 sha256 未变。
2. **openpyxl 第二入口**：`data_only=True` 读出的值与值视图逐格一致；
   普通模式下公式文本仍是公式（非常数）；共享跟随格经 openpyxl
   展开的文本与本工具位移展开一致。

## 目录结构

```
xlcalc/
  errors.py    电子表格错误码与内部 FormulaError
  values.py    六类值模型、比较/算术语义
  lexer.py     手写词法分析（无 eval）
  parser.py    递归下降语法、A1 解析与渲染、共享公式相对位移
  ooxml.py     直读 ZIP/XML，区分共享主格/跟随格、数组/外部链接
  engine.py    依赖图求值、惰性 IF、IFERROR、SUM 双语义、循环定位
  writer.py    只刷新公式格缓存的结果副本写入
  views.py     值视图与 explain 构建
  verify.py    双入口独立验证
  cli.py       calculate / explain / verify
tests/         58 个 pytest 用例（含手算小表、循环、不支持特性、文件完整性）
```

## 运行测试

```sh
python -m pytest tests/ -q
```
