# extract — 文本型 PDF 三列表提取

从文本型 PDF 中提取固定三列语义的表格（**日期 / 说明 / 金额**），输出 CSV 与来源 JSONL。
只依赖字符/词块的几何坐标，不预置每页坐标，不做 OCR。

## 命令

```sh
python3 extract.py fixtures/report.pdf --csv rows.csv --jsonl rows.jsonl
```

- `pdf`：输入 PDF（文本型）。`--csv` / `--jsonl` 省略时默认 `<输入名>.csv` / `<输入名>.jsonl`。
- 退出码恒为 0；待复核项不阻断输出，摘要与明细打印到 stderr。
- 依赖：Python 3.11 + PyMuPDF（见 `requirements.txt`）。
- 测试：`python3 -m unittest discover -s tests`（16 个用例）。

## 输出格式

### CSV

`row_id,date,description,amount` 每行一条明细。`amount` 为原文（如 `-15.00`）；
金额原文缺失或解析失败时留空——不静默猜值，原因见 JSONL 中对应 review 记录。

### JSONL（来源）

每行一条记录，`type` 区分：

**`row`**：一条明细。每个单元格给出 `status`、`text` 和 `fragments`：

```json
{"type":"row","row_id":3,"date":"2026-01-03",
 "description":"Equipment maintenance and replacement components","amount":"210.25",
 "cells":{"date":{"status":"ok","text":"2026-01-03","fragments":[{"page":1,"text":"2026-01-03","bbox":[40.0,730.17,96.25,745.29]}]},
          "description":{"status":"ok","text":"...","fragments":[{"page":1,"text":"Equipment","bbox":[...]},{"page":2,"text":"replacement","bbox":[...]}]},
          "amount":{"status":"ok","text":"210.25","fragments":[{"page":2,"text":"210.25","bbox":[455.0,100.18,488.64,115.29]}]}}}
```

- `fragments`：页码（1 基）+ 原文 + 该页内 bbox。**跨页单元格保留各页片段**（上例说明列
  第 1、2 页各一段，各有独立 bbox），绝不以一个跨页矩形代替。
- `status` 区分**原文缺失**与**提取失败**：
  - `ok`：有原文且（金额列）解析通过；
  - `absent`：该列区域无原文（如金额缺失）——属原文缺失；
  - `unparsed`：有原文但不是合法的两位小数金额——属提取/解析失败，原文保留在 `text`/`fragments` 待查。
- 说明列独立聚合，其中的日期、编号、金额样数字原样保留，不因金额解析而截断。

**`review`**：待复核项。`issue` 取值：

| issue | 含义 |
|---|---|
| `amount_missing` | 明细封闭时金额列无原文（含页末未续接的情形），CSV 留空 |
| `amount_unparsed` | 金额原文非法（符号异常、非两位小数、多个候选值），未猜值 |
| `amount_conflict` | 续行带金额而上一条已有金额，未合并，候选归属见 `fragments` |
| `orphan_continuation` | 续行型内容无可归属的未完结明细（含未重复表头的页面），`candidates` 给出候选行 |
| `ambiguous_column` | 词心距列边界 ≤3pt，两个列归属皆可行，未静默归列 |
| `row_not_recognized` | 表格内一行日期列非合法 ISO 日期且含其他列内容，未作为明细 |
| `continuation_rejected` | 续行型内容与上一行垂直间距超容差，表格结束 |

## 行聚合与列归属的容差

所有坐标来自 `page.get_text("words")`，**先几何排序再处理**，不把 PDF 内容流的
存储顺序当阅读顺序（`make_pdf.py --shuffle` 变体输出逐字节一致，有测试守护）。

- **行聚合**：词按行心 y 聚类，同一条视觉行的词心 y 差 ≤ `max(2pt, 0.35 × 中位词高)`；
  行内按 x 排序。11pt 样例中容差约 5.3pt，远小于 16pt 的行距。
- **列归属**：列边界不预置，取每页表头行 `Date`/`Description`/`Amount` 三词左边缘
  两两中点；词按**横向中心**落入的区间归列。词心距边界 ≤3pt 为歧义区，转
  `ambiguous_column` 待复核，不猜。
- **新行**：只能由日期列中**单个且日历合法**的 ISO 日期（`YYYY-MM-DD`）开启；
  说明列里的日期、数字不产生新行，金额只取自金额列。
- **续行（同页）**：日期列为空、说明/金额列有内容、且与上一正文行行心距
  ≤ `2.5 × 中位词高`（样例 16pt 续行距通过，30pt 行距不误并）。超距则表格结束并留痕。
- **表格结束**：日期列出现非日期内容（页脚 `Page N`、`Table ends`、表后说明同此路径），
  或续行超距。表头行之上、表格结束之后的内容一律忽略——表后说明里的日期与
  金额（如 `999.99`）不会进入结果。

## 跨页续接条件

页末未封闭的明细（携带行）与**新页布局**结合判断，同时满足才续接：

1. 新页重复出现三列表头，且三个列锚点与上页布局的漂移均 ≤ 8pt；
2. 新页首个正文行为续行型（日期列为空）；
3. 金额不冲突：携带行缺金额、续行可补金额；双方都有金额 → `amount_conflict`，不合并。

证据不足时不无条件拼给上一行：新页无重复表头 → 携带行封闭（缺金额则
`amount_missing`），疑似续行以 `orphan_continuation` 留痕并给出候选归属；
表头锚点漂移超容差 → 放弃续接并封闭。

## 支持边界

- 仅文本型 PDF；**不做 OCR**，扫描页/图片页无文字层时整页跳过（无输出、无诊断）。
- 仅固定三列语义（Date/Description/Amount 表头）；**不做任意表格识别**，
  不处理合并单元格、多表头层级、无表头表格。
- 日期仅支持日历合法的 ISO `YYYY-MM-DD`；金额仅支持 `-?` 可选负号的两位小数
  （如 `120.50`、`-15.00`）。千分位、括号负数、货币符号等一律 `amount_unparsed` 待复核。
- 跨页续接以下一页重复表头为前提；无表头续页不猜。
- 表头之后的页面其余内容（页眉、页码、表后说明）按几何规则忽略，不做版式分类。

## 验证

`tests/test_extract.py` 覆盖：重复表头不进入数据、说明换行聚合、跨页续接
（片段分页留痕）、表外数字排除、**写入顺序打乱结果逐字节一致**、金额缺失/非法/
冲突/孤儿续行/非法日期等诊断，以及**坐标回查**——抽样单元格按 JSONL 中的 bbox
回到原页重新取词，与片段原文逐一比对。
