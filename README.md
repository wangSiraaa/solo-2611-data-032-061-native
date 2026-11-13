# 离线事务历史审计 CLI（txaudit）

纯 Python 3.11 标准库实现（无第三方依赖），对并发事务历史做离线审计：

1. **输入语义校验**：单版本读写语义回放，逐条核对 `read` 的来源身份（`write_id` /
   `INITIAL`）与实际读值；违反语义的操作全部报告，不进入可串行化判定；
2. **视图可串行化**：把每个事务整段按原序连续执行，枚举事务的全部排列，
   按 read 来源 **write 身份**（不是字符串值）和每键最终写入者判定，输出合法排列
   数量与码点序最小的合法排列；不存在则明确为 0；
3. **冲突可串行化**：构造冲突先后图（不同事务同键操作且至少一个是 write 即加边，
   每条边附逐组来源操作证据），三色 DFS 检测环并给出一条逐边可核验的环；
4. **独立复核**：另一条实现路径（递归 DFS + 增量状态回溯、独立边集重建）交叉核对，
   ≤6 事务时全量核对合法排列集合；
5. **SQLite 持久化**：输入历史与规范 JSON 报告一并落库，全部本地完成，不连接任何
   真实数据库服务。

## 目录结构

```
txaudit/
  model.py      输入加载、约束校验、单版本语义回放
  analysis.py   视图等价全枚举（itertools 路径）、冲突图与环检测
  verify.py     独立复核器（递归 DFS + 增量回溯，独立边集重建）
  generate.py   固定样例套件 + 可复现伪随机历史合成
  storage.py    SQLite 存储（audit_runs 表）
  report.py     规范 JSON 报告（稳定排序、固定序列化参数、SHA-256 指纹）
  cli.py        generate / audit / verify / list-db 子命令
tests/
  test_txaudit.py  24 个 unittest 用例（标准库，无第三方依赖）
.runtime/           运行产物（已被 .gitignore 忽略）
```

## 输入 JSON 说明

```json
{
  "history_id": "可选，缺省 default",
  "keys": {"x": "x0", "y": "y0"},
  "transactions": [1, 2, 3],
  "operations": [
    {"seq": 1, "transaction": 1, "op": "write",
     "write_id": "w1", "key": "x", "value": "a"},
    {"seq": 2, "transaction": 2, "op": "read",
     "key": "x", "value": "a", "read_from": "w1"},
    {"seq": 3, "transaction": 1, "op": "commit"}
  ]
}
```

| 约束 | 规则 |
| --- | --- |
| 规模 | 事务 1..8 个；键 1..20 个；操作 1..200 条 |
| `seq` | 全局唯一整数；工具按 seq 升序回放，输入数组自身无需有序 |
| `write` | 必须带全局唯一 `write_id`、已声明的 `key`、字符串 `value` |
| `read` | 必须带 `key`、字符串实际读值 `value`、`read_from`（某 `write_id` 或 `INITIAL`） |
| `commit` | 每个事务必须且只能提交一次；提交后不得再有该事务的任何操作；无回滚 |
| 键 | 全部预先存在，不允许新增/删除，read/write 只能引用 `keys` 中声明的键 |

**单版本语义**：read 必须引用全局顺序中该键此前最近一次 write 的 `write_id`，
此前没有写入则只能引用 `INITIAL`；写入事务即使尚未提交，其写入也按此规则可见。
来源按 **write_id 身份**判定——两个不同 `write_id` 恰好写入相同字符串也不得混同；
身份与实际值分别核对，错误分别报告（`BAD_READ_SOURCE` / `BAD_READ_VALUE`）。

**最终写入者**：每键由整段历史最后一次 write 确定（记录其 `write_id`），
从未被写入的键为 `INITIAL`。

## 判定语义

- **视图串行等价**：存在一个事务排列，使每个事务自身操作按原顺序连续执行后，
  ① 每个 read 的来源 write 身份与原历史完全相同；② 每键最终写入者与原历史完全相同。
  工具枚举完全部 n! 个排列后才下结论；一次排列失败不会终止枚举。
- **冲突先后图**：不同事务对同一键的两个操作，只要至少一个是 write，就按原历史
  先后加有向边；read-read 不加边。每条边保留至少一组证据（键、两端 seq、操作类型、
  write_id），可逐条回到输入核验。图无环 ⇔ 冲突可串行化。
