# 小型联动分析台

Python 3.11 + SQLite + 原生网页（无前端框架）。地区 / 类别 / 日期联动筛选交易，
服务端聚合，前端只拿图表结果与当前页。

## 启动

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python run.py            # http://127.0.0.1:8000
```

首次启动会把 `fixtures/transactions.csv` 装入 `.runtime/analyticdesk.sqlite3`
（可用环境变量 `ANALYTICDESK_CSV` / `ANALYTICDESK_COLUMNS` 指向同一 schema 的其他输入）。

## 语义

- **同维度多选取并集，不同维度取交集。**
- **日期左闭右开** `[date_from, date_to)`；`date_from == date_to` 是合法空区间。
- **金额为有符号整数分**，不做浮点换算；明细与指标都直接报告分。
- **NULL 与空字符串是两个值**：CSV 的 `\N` 装入 SQL NULL，`""` 原样保留。
  选择器里分别显示 `(空值 NULL)` 与 `(空字符串)`，SQL 分别用 `IS NULL` / `= ''`。
- **稳定排序 + 唯一记录号裁决**：`business_date, record_id`，翻页不重不漏。
- **两种类别口径**：
  - 类别分布 `category_distribution`：完整筛选（含类别自身筛选）。
  - 类别候选 `category_candidates`：移除类别自身筛选后的候选分布，用于继续添加类别。
  两图都标记 `basis` 字段，点击都作用于同一份筛选状态（分布图点击 = 钻取明细，
  候选图点击 = 增加该类别）。

## 交互

- 多选地区 / 类别、日期区间；「撤销上次筛选」只保留一次；
- 点击类别分布柱 → 钻取该类别的明细，页面出现「返回钻取前筛选」；
- 点击候选柱 → 类别并集增加；点击趋势某天 → 钻取该日明细。

## 并发与失败

- 每次筛选变更有独立 `state_id`，所有请求/响应都带它，面板角标与全局都显示；
- 旧请求不会取消，而是在后端真实完成，再按 `(generation, state_id)` 丢弃，
  旧响应晚到不会覆盖新视图；页面「请求与状态核对」表记录每次请求的结局；
- 后端支持可控延迟 `?delay_ms=`（上限 30s）与部件失败 `?fail=metrics,trend,...`；
  单部件失败显示「未完成 + 重试」，不显示旧数字，其余部件仍带同一 `state_id`；
  `fail=all` 则整页失败、不拼任何数值。

## 核验

```sh
# 独立参考实现：逐行扫描 CSV 重算，1353 项 HTTP 对照
.venv/bin/python tests/verify.py

# 浏览器端到端：真实操作页面（需 Playwright + Chromium）
# UI 核验依赖通过 npm 临时安装，不影响应用运行：
npm install --no-save playwright && npx playwright install chromium
node tests/ui_verify.js
```

UI 测试覆盖多选、NULL/空串、日期边界、两口径、钻取、撤销、快慢乱序、单图失败重试、
整页失败与分页稳定性，并核对面板状态标识与数值。
