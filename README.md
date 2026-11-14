# 单一冻结拟合工件：离线批处理与在线 HTTP 同契约

数据团队的离线批处理（NumPy 列式）与在线请求（标准库 HTTP 标量执行）加载
**同一个纯 JSON 工件**。拟合只发生一次；转换路径不重估统计、不扩词表，进程
重启或训练行乱序都不改变输出列身份。

- Python 3.11，仅依赖 `numpy`（HTTP 服务只用标准库，不引入 numpy）
- 工件是普通 JSON 数据：无 pickle、无可执行代码，含 sha256 摘要
- 两条转换路径**各自独立实现**，共享的只有工件与校验契约（`contract.py`）；
  batch 不是"循环请求 HTTP"，也不调用任何单记录函数

## 目录

```
src/featurizer/
  contract.py   输入校验规则、保留码、错误类型（无转换逻辑，两路径共用）
  artifact.py   工件 JSON 结构、sha256 摘要、启动/加载全量校验
  fit.py        只读训练数据，估计统计并冻结工件（纯 Python，无 numpy）
  batch.py      NumPy 列式批转换（先整表校验，再向量化）
  serve.py      ThreadingHTTPServer 标量转换（无 numpy，独立实现）
scripts/
  make_test_data.py  生成 .runtime/ 下的补充测试数据（全缺失列、乱序、漂移）
  verify.py          33 项自动核验（真实子进程 + 独立手算 oracle）
  run_verify.sh      一键核验
artifacts/featurizer.json  由 fixtures/train.jsonl 拟合的冻结工件
examples/                  真实服务的请求/响应样例
```

## 快速开始

```sh
python3 -m venv .venv
.venv/bin/pip install -e .          # 或 pip install -r requirements.txt

# 1) 拟合（只读训练数据）
.venv/bin/python -m featurizer.fit \
  --train fixtures/train.jsonl --spec fixtures/spec.json \
  --out artifacts/featurizer.json

# 2) 离线批处理（NumPy 列式；任一记录非法则整批拒绝、不写输出文件）
.venv/bin/python -m featurizer.batch \
  --artifact artifacts/featurizer.json \
  --input fixtures/batch.jsonl --output out.jsonl

# 3) 在线服务（工件先校验后监听；损坏工件直接非零退出，不开放端口）
.venv/bin/python -m featurizer.serve \
  --artifact artifacts/featurizer.json --host 127.0.0.1 --port 8000

# 一键自动核验（真实启动服务进程，覆盖整批/分批/逐条/并发/错误/损坏工件）
./scripts/run_verify.sh
```

## 输入契约（spec.json）

两个数值字段 `amount`、`constant`，一个单值类别 `channel`，记录主键 `record_id`。

- 数值：JSON number 或 `null`。**布尔值拒绝**（不会悄悄变成 0/1）；
  `NaN`/`Infinity` 等非有限值按字段路径拒绝；字符串数字不隐式转换。
- 类别：字符串或 `null`，按**精确字符串**匹配稳定词表。
- 多余字段：两路径统一 `reject`（400 / 批退出码 2），带字段路径。

## 拟合策略（可复算，全部写进工件）

数值统计只用训练非缺失值，标准差为总体标准差（ddof=0）：

| 情形 | 策略 |
|---|---|
| 普通列 | 均值/标准差由非缺失值估计；缺失填训练均值，`*_missing=1`，再 z-score |
| 常数列（std=0） | 不做除法，`*_scaled` 恒为 0；缺失仍由指示列表达 |
| 整列缺失 | mean=0、std=0；所有值填 0、scaled=0、指示列恒 1 |

类别词表 = 训练非空字符串的**排序集合**，因此与进程、行顺序无关。保留整数码：

- `0` = null（保留码 `code_null`）
- `1` = 未见类别（保留码 `code_unknown`）
- `>= 2` = 真实词表项（偏移 `vocab_index_offset=2`）

真实字符串 `"__UNKNOWN__"` 是普通词表项（本数据中码为 3），与保留码 1 不会
混淆；`null`、`""`、未见类别各自可区分。输出另含 `channel_missing`、
`channel_known` 指示列。

### 手算核对（fixtures/train.jsonl，不依赖两个实现互证）

`amount` 非缺失值 `[1,3,5,7,9]`：