- 两个结论**分别报告**：冲突图有环只否定冲突可串行化，不自动否定视图可串行化。
  固定样例 `blind_write_cycle_view_ok` 即“图有环但存在视图等价排列”的反例。

## 完整原生命令

以下命令均在仓库根目录执行，只用 Python 3.11 标准库。

```bash
# 0) 运行全部测试
python3.11 -m unittest tests.test_txaudit -v

# 1) 生成全部固定样例（11 个）到目录
python3.11 -m txaudit generate --all --out .runtime/examples/suites

# 1b) 生成单个固定样例
python3.11 -m txaudit generate --suite blind_write_cycle_view_ok \
    --out .runtime/examples/suites/blind_write_cycle_view_ok.json

# 1c) 生成可复现随机历史（同 seed/参数输出字节级一致；自动标注 read_from）
python3.11 -m txaudit generate --random --seed 20261113 \
    --transactions 8 --keys 12 --ops 64 \
    --out .runtime/examples/bench8.json

# 2) 审计：报告写文件，同时把输入与报告存入 SQLite
python3.11 -m txaudit audit .runtime/examples/suites/blind_write_cycle_view_ok.json \
    --report .runtime/reports/blind_write_cycle_view_ok.json \
    --db .runtime/audit.db

# 2b) 审计结果直接打印到 stdout（不写库）
python3.11 -m txaudit audit .runtime/examples/bench8.json

# 3) 独立复核已生成的报告（退出码 0 通过 / 1 不通过 / 2 用法或 IO 错误）
python3.11 -m txaudit verify .runtime/examples/suites/blind_write_cycle_view_ok.json \
    --report .runtime/reports/blind_write_cycle_view_ok.json

# 4) 列出 SQLite 中的审计记录
python3.11 -m txaudit list-db --db .runtime/audit.db
```

退出码约定：`0` 成功（含“成功产出输入语义错误报告”）；`1` 独立复核未通过；
`2` 用法、IO 或 JSON 语法错误。审计耗时等非确定性信息只打印到 stderr，
不进入报告 JSON，保证重复输入得到字节级相同的报告。

## 固定样例与覆盖点

| 样例 | 覆盖点 |
| --- | --- |
| `serial` | 正常串行，同键读写/写写冲突下唯一合法排列 |
| `readonly` | 只读事务不产生出自它的写边，插入位置自由产生多个合法排列 |
| `rw_cycle_bad` | 读后写循环：枚举完 2! 个排列，合法数明确为 0，两个判定都失败 |
| `self_read` | 事务读自身写入，同键多次自写自读，来源精确到 write_id |
| `blind_write_cycle_two_txn` | 两事务纯盲写环：图有环且视图合法数为 0（对照反例） |
| `blind_write_cycle_view_ok` | **无读取盲写致冲突图有环，但视图等价排列存在**（核心反例） |
| `same_value_identity_ok` | 两个不同 write_id 写相同字符串，合法历史按身份区分来源 |
| `same_value_bad_source` | 同上但 read_from 错指：值相同仍报 `BAD_READ_SOURCE`，不进入判定 |
| `same_value_permutation_identity` | 排列内字符串全程相同但来源身份改变，视图枚举按身份拒绝 |
| `view_but_not_conflict` | 含盲写的经典结构：视图可串行化、冲突图有环 |
| `untouched_key` | 未写入键的最终写入者保持 `INITIAL` |

## 8 事务上限的运行时间

在本机（Linux、Python 3.11.2）对 8 事务历史全枚举 8! = 40320 个排列并做独立复核，
固定 seed=20261113 可复现，实测约 **50–150 ms**（随操作数 32/64/100/200 变化）；
同一份输入重复运行的报告 SHA-256 完全相同。测试
`DeterminismAndBenchmarkTests.test_eight_transaction_runtime_reproducible`
会打印实测耗时，并以 30 秒宽松上界断言。

## 规范报告稳定性

- 列表全部显式排序：事务编号升序、read/证据按 seq、边按端点、环按 DFS 访问序；
- 字典固定键顺序手工组装，不含时间戳/主机名等非确定字段；
- 统一 `json.dumps(ensure_ascii=False, indent=2)`，UTF-8 输出、末尾换行；
- 报告含输入内容的 SHA-256 指纹，便于核对“报告对应哪份输入”。
