# 谓词语法说明（受控 SQL 布尔表达式子集）

本工具只接受 SQL 布尔表达式的一个**受控子集**，不支持多语句、子查询、算术、
函数或隐式类型转换。用户文本先经词法/语法分析生成 AST，再由 AST 生成受控
SQLite 表达式；用户原文永远不会被拼接进数据库执行的 SQL 字符串。

## 1. 词法单元

| 类别 | 形式 | 说明 |
| --- | --- | --- |
| 整数 | `0`、`-7`、`9223372036854775807` | 有符号 64 位整数；负号必须与数字紧贴才属于字面量 |
| NULL | `NULL`（关键字大小写不敏感） | 显式空值字面量 |
| 列引用 | `x`、`col_1`、`_tmp` | 未加引号的普通标识符，**区分大小写** |
| 比较 | `=` `<>` `<` `<=` `>` `>=` | 不支持 `==`、`!<` 等方言 |
| 空值判断 | `IS NULL`、`IS NOT NULL` | |
| 列表 | `IN (...)`、`NOT IN (...)` | 列表至少 1 个元素，元素只能是整数或 NULL |
| 逻辑 | `AND`、`OR`、`NOT` | 关键字大小写不敏感 |
| 括号 | `(` `)` | 改变优先级 |
| 分隔 | `,` | IN 列表元素分隔 |

每个谓词最多 **200 个词法单元**（EOF 不计）。非法字符（`+ - * / %` 算术符、
引号、方括号、分号、小数点、`$` 等）在词法阶段按“行:列”定位拒绝。

## 2. 语法（优先级从低到高）

```
or_expr   := and_expr ( OR and_expr )*
and_expr  := not_expr ( AND not_expr )*
not_expr  := NOT not_expr | predicate
predicate := atom ( ( '=' | '<>' | '<' | '<=' | '>' | '>=' ) atom
                   | IS [ NOT ] NULL
                   | [ NOT ] IN '(' list_element ( ',' list_element )* ')' )?
atom      := INT | NULL | IDENT | '(' or_expr ')'
list_element := INT | NULL
```

- `OR` 优先级最低，`AND` 次之，`NOT` 再次之，比较/IS NULL/IN 是最内层谓词；
- `AND`、`OR` 左结合，`NOT` 为右结合前缀；
- 括号内必须是完整布尔表达式；比较操作数只能是列/整数/NULL，不能是布尔表达式
  （因此 `(x=1) = 1`、链式比较 `x=1=2` 都被拒绝）；
- 单独的列或字面量不是布尔表达式，`x`、`1`、`NULL` 作为整个谓词会被拒绝；
- `IN` 列表不允许空列表、列引用、表达式或子查询。

## 3. AST 形态（S-表达式）

| 写法 | AST |
| --- | --- |
| `x = 1` | `(= x 1)` |
| `x IS NOT NULL` | `(is-not-null x)` |
| `x NOT IN (1, NULL)` | `(not-in x [1 NULL])` |
| `NOT (a OR b)` | `(not (or a b))` |
| `a AND b OR c` | `(or (and a b) c)` |

## 4. 三值逻辑（3VL）

逻辑值为 `TRUE` / `FALSE` / `UNKNOWN`，NULL 参与比较时结果为 UNKNOWN。

### 比较与空值判断
- 任一操作数为 NULL 的 `= <> < <= > >=` 结果恒为 UNKNOWN（因此 `x = x` 在
  `x` 为 NULL 时是 UNKNOWN，不是 TRUE）；
- `IS NULL` / `IS NOT NULL` 永远返回 TRUE 或 FALSE，不产生 UNKNOWN。

### 真值表

| AND | T | F | U |
| --- | --- | --- | --- |
| **T** | T | F | U |
| **F** | F | F | F |
| **U** | U | F | U |

| OR | T | F | U |
| --- | --- | --- | --- |
| **T** | T | T | T |
| **F** | T | F | U |
| **U** | T | U | U |

| NOT | 结果 |
| --- | --- |
| T | F |
| F | T |
| U | U |

### IN / NOT IN（严格保留 NULL 行为）
设针为 `v`，列表元素集合为 `S`：

- `v IN S`
  - `v` 为 NULL → UNKNOWN；
  - `v` 命中 S 中任一非 NULL 元素 → TRUE；
  - 否则 S 含 NULL → UNKNOWN；否则 FALSE。
- `v NOT IN S` 等价于对 `v IN S` 的结果做**三值 NOT**：
  - `v` 为 NULL → UNKNOWN；
  - 命中任一非 NULL 元素 → FALSE；
  - 否则 S 含 NULL → **UNKNOWN（不是 TRUE！）**；否则 TRUE。

关键陷阱：`x NOT IN (1, NULL)` 在 `x=2` 时是 UNKNOWN 而非 TRUE——若先把
UNKNOWN 当 false 再取反就会错误得到 TRUE。本工具直接按三值 NOT 实现，
并用 SQLite 逐赋值交叉核对。

## 5. 两类等价

- **值等价**：有限域内每个赋值下，两边的 TRUE/FALSE/UNKNOWN 完全相同；
- **筛选等价**：有限域内每个赋值下，两边“是否为 TRUE（WHERE 是否收录）”相同。
  FALSE 与 UNKNOWN 都被 WHERE 过滤，因此筛选等价弱于值等价。

反例按**规范顺序**取最小：列按列名字典序排列，每列 NULL 在前、其余整数
升序；枚举到的第一个分叉即最小反例，与声明书写顺序、域值书写顺序无关。

## 6. 范围声明

所有结论只针对声明文件给出的**有限域**。有限域上的等价不代表、也不声称
证明在全部 64 位整数域或任意关系上的等价。
