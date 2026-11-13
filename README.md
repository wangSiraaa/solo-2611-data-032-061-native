# 多版本键值 CLI(Python 3.11 + SQLite,快照隔离)

本仓库实现一个小型多版本键值存储命令行工具 `mvcc.py`,面向“数据继续修改、
查询视图保持不变、又不永久保留全部历史值”的本地分析场景。仅依赖 Python 3.11
标准库(SQLite 内置),数据库与全部样例均在本机。

- 实现:`mvcc.py`(CLI 全部命令)
- 两进程交错演示:`demo.py`
- 语义测试套件:`run_tests.py`(18 个用例,含万级键与 kill -9 故障)
- 原生环境说明见 [environment/README.md](environment/README.md)(本题选用
  Python 标准库自带的 SQLite,无需启动 PostgreSQL)

## 语义模型

- 键、值均为**非空 UTF-8 字符串**;删除用**独立墓碑**(tombstone)表示,
  不允许用空串冒充删除(`put` 空值直接报错)。
- 每次**成功的写事务**取得严格递增的整数提交版本(1 起,无间隙);
  **只读事务不消耗版本号**,但也必须显式 `commit`/`abort` 结束。
- `begin` 保存当时最新已提交版本为 `begin_version`;事务编号由调用者提供,
  **重复 begin 返回原事务身份**,不创建新快照。事务跨多次 CLI 调用存活,
  直到显式 `commit`/`abort`,**不按等待时长过期**。
- 事务内 `get`/`scan` 读取 `<= begin_version` 的最新已提交值,再叠加本事务
  私有 put/delete;其他事务未提交的修改不可见。`scan` 按 **UTF-8 字节序**
  (SQLite TEXT 的 BINARY 排序即 UTF-8 字节序),`--start` 含、`--end` 不含。
- **快照隔离**:提交时若写集中任一键在 `begin_version` 之后被其他事务成功
  修改(含删除),整笔写事务冲突失败,私有修改均不发布;写不同键可以成功,
  不额外承诺消除跨键写偏差。
- 一个事务的多键版本、提交编号与终态在**同一个 SQLite 事务**中原子持久化;
  **重复 commit 返回原回执**,绝不产生第二个提交版本。

## 数据库结构

```sql
CREATE TABLE meta (            -- 单行:下一个待分配的提交版本(首个提交为 1)
  id            INTEGER PRIMARY KEY CHECK (id = 1),
  next_version  INTEGER NOT NULL
);
CREATE TABLE txns (            -- 事务登记簿:终态与幂等回执永不删除
  txn_id         TEXT PRIMARY KEY,               -- 事务编号,调用者提供
  begin_version  INTEGER NOT NULL,               -- begin 时最新已提交版本
  state          TEXT NOT NULL
    CHECK (state IN ('active','committed','aborted','conflicted')),
  commit_version INTEGER,                        -- 写事务的提交版本;只读为 NULL
  receipt        TEXT,                           -- 终态回执(JSON),幂等重放用
  created_at     TEXT NOT NULL,
  ended_at       TEXT
);
CREATE TABLE kv_versions (     -- 已提交多版本数据;gc 只清理本表
  key       TEXT    NOT NULL,
  version   INTEGER NOT NULL,
  value     TEXT,              -- 墓碑时为 NULL
  tombstone INTEGER NOT NULL CHECK (tombstone IN (0,1)),
  txn_id    TEXT    NOT NULL,  -- 由哪个事务提交(审计)
  PRIMARY KEY (key, version)
);
CREATE INDEX idx_kv_versions_version ON kv_versions(version);
CREATE TABLE writes (          -- 各活跃事务的私有写缓冲;其他事务不可见,
  txn_id    TEXT    NOT NULL,  -- gc 不参与本表;提交时发布、中止/冲突时丢弃
  key       TEXT    NOT NULL,
  value     TEXT,
  tombstone INTEGER NOT NULL CHECK (tombstone IN (0,1)),
  PRIMARY KEY (txn_id, key)
);
```

## 并发与故障控制

- **WAL + synchronous=FULL**:读不阻塞写;提交即落盘,回执不丢。
  `busy_timeout=30s`,写者排队而非立即报错。
- **写者串行化**:`begin`/`put`/`delete`/`commit`/`abort`/`gc` 都在
  `BEGIN IMMEDIATE` 事务内执行,彼此互斥;`get`/`scan` 用只读事务。
- **原子提交**:版本行、`meta.next_version`、事务终态、回执、私有写清理
  同属一个 SQLite 事务,要么全部生效要么全部回滚。
- **gc 与 begin/commit 并发安全**:三者由 `BEGIN IMMEDIATE` 串行化。
  若 begin 先登记,gc 看得到该快照并为其保留版本;若 gc 先完成,新快照的
  `begin_version` 是 gc 之后的最新已提交版本,它只会读各键最新版本,而
  gc 永远保留每键最新版本,因此不会漏掉刚建立的有效快照。
