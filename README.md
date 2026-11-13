# 本地 JSONL 契约验证器

仅使用 Python 3.11 标准库实现指定的 JSON Schema 子集。不依赖任何第三方验证库，
不访问文件或网络引用。所有契约与数据均为本地合成。

```
run.py                命令行入口
contract_validator/
  pointer.py          RFC 6901 JSON Pointer（~0 / ~1 转义）
  report_json.py      单行读取、JSON 解析、规范 JSON、拒绝报告
  contract_check.py   契约静态检查 / 编译（check-schema）
  instance_check.py   逐条数据验证（validate）
  baseline.py         独立有限展开基准（仅 verify 使用，不共享验证逻辑）
  generate.py         本地合成契约与 JSONL 语料
  verify.py           端到端一致性 / 边界自检（verify）
  cli.py              子命令与原子发布
tests/test_validator.py  标准库单元测试
```

## 快速开始

```sh
python3 run.py check-schema path/to/schema.json
python3 run.py validate path/to/schema.json path/to/data.jsonl path/to/output.jsonl --summary path/to/summary.json
python3 run.py generate --out-dir .runtime/corpus
python3 run.py verify --out-dir .runtime/verify
```

也可直接以模块运行：`python3 -m contract_validator check-schema ...`。

退出码：

| 命令 | 0 | 1 | 2 |
|------|---|---|---|
| `check-schema` | 契约合法 | — | 契约错误 |
| `validate` | 全部通过 | 有行被拒绝 | 契约错误或输入不可读 |
| `generate` | 成功 | — | — |
| `verify` | 全部自检通过 | 有失败 | — |

## 支持的契约子集

每个 schema 节点必须是 JSON 对象。`type` 在普通节点上**必填**，取值仅限
`object` / `array` / `string` / `integer` / `boolean` / `null`。

| 关键字 | 说明 |
|--------|------|
| `type` | 六种类型之一，必填（`$ref` 节点与 `oneOf` 节点除外） |
| `properties` | 对象：属性名 → 子契约 |
| `required` | 对象：字符串数组；**只检查键是否存在**，存在则值仍需通过对应子契约 |
| `additionalProperties` | 对象：JSON 布尔值。`false` 时未在 `properties` 中声明的键被拒绝 |
| `items` | 数组：单个子契约，作用于每个元素；省略时元素形状不受约束 |
| `oneOf` | 非空分支数组；恰好一个分支成功才算通过 |
| `$defs` | **仅根节点**允许；`oneOf` 根节点与 `$ref` 根节点亦可另有 `$defs` |
| `$ref` | 仅限本地 `#/$defs/<名称>`（单个 JSON Pointer 记号，支持 `~0`/`~1`） |
| `title`, `description` | 允许出现在 `type` 节点与 `$ref` 节点上，忽略 |

约束与组合规则（在读取任何数据前静态检查）：

* `$ref` 节点除 `title` / `description` 外不得有其它关键字；因此
  `{"$ref": ..., "type": ...}` 与 `{"$ref": ..., "oneOf": ...}` 均为契约错误
  （未知关键字或非法组合）。根级 `$ref` 节点允许另带 `$defs`。
* `oneOf` 节点只包含分支数组（根级可另带 `$defs`）。`oneOf` 之外出现 `type`、
  `properties`、`title` 等任何关键字都按**未知关键字**拒绝。
* `properties` / `required` / `additionalProperties` 只能出现在
  `type: object` 节点；`items` 只能出现在 `type: array` 节点，否则为
  `invalid-combination`。`additionalProperties` 必须是布尔值（对象形式不支持）。
  `type: array` 可省略 `items`，此时只校验数组类型本身。
* `type` 缺失或不支持、`properties` 非对象、`required` 非字符串数组或含重复名、
  `items` 非对象、`oneOf` 非数组或空数组，均为契约错误。
* 未知关键字一律拒绝，不会当作普通数据违规。

### 引用与递归

