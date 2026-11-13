# topic-migrate 使用说明

本地主题分析与“人工名称迁移”工具。它只对本地合成工单训练可解释的 NMF 主题模型，
不调用在线模型，也不依赖外部嵌入、数据库或平台账号。

核心规则：

- **模型编号是局部的。** 每期独立训练词表与预处理统计；local integer id 只在该期
  有意义。
- **业务名称绑定稳定 topic_id。** 名称文件按内容 fingerprint 解析，不按 local id。
  一致地重排模型编号不会移动已确认的业务名称；不一致的改动会被拒绝。
- **候选不等于确认。** compare 只提出候选，并标出多近邻、拆分、合并等竞争关系；
  必须由人工在 decision 模板中逐项选择继续或新建后才可应用。
- **绑定可失效。** decision 绑定两期输入、模型摘要、plan 与旧名称；任一来源变化，
  apply-names 会拒绝旧决定。重训后必须重做 compare 与审阅。
- **弱证据不强归类。** 只有达到主题支持词数（`min_document_evidence`）、且归属分数
  与边际达标的文档才被归属；空正文保留为不可建模状态。
- **输出可审阅。** HTML 只读；decision 可编辑；命名后的主题与逐条证据可导出。

## 安装

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
# 或以可编辑包安装（含 CLI 入口）：
.venv/bin/pip install -e '.[dev]'
```

需要 Python 3.11、`numpy`、`scikit-learn`。

## 入口

```sh
.venv/bin/python -m topic_migrate train       ...
.venv/bin/python -m topic_migrate compare     ...
.venv/bin/python -m topic_migrate apply-names ...
# 安装后也可直接：
.venv/bin/topic-migrate train ...
```

## 1. train 每期

```sh
.venv/bin/python -m topic_migrate train \
  --input fixtures/period_a.jsonl --plan fixtures/plan.json --period a --out out/a
.venv/bin/python -m topic_migrate train \
  --input fixtures/period_b.jsonl --plan fixtures/plan.json --period b --out out/b
```

产物：`model.json`（词表、主题关键词、代表工单、逐条归属分数与原因、原文）、
`names.template.json`（人工命名脚手架）、`summary.json`。

识别稳定业务主题后，分析员编写 `names.json`：保留 `fingerprint`，为每个主题填写
稳定 `topic_id` 与 `name`。示例见 `examples/period_a.names.json`。若编号被重排，
可用 `train --names <names.json>` 验证名称能按 fingerprint 正确解析。

## 2. compare 提出候选

```sh
.venv/bin/python -m topic_migrate compare \
  --old-model out/a/model.json --new-model out/b/model.json \
  --old-names examples/period_a.names.json --out out/compare
```

产物：

- `compare.review.html` — 只读审阅页（含两期绑定摘要、旧名称回查、新期代表工单原文、
  候选分数、竞争关系、未决旧主题）。
- `compare.json` — 机器可读报告；候选分数与证据。
- `decision.template.json` — 可编辑决定模板，`action` 全部留空。

比较空间与分数：

- 词项空间只取两期词表交集，并按各主题在该交集内的质量重新归一；覆盖率对余弦
  打折，避免“词表不相交却相似”。
- 工单证据使用两期都出现且可建模的 `ticket_id`，以各期主题比例的二项
  Bhattacharyya 系数求亲和度，并统计共同归属。
- `composite = 0.5*term + 0.3*document + 0.2*support_jaccard`（可在
  `--config` 覆盖，权重和为 1）。
- 但**候选关系从不计入“确认的主题增减”**。compare 输出中的 `confirmed_changes`
  恒为空，`net_change` 为 null。

## 3. apply-names 应用人工决定

在 `decision.template.json` 中为每个新主题填写：

- `"action": "continue"` + `continue_topic_id`：沿用旧业务 topic_id 与名称；
- 或 `"action": "new"` + 新的 `new_topic_id` + `new_name`。

带竞争标记（拆分/合并/多近邻）的条目还必须写 `note`。旧主题可显式 `retire`；
未表态的旧主题单独列出，不算删除。

```sh
.venv/bin/python -m topic_migrate apply-names \
  --old-model out/a/model.json --new-model out/b/model.json \
  --old-names examples/period_a.names.json \
  --decision examples/decision.example.json \
  --new-input fixtures/period_b.jsonl --out out/applied
