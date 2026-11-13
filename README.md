# sql3v — 有限域 SQL 三值谓词核验 CLI

报表条件改写后文本看似相同，却可能因为 NULL 导致结果变化。本工具在
**不连接业务库**的前提下，对两个 SQL 布尔谓词做有限域上的三值逻辑核验，
并用本地内存 SQLite（标准库 `sqlite3`）逐赋值交叉核对。仅依赖 Python 3.11
标准库，无需安装第三方包、无需网络。

## 1. 它回答什么

对每个谓词分别给出有限域内 `TRUE / FALSE / UNKNOWN` 的赋值计数与 WHERE 收录
行数，并分别判定：

- **值等价**：每个赋值下两边的三值结果完全相同；
- **筛选等价**：每个赋值下两边“是否为 TRUE”相同（FALSE 与 UNKNOWN 都被过滤）。

不等价时给出按规范顺序（列名字典序；每列 NULL 在前、整数升序）的**最小反例
赋值向量**、两边最终逻辑值和关键子表达式结果。两种等价可以结论不同、反例
不同。结论只针对给定有限域，不声称证明全整数域等价。

## 2. 声明文件

```json
{
  "columns": [
    {"name": "x", "values": [null, 1, -2]},
    {"name": "y", "values": [1, 2, null]}
  ],
  "predicate_a": "NOT (x = 1 OR y = 2)",
  "predicate_b": "x <> 1 AND y <> 2"
}
```

约束：1–6 列；列名为未加引号普通标识符、区分大小写、不可重复；每列 1–5 个
允许值，值只能是有符号 64 位整数或显式 `null`，同列不得重复；总赋值组合数
不超过 20000（6 列 × 每列 5 值上限为 15625，正常声明不会触顶，超限仍拒绝）。
语法细节见 [docs/syntax.md](docs/syntax.md)。

## 3. 四个子命令

```bash
# 词法/语法分析、AST 与受控 SQLite 表达式（不执行任何 SQL）
python3 -m sql3v generate examples/demorgan.json

# 枚举有限域，输出计数、两类等价结论与规范反例（加 --sqlite 同步交叉核对）
python3 -m sql3v compare examples/not_in_null.json
python3 -m sql3v compare examples/different_counterexamples.json --sqlite

# 对单个赋值逐步解释三值求值（未给的列取规范域首值）
python3 -m sql3v explain examples/not_in_null.json --assign x=2

# 全量 SQLite 交叉核对 + 手工 AND/OR/NOT 真值表（NOT 3 组、AND/OR 各 9 组）
python3 -m sql3v verify examples/demorgan.json
```

所有命令支持 `--json` 输出机器可读结果；声明文件路径传 `-` 表示从标准输入
读取。输入不合法（非法字符、缺操作数、未知列、不支持语法、域约束违反等）
一律带行/列定位报错并以退出码 2 结束。

## 4. 快速演示（NOT IN 含 NULL 的真实行为）

```bash
python3 -m sql3v compare examples/not_in_null.json
```

`x NOT IN (1, NULL)` 与 `x NOT IN (1, 2) AND x IS NOT NULL` 在域
`{NULL,1,2}` 上**筛选等价**（WHERE 都收录 0 行）但**值不等价**：`x=NULL`
时前者是 UNKNOWN、后者是 FALSE。只比较最终 WHERE 行数会把这种差异掩盖掉。

## 5. 安全模型（为什么不会把用户文本交给数据库）

1. 用户谓词只进入自建词法分析器与递归下降解析器，产出带位置信息的 AST；
2. SQLite 表达式由 AST 节点拼出固定结构，运算符/关键字均为程序内常量；
3. 执行期查询把列引用和字面量全部渲染为 `?` 占位符，值通过 `sqlite3`
   绑定参数传入，SQL 文本与任何用户值无关；展示用模板里的列名来自已校验
   声明并做双引号定界转义；
4. 每个赋值打开一个独立 `:memory:` 库，只执行
   `SELECT <expr>` 与 `SELECT 1 WHERE <expr>` 两条语句，不建表、不执行
   DDL/PRAGMA/其他任何 SQL；分号、多语句、子查询在词法/语法阶段即被拒绝。

## 6. 运行测试

```bash
python3 -m unittest discover -s tests -v
```

71 个测试覆盖：AND/OR/NOT 全组合手工真值表、词法错误定位、int64 边界、
200 token 上限、解析优先级与结合性、De Morgan、NOT IN 含 NULL、`x=x`、
NULL 自比较、IS NULL、双重否定、一假一未知只在值层面不同、规范反例不受
输入顺序影响、占位符/参数绑定顺序、CLI 端到端与非法输入退出码。

## 7. 目录结构

```
sql3v/            主包（词法/语法/AST/求值/代码生成/SQLite 核对/CLI）
tests/            unittest 测试套件
examples/         合成样例（含 4 个故意非法的 invalid_*.json）
docs/syntax.md    语法、优先级、三值真值表与 IN/NOT IN 语义说明
environment/      初始工作区自带的环境说明（与本工具运行无关）
```

## 8. 运行环境

Linux + Python 3.11（开发机为 3.11.2，SQLite 3.40.1）。本工具只用标准库，
不需要 PostgreSQL 或任何外部服务；所有临时核对都在进程内的独立内存库完成。