`$ref` 目标为 `#/$defs/<名称>`，其中 `<名称>` 是单个 JSON Pointer 记号：
名称中的 `~` 写 `~0`、`/` 写 `~1`。形如 `#/$defs/a/b`、`#/definitions/x` 或
任何带 scheme 的引用都被拒绝（`invalid-ref`），目标不存在为 `unresolved-ref`。

递归只有经 `properties` 或 `items` **下降一层数据**后才允许回到原节点
（例如树的 `children`）。仅由 `$ref` 与 `oneOf` 边构成、不消耗数据层级就回到
原节点的循环，在契约预检阶段即被拒绝，错误码 `zero-progress-cycle`，并给出
引用路径，例如：

```
/$defs/loop -[$ref #/$defs/loop]-> /$defs/loop
/$defs/a -[$ref #/$defs/b]-> /$defs/b -[oneOf[0]]-> /$defs/b/oneOf/0 -[$ref #/$defs/a]-> /$defs/a
```

### 规模上限

| 上限 | 值 | 超出行为 |
|------|----|----------|
| 契约图节点数 | 200 | `check-schema` 报 `too-many-nodes`（契约错误） |
| 契约 JSON 嵌套 | 1000 | `contract-too-deep`（契约错误） |
| 单行字节数（不含 `\n`） | 65536（64 KiB） | 该行 `line-too-long`，其余行继续 |
| 数据 JSON 嵌套深度 | 64 | 该行 `too-deep`，其余行继续 |

深度按 `{}`/`[]` 的最外层计为第 1 层（标量为 0 层）；字符串内的括号不计数。
深度在 `json.loads` 之前用字符串感知扫描判定，因此解释器递归保护不会被触发。
数据验证本身使用显式工作栈，不依赖 Python 递归深度。

## 验证语义

* `integer` 只接受真正的 JSON 整数：布尔值 `true`/`false`（Python 中 `bool` 是
  `int` 子类）被拒绝；小数（含 `1.0`）、非有限数字（`NaN`/`Infinity`/
  `-Infinity`，在解析入口即 `invalid-json`）也被拒绝。
* 对象中**未出现的属性**与**显式 `null`** 不同：`required` 只检查键存在，键存在
  时值仍须通过对应子契约（若有）。缺失属性错误的数据指针指向**该属性位置**。
* `additionalProperties: false`：每个未声明键报 `additional-property-forbidden`；
  仍会先检查 `required`，并按码点序遍历已声明属性。
* `oneOf`：**必须恰好一个分支成功**。
  * 零个成功：失败，报告该 `oneOf` 节点错误，并给出**每个分支一个代表错误**。
  * 多个成功：失败，记录全部成功分支编号（`successBranches`）。
  * 不会在遇到第一个成功分支时提前停止；所有分支都会被求值。
  * 分支代表错误先按数据路径深度（根为 0），再按数据指针码点序，最后按契约
    指针码点序选择。
* 相同（契约节点，数据节点）对在一次逐条验证中只计算一次并复用结果（memo），
  避免 `oneOf` 分支组合导致的指数重复。每条输入行使用独立 memo 表（不同行不共享
  结果），`validate` 汇总中的 `pairsChecked` / `memoHits` / `memoMisses` 是**全部行
  的累计**。`--no-cache` 仅用于对比，不改变结论。

## 输入与输出

输入按物理行处理，只按 `\n` 切分：末尾换行不产生幻影行，末行无换行也计入。
每行独立判定，任何一行失败都不影响后续行。

* 通过行：输出**按键排序的规范 JSON**（`ensure_ascii=True`、紧凑分隔符、
  无 NaN），对象字段顺序不影响输出。
