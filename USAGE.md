# Avro 消费者升级：schema 级兼容性预检与投影

在升级 Avro 消费者之前，先在 **schema 级别**证明新 reader 能接收生产者允许写入的
**全部**记录（包括样例数据里从未出现的 union 分支），预检通过后才把 Avro 消息
投影成 reader 字段顺序的 JSONL。不接队列、不接 Schema Registry，二进制解码委托给
`fastavro`，兼容性判断全部由本包自行实现。

## 安装

```sh
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## 命令

### check —— 只做兼容性预检

```sh
.venv/bin/python -m avro_compat check fixtures/writer.avsc fixtures/reader-ok.avsc \
    --plan plan.json
```

退出码：`0` 兼容；`1` 模式不兼容（逐项打印双方 schema 路径与原因）；
`2` schema 自身非法或落在支持子集之外。

### project —— 预检通过后投影

```sh
.venv/bin/python -m avro_compat project \
    fixtures/writer.avsc fixtures/reader-ok.avsc fixtures/messages.avro \
    --out out.jsonl --plan plan.json
```

- 预检不通过（退出码 `1`）时绝不解码数据、绝不创建输出文件；
- 解码/投影失败（退出码 `3`）时不留下残缺或“宣称完成”的结果，写出采用临时文件 +
  原子替换；
- `out.jsonl` 每行一个信封：`{"index": 原记录序号, "record": {...reader 字段顺序...}}`；
- `plan.json` 是与数据无关的解析计划，逐字段说明来源（writer 路径 / alias 改名 /
  default）、union 各分支计划与数值提升（`int→long`、`int→double`、`long→double`）。

## 支持与拒绝的 Avro 特性

- **支持**：`record`（含内联命名 record、namespace 继承、record/field alias）、
  `array`、`null`、`boolean`、`int`、`long`、`double`、`string`，以及**恰好由两种
  不同支持类型组成**的 union。
- **拒绝**：递归/前向引用、`enum`、`map`、`fixed`、`logicalType`、字段 `order` 等；
  非两分支或分支类型重复的 union。
- 读者可丢弃生产者字段；用 field alias 接收旧字段名；reader 新字段必须带合法
  default，default 按声明类型校验（union 字段按 Avro 规则取首分支）。
- 数值只允许 Avro 规定的提升，禁止 Python 式隐式转换（`string→int` 直接判不兼容）。

## 关键语义

- **全 union 覆盖**：writer union 的每个分支都必须能被 reader 接收，与样例数据无关。
  `fixtures/reader-bad.avsc` 把 `contact` 从 `["null","string"]` 改成
  `["null","int"]`，而 `messages.avro` 里两条记录的 `contact` 都是 `null`——
  fastavro 读样例不会报错，但 `check` 仍判失败，因为 `string` 分支没有接收方。
- **null ≠ 缺字段**：writer 显式写出的 `null` 走 union 的 null 分支；reader 新字段
  （writer 无对应字段）才使用声明 default，二者不会互相顶替。
- **可变 default 隔离**：数组/record 类型的 default 在每条记录投影时独立深拷贝，
  记录之间不共享可变对象，计划本身也不会被投影过程污染。
- **源记录不可变**：投影只构造新对象，不修改 fastavro 解码出的源记录。

## 测试

```sh
.venv/bin/python -m unittest discover -s tests -v
```

覆盖：子集校验与拒绝项、合法升级、alias 改名与 alias 冲突、数值提升与隐式转换拦截、
缺 default、union 全分支覆盖（含未出现分支）、null/缺字段区分、可变 default 隔离、
源记录不可变、解码失败无残留输出，以及与 fastavro 的交叉核对。