```
mean = (1+3+5+7+9)/5 = 5
var  = ((1-5)²+(3-5)²+(5-5)²+(7-5)²+(9-5)²)/5 = 40/5 = 8
std  = √8 ≈ 2.8284271247461903
```

`constant` 非缺失值全为 5 → mean=5、std=0（常数列策略）。
`verify.py` 的 M1 用上述手算结果直接比对工件；M4 的转换期望值由脚本内独立
oracle 重新手算，batch 与 HTTP 都向 oracle 对齐，而非互相对齐。

## 输出列布局（冻结在工件内，顺序固定）

```
amount_scaled float64 | amount_missing int8
constant_scaled float64 | constant_missing int8
channel_missing int8 | channel_known int8 | channel_code int64
```

工件还保存 `column_order_signature`（列名序列的 sha256 前 16 位）与每个列的
dtype；服务启动时重新校验布局与有限参数。

## HTTP 接口

### `GET /health`

```json
{"status": "ready",
 "artifact_digest": "8800d129…",
 "columns": ["amount_scaled", "amount_missing", "..."]}
```

### `POST /transform`（单记录）

请求：

```json
{"record_id": "r3", "amount": 12, "constant": 5, "channel": "partner"}
```

响应（含工件摘要、固定列名、record_id、数值）：

```json
{"record_id": "r3",
 "artifact_digest": "8800d129…",
 "columns": ["amount_scaled", "..."],
 "features": {"amount_scaled": 2.4748737341529163, "amount_missing": 0,
              "constant_scaled": 0.0, "constant_missing": 0,
              "channel_missing": 0, "channel_known": 0, "channel_code": 1}}
```

错误响应（显式状态码 + 字段路径）：

```json
{"error": {"status": 400, "path": "amount",
           "message": "boolean is not a valid numeric value"}}
```

| 场景 | 状态码 |
|---|---|
| 布尔数值 / 错误类型 / 非有限数 / 缺字段 / 多余字段 / 坏 record_id | 400，`path` 定位字段 |
| body 非 JSON / 不是 JSON 对象 | 400，`path=""` |
| GET /transform | 405；未知路径 | 404；body 过大 | 413；缺 Content-Length | 411 |

## 隔离与冻结保证

- 服务启动先 `load_artifact()` 全量校验（结构、有限数、dtype、列布局、
  digest），失败即退出码 1，**在 bind 端口之前**；正常启动后只持有一个冻结
  工件，进程生命周期内不重新加载、不接受热更新。
- 每个请求构造全新的局部 `features` dict；词表 dict 与统计值只读，无共享
  可变缓冲。`ThreadingHTTPServer` 下并发请求不会串值（M4.4 用 Barrier 同时
  发起 6 个不同载荷，另有 20 线程抽查）。
- 错误请求不改变任何状态；错误风暴后合法请求仍成功（M5.13）。
- 批处理先整表校验后分配特征缓冲；任何一行非法则整批失败、无输出文件
  （M5.11/M5.12）。
- 处理分布漂移数据后工件字节不变、同一记录特征不变（M8.1）；作为对照，
  真正用漂移数据重新拟合会得到不同 digest（M8.2），证明校验有区分力。

## 自动核验矩阵（scripts/verify.py，33/33）

| 组 | 覆盖内容 |
|---|---|
| M0–M1 | 拟合成功；均值/std/计数与手算一致；词表排序与保留码 |
| M2 | 乱序训练行拟合出**相同 digest 与列布局** |
| M3 | 工件重载校验；篡改统计触发 digest 不一致 |
| M4 | 整批、分批拼接、逐条 HTTP、Barrier 并发 HTTP 全部等于独立手算 oracle |
| M5 | 布尔/字符串/非有限/缺字段/多余字段/坏 id/坏类别/坏 JSON/非对象 body 的状态码与字段路径；批拒绝不落盘；错误后合法请求成功 |
| M6 | null、未知类别、真实 `"__UNKNOWN__"`、`""` 四种码互不混淆 |
| M7 | 整列缺失列在两路径均为 fill 0 / scaled 0 / 指示恒 1 |
| M8 | 漂移批处理不更新工件（字节与同记录特征不变），refit 对照 digest 变化 |
| M9 | 损坏 JSON 与被篡改工件：服务拒绝启动且端口不开放，batch 同样拒绝 |

测试产物全部位于 git-ignored 的 `.runtime/`；`fixtures/` 保持原样。
无热更新、无缓存、无 Arrow、无压测组件。