* 失败行：输出失败报告对象，包含
  * `line`：原文件物理行号（从 1 起）；
  * `code`：稳定错误码；
  * `dataPointer` / `schemaPointer`：RFC 6901 指针（含 `/` 与 `~` 的名称分别
    转义为 `~1` / `~0`；根为空串 `""`）；
  * `message`；
  * `errors`：按上述代表错误规则排序的全部错误；
  * `successBranches`：多成功时列出；
  * 行级拒绝（重复键、非法 JSON、超长、过深、非 UTF-8）只带行级字段。
* 行级拒绝码：`duplicate-key`、`invalid-json`、`line-too-long`、`too-deep`。
* 数据错误码：`type-mismatch`、`required-property-missing`、
  `additional-property-forbidden`、`oneof-no-match`、`oneof-multiple-success`。
* 汇总（`--summary` 路径或 stderr）：`inputLines`、`accepted`、`rejected`、
  `pairsChecked`、`memoHits`、`memoMisses`、`useCache`。始终满足
  **输入行数 = 通过数 + 拒绝数**。

### 输出发布是原子的

报告先写入目标目录内的临时文件，`fsync` 后 `os.replace` 原子替换。写入失败
（权限、磁盘满等）不会替换旧版本，旧文件保持完整。契约错误在打开输入文件之前
就被报告，因此契约错误不依赖输入是否存在。

## 生成语料

`generate` 在 `--out-dir` 下生成确定性语料：

* `recursive-tree/`：递归二叉树，叶子通过合法 `oneOf` 终止；含子节点两分支都不匹配的拒绝。
* `required-null/`：`required` 与显式 `null`；缺失与 `null` 区分。
* `integer-not-boolean/`：布尔、`1.0`、`1.5` 拒绝。
* `oneof-two-success/`：两分支同时成立被拒绝，恰好一个通过。
* `strict-and-open-objects/`、`open-object/`：`additionalProperties` 布尔两态。
* `pointer-escaping/`：名称 `a/b`、`a~b` 的诊断转义。
* `depth-limit/`：63 与 64 层通过、65 层拒绝。
* `ingress-errors/`：重复键与非法 JSON 逐行拒绝。
* `rejected-schemas/`：直接循环、间接循环、`oneOf` 循环、未知关键字、
  非法组合、`$ref` 带约束、未解析 `$ref`、超过 200 节点；`check-schema` 必须拒绝。

每个场景包含 `schema.json`、`data.jsonl`、`meta.json`（期望的行级结论）。

## 自检（verify）

`verify` 覆盖：递归树、合法终止分支、直接与间接无进展循环、两分支同时成立、
`required` 配合 `null`、布尔冒充整数、未知字段、指针转义、深度与行长上限、
物理行边界、重复键/非法 JSON、字段顺序不变性、契约先于数据、原子发布、
节点上限。还使用 `baseline.py`（独立有限展开、不共享验证逻辑、不共享 memo）对
小数据交叉核对通过与拒绝结论，并对比有/无 memo 的 (节点, 数据) 计算次数以证明
结果复用。

```sh
python3 run.py verify --out-dir .runtime/verify
# {"cases":29,"failed":0,"failures":[],"ok":true,"passed":29, "metrics": {...}}
```

`verify` 的 `metrics` 字段记录 `memo-reuse` 用例中 (契约节点, 数据节点) 对的实际
计算次数对比，例如：

```
tree:         cached=36  uncached=36
3-branch:     cached=12  uncached=16  hits=2
diamond(8):   cached=175 uncached=637 hits=16
```

说明相同节点对只计算一次、后续命中 memo。

另有标准库单元测试：

```sh
python3 -m unittest discover -s tests -v
```

`verify` 与单元测试只读取 `.runtime/` 下的本地文件，不进行任何网络访问。

## 已知取舍

* 本子集无 `enum` / `const` / 数值范围等关键字；出现即按未知关键字拒绝。
* `oneOf` 分支内同样按同一子集静态检查，未知关键字同样被拒绝。
* 输出使用 ASCII 转义：确定、跨 locale 稳定，但不原样保留非 ASCII 字符。
* 验证结果按行独立；一行的拒绝不会阻止后续行。