- **故障注入点**(环境变量,用于崩溃演练):
  - `MVCC_FAILPOINT="点1,点2"`:命中时 `os._exit(118)` 模拟 kill -9;
  - `MVCC_SLEEP="点=毫秒,..."`:命中时先在事务内睡眠,便于真实 kill 落在
    事务中间(测试用 `kill -9` 验证)。
  - 点位:`begin:after_insert`、`put:after_write`、`delete:after_write`、
    `commit:after_version_alloc`、`commit:before_finalize`、
    `commit:after_commit`、`gc:after_snapshot_read`、`gc:after_delete`。
- **崩溃恢复语义**(均有测试覆盖):
  - 提交事务在 `commit:before_finalize` 前崩溃 → SQLite 回滚,不发布任何
    版本,事务仍为 active,可干净重提,版本只分配一次;
  - 在 `commit:after_commit`(库已提交、回执未打印)崩溃 → 重提返回
    **同一回执、同一提交版本**,不产生第二个版本;
  - gc 中途崩溃 → 整个 gc 回滚,无半清理状态。

## GC 规则

`gc` 依据当前所有未结束事务(`state='active'`)的 `begin_version`:

- 每个键的**最新版本必须保留**(含墓碑);
- 每个活跃快照对该键**实际会读到的那个版本**(`<= begin_version` 的最大
  版本,可能是墓碑)必须保留;
- 其余已提交旧版本**物理删除**;私有写缓冲不参与删除。
- 墓碑同样参与可见性:若某快照读到的是墓碑,该墓碑被保留,已删除键不会因
  清理暴露更老值而复活。
- 输出每个保留版本的原因(`latest` / `snapshot:<txn>@begin=<v>`)与删除数;
  `--summary` 只输出计数。`txns` 终态与幂等回执**不随业务版本回收**。

## 命令参考

```
python3 mvcc.py --db <path> init
python3 mvcc.py --db <path> begin  --txn T
python3 mvcc.py --db <path> put    --txn T --key K --value V
python3 mvcc.py --db <path> put    --txn T --file kvs.jsonl   # {"key":..,"value":..}/行
python3 mvcc.py --db <path> delete --txn T --key K
python3 mvcc.py --db <path> delete --txn T --file keys.jsonl  # {"key":..}/行
python3 mvcc.py --db <path> get    --txn T --key K
python3 mvcc.py --db <path> scan   --txn T [--start S] [--end E] [--limit N]
python3 mvcc.py --db <path> commit --txn T
python3 mvcc.py --db <path> abort  --txn T
python3 mvcc.py --db <path> gc [--summary]
python3 mvcc.py --db <path> verify --oracle oracle.jsonl [--strict-gc] [--sample N]
```

- 所有命令输出单行 JSON;退出码:0 正常(写冲突也是正常结果,见
  `status:"conflicted"`)、2 用法/状态错误、1 内部错误、118 故障注入崩溃。
- `--db` 也可用环境变量 `MVCC_DB` 指定,默认 `./mvcc.db`。
- `verify` 是**独立验证器**:从驱动侧记录的完整提交历史(oracle JSONL,
  回收前)重建各键全部版本,计算每个活跃快照应见值,再用 CLI 的 `scan`
  全量比对、抽样 `get` 比对;并检查 gc 不变量(活跃快照需要的版本必须
  还在;`--strict-gc` 时不许多余版本)、事务终态与幂等回执、提交版本序列。

## 原生运行说明

需要 Python 3.11+(只用标准库,无需安装任何依赖,无需启动服务):

```sh
cd /workspace
python3 --version            # 3.11.x

# 1) 最小上手
DB=.runtime/quick.db
python3 mvcc.py --db $DB init
python3 mvcc.py --db $DB begin --txn=t1
python3 mvcc.py --db $DB put --txn=t1 --key=hello --value=世界
python3 mvcc.py --db $DB commit --txn=t1
python3 mvcc.py --db $DB begin --txn=r1
python3 mvcc.py --db $DB get --txn=r1 --key=hello
python3 mvcc.py --db $DB commit --txn=r1
python3 mvcc.py --db $DB gc

# 2) 两进程交错演示:长读事务保持旧视图,另一进程覆盖/删除/重插同键,
#    其间多次 gc;旧读结束后 gc 释放被固定版本;最后独立验证。
python3 demo.py run --dir .runtime/demo

# 3) 语义测试套件(18 个用例:双写冲突、多键全回滚、独立键成功、
#    读自身修改、幂等 begin/commit/abort、墓碑不复活、gc 释放、
#    故障注入与 kill -9、并发 begin/gc/commit、随机负载验证、万级键)
python3 run_tests.py          # 或 python3 run_tests.py test_stress_10k_keys
```

数据库文件、演示与测试产物均写入 `.runtime/`(已 gitignore);测试本身使用
临时目录,不留残留。

## 范围说明

本工具聚焦**版本语义正确性**而非性能:数据规模为本地合成的万级键
(`test_stress_10k_keys` 用 10000 键验证快照固定、gc 精确删除数与扫描
结果),未做性能排行榜式调优。