```

产物：`named_topics.json`、`evidence.jsonl`（逐条主题/文档/未归属证据）、
`confirmed_changes.json`、`names.next.json`（新期名称文件，可直接作为再下一期的
`--old-names`，实现逐期链式迁移）、`named.review.html`。

用 `--compare-report out/compare/compare.json` 可让 apply 校验决定确实绑定到所审阅的
那份 compare 报告；报告被改动或与决定不一致会被拒绝。

退出码：`0` 成功，`2` 输入/模型错误，`3` 绑定过期（来源变化），
`4` 决定不完整或非法。

## 复现给定样例

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
mkdir -p out
.venv/bin/python -m topic_migrate train --input fixtures/period_a.jsonl \
  --plan fixtures/plan.json --period a --out out/a
.venv/bin/python -m topic_migrate train --input fixtures/period_b.jsonl \
  --plan fixtures/plan.json --period b --out out/b
# examples/period_a.names.json 绑定的是同一 plan/seed 下重训得到的 A 期指纹；
# 若换了输入或参数，请以新 names.template.json 重新确认 fingerprint 后填写。
.venv/bin/python -m topic_migrate compare --old-model out/a/model.json \
  --new-model out/b/model.json --old-names examples/period_a.names.json --out out/compare
# 编辑 out/compare/decision.template.json 后：
.venv/bin/python -m topic_migrate apply-names --old-model out/a/model.json \
  --new-model out/b/model.json --old-names examples/period_a.names.json \
  --decision out/compare/decision.template.json \
  --new-input fixtures/period_b.jsonl --out out/applied
```

样例决策 `examples/decision.example.json` 选择：账户登录、配送物流、账单发票三个
主题延续，物流异常主题作为人工确认的拆出新主题。apply 后确认变化为
`continued=3, added=1, retired=0, net=+1`。

## 补充案例

`examples/supplement/` 下各有 `period_a.jsonl`、`period_b.jsonl`、`plan.json` 与
人工 `period_a.names.json`：

| 目录 | 检验点 |
| --- | --- |
| `topic_number_swap` | 两期局部编号顺序不同；候选仍按内容匹配到正确业务名称。 |
| `repeated_terms` | 两主题共用“退款”等词但语境不同（账单 vs 配送）；不因共词而合并。 |
| `insufficient_evidence` | 单主题词、泛化词、空/纯空白正文；弱证据保持未归属。 |
| `topic_merge` | 新期把两个旧主题合为一个；标出歧义与合并竞争，等待人工决定。 |

```sh
d=examples/supplement/topic_number_swap
.venv/bin/python -m topic_migrate train --input $d/period_a.jsonl --plan $d/plan.json --period a --out out/s-a
.venv/bin/python -m topic_migrate train --input $d/period_b.jsonl --plan $d/plan.json --period b --out out/s-b
.venv/bin/python -m topic_migrate compare --old-model out/s-a/model.json --new-model out/s-b/model.json \
  --old-names $d/period_a.names.json --out out/s-compare
```

同理替换 `repeated_terms`、`insufficient_evidence`。

## 测试

```sh
.venv/bin/pip install pytest
.venv/bin/python -m pytest -q
```

测试覆盖：重复身份冲突、空文本保留、词表按期隔离、弱证据未归属、编号置换不改变
模型摘要与已确认名称、候选按内容而非编号匹配、拆分标记、过期绑定/输入变化被拒绝、
只读 HTML 无脚本、CLI 退出码。

## 设计边界

- 不做漂移工作台、账号体系、数据库版本化或完整平台。
- 不自动完成复杂拆分/合并继承；多候选只列竞争关系并等待人工决定。
- 不以无监督模型逐项复原人工标签为目标；候选质量以少量人工审阅为准。
