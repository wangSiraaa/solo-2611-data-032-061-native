# 两阶段提交（2PC）协议说明

本目录实现一个**可在本机反复中断**的两阶段提交演示。协调器与两个参与者各用**独立 SQLite 文件**；不使用单一合并数据库，也**不使用 ATTACH / 跨库事务**。

```
           登记(REGISTER)
   客户端 ───────────────▶ 协调器 coordinator.sqlite3
                               │  持久化请求与内容摘要
                               │
                    prepare    │ 独立子进程调用
              ┌────────────────┴────────────────┐
              ▼                                 ▼
      participant-p1.sqlite3          participant-p2.sqlite3
              │  YES / NO                       │  YES / NO
              └────────────────┬────────────────┘
                               ▼
                    记录最终决定 DECIDED_COMMIT / DECIDED_ABORT   ← 提交点
                               │  (synchronous=FULL，先落盘)
                    commit/abort 决定下发
              ┌────────────────┴────────────────┐
              ▼                                 ▼
         应用暂存变更 + 写终态（同一 SQLite 事务）  同样
```

---

## 1. 数据模型

### 1.1 参与者（`participant-p1.sqlite3`、`participant-p2.sqlite3`）

| 表 | 作用 |
| --- | --- |
| `business(key TEXT PRIMARY KEY, value INTEGER)` | 业务表：字符串键 → 整数值 |
| `prepared_change(txid, seq, key, old_value, new_value)` | 暂存的最终值；prepare 时写入，**业务表不变** |
| `reservation(key PRIMARY KEY, txid)` | 逻辑占用：被 prepare 的键对其它写入不可用 |
| `participant_txn(txid, state, updates_json, updates_digest, request_digest, decision, ...)` | 每笔事务在参与者侧的持久状态：`PREPARED → COMMITTED / ABORTED` |
| `participant_log(id, at, txid, event, detail)` | 参与者本地日志，含 `PREPARE_YES / PREPARE_NO / COMMIT_APPLIED / ABORT_APPLIED / *_REPLAY` |
| `change_audit(txid, key, old_value, new_value, outcome, finished_at)` | 终止时写入的不可变审计，保留该事务实际读到的旧值；重复恢复不会新增行 |
| `reservation_guard_{insert,update,delete}` 触发器 | 兜底：即使绕过程序直接 SQL 修改被占用的 business 键也会被拒绝 |

### 1.2 协调器（`coordinator.sqlite3`）

| 表 | 作用 |
| --- | --- |
| `coordinator_txn(txid, state, request_json, request_digest, decision, decision_order, decision_digest, ...)` | 请求内容、生命周期与**不可更改的最终决定** |
| `participant_reply(txid, participant, prepare, state, delivered_order, ...)` | 每个参与者回答的 prepare 结果与已确认的终态 |
| `coordinator_log(id, at, txid, event, detail)` | 追加写的人类可读日志：`REGISTER / REPLY / DECIDED_COMMIT / DECIDED_ABORT / COMPLETE` |
| `coordinator_counter(name, value)` | 单调递增的投递序号，用于生成可核验的决定摘要 |

协调器状态机：`OPEN → PREPARED_BOTH → DECIDED_COMMIT/DECIDED_ABORT → COMPLETE`。
注意 `PREPARED_BOTH` 仅表示"两边都 YES、等待决定"，**不携带任何提交意图**；真正有约束力的是 `DECIDED_*`。

---

## 2. 请求格式

```json
{
  "txid": "tx-0001",
  "participants": {
    "p1": [
      {"key": "a",           "expected": 1,       "new": 10},
      {"key": "brand-new",   "expected": "ABSENT","new": 5}
    ],
    "p2": [
      {"key": "x", "expected": 1, "new": 20}
    ]
  }
}
```

规则（协调器做结构校验，**每个参与者再独立校验自己的那一半**）：

* `txid` 唯一、非空、≤128 字符；
* 必须恰好包含 `p1`、`p2`，不得有其它端点；
* 每边最多 10 项，同边 `key` 不得重复；
* `expected` 为整数或字符串 `"ABSENT"`（新增）；`new` 为 64 位整数；
* 违反这些规则时，`prepare` 返回 **NO**（带去 `invalid: true`），协议走 ABORT，**不修改任何可见业务值**；
* 同一个 `txid` 只要内容有任何差异（更新项、旧值、端点、顺序）就 **拒绝复用**：协调器比对整个请求摘要，参与者比对自己那半边的摘要，错误码 `TXID_CONTENT_MISMATCH`。

---

## 3. 协议状态表

### 3.1 协调器

| 起点 | 事件 | 终点 | 持久化内容 | 备注 |
| --- | --- | --- | --- | --- |
| — | `REGISTER`（新 txid） | `OPEN` | 请求 JSON、请求摘要、两边更新摘要 | 内容一旦保存即固定 |
| `OPEN` | 收到一边 `YES` | `OPEN` | `participant_reply` 更新 | 另一边未答复 |
| `OPEN` | 两边均 `YES` | `PREPARED_BOTH` | 仅簿记 | **非提交点**，进程退出后恢复按"无决定→ABORT"处理 |
| `PREPARED_BOTH`/`OPEN` | 两个都 YES → `decide(COMMIT)` | `DECIDED_COMMIT` | `decision`, `decision_order`, `decision_digest`, `decided_at` | **提交点**，先于任何下发 |
| `OPEN`/`PREPARED_BOTH` | 任一边非 YES（NO / UNKNOWN） | `DECIDED_ABORT` | 同上 | 无决定时恢复也会写 ABORT |
| `DECIDED_COMMIT` | 两边确认 `COMMITTED` | `COMPLETE` | `completed_at` | 只有此时才报告"完成" |
| `DECIDED_ABORT` | 两边确认 `ABORTED`（或该边从未 prepare） | `COMPLETE` | `completed_at` | |
| 任意已有决定 | 尝试写入相反决定 | 拒绝 | — | `CONTRADICTORY_DECISION`，决定一旦保存不可改变 |

### 3.2 参与者

| 起点 | 事件 | 终点 | 与业务值的关系 |
| --- | --- | --- | --- |
| 无记录 | `prepare`，全部旧值符合且键未被占用 | `PREPARED` | 写入 `prepared_change` + `reservation` + 状态，**同一个 BEGIN IMMEDIATE 事务**；业务值不变 |
| 无记录 | `prepare`，有旧值不符 / 键被占用 / 请求非法 | 无记录 | 只写一条 `PREPARE_NO` 日志；不占用、不暂存 |
| `PREPARED` | 收到**可核验**的 COMMIT | `COMMITTED` | 应用 staging 值 + 删 staging/占用 + 写终态 + `change_audit`，**同一个事务** |
| `PREPARED` | 收到**可核验**的 ABORT | `ABORTED` | 删 staging/占用 + 写终态 + 审计，同一事务；业务值不变 |
| `COMMITTED` | 重复 COMMIT / 重复 prepare（同内容） | `COMMITTED` | 幂等：仅记 `*_REPLAY`，业务不二次变化 |
| `ABORTED` | 重复 ABORT / 重复 prepare（同内容，返回 NO+ABORTED） | `ABORTED` | 幂等 |
| `COMMITTED` | ABORT | 拒绝 | `CONTRADICTORY_DECISION` |
| `ABORTED` | COMMIT | 拒绝 | `CONTRADICTORY_DECISION` |
| `PREPARED` | 断连、超时、重启 | **仍 `PREPARED`** | 不会自行回滚；占用保持，可查询、可解释 |

**关键点**：参与者绝不因为连不上协调器、等待超时或进程重启就把 `PREPARED` 当作已回滚。只有持有"与本地保存的 `request_digest` 一致、且摘要可核验"的协调决定时才迁移终态。

---

## 4. 决定的可核验性

参与者本地保存 `request_digest`（覆盖完整请求）。协调器下发决定时带上：

```
decision_digest = SHA-256( "2pc-decision\n"
                           "txid=<txid>\n"
                           "request=<request_digest>\n"
                           "decision=<COMMIT|ABORT>\n"
                           "order=<单调序号>\n" )
```

参与者用**自己保存的** `request_digest` 重新计算并比对；不符则 `BAD_DECISION_DIGEST` 拒绝执行，状态与占用保持不变。因此伪造、错配或属于别的事务的决定都无法驱动提交/撤销。重复下发相同决定得到相同摘要，天然幂等；相反决定摘要不同且会被终态检查拒绝。

---

## 5. 恢复规则

`recover` 逐笔读取协调器持久记录：

| 协调器记录 | 恢复动作 |
| --- | --- |
| **没有最终决定**（`OPEN`/`PREPARED_BOTH`） | **记录 ABORT**（`DECIDED_ABORT`），再向两边下发 ABORT。前提是：没有 COMMIT 决定且参与者按协议只能依据已保存决定提交，因此两边不可能已提交。 |
| 已记录 `COMMIT` | **继续驱动**尚未完成的参与者提交；**绝不**改成补偿回滚。 |
| 已记录 `ABORT` | 继续驱动 ABORT。 |
| 已 `COMPLETE` | 什么都不做（`already_complete`），不产生业务或审计变化。 |

恢复可以反复执行：已完成的事务再次恢复不增加业务影响；未完成的下发重试是幂等的。**不允许**通过删除 `reservation` 记录来"解除"占用：占用只能由应用可核验决定的那一个事务释放。

---

## 6. 允许的暂态与不提供的保证

* 决定保存后、下发过程中，允许出现**一边已 COMMITTED/ABORTED、另一边仍 PREPARED** 的暂态。
* 此时 `result` 明确返回 `complete: false` 并列出仍待完成的参与者；只有两边都完成才 `complete: true`。
* 本协议**不提供跨库瞬时一致读取**：在两个独立文件同时对外可读的任意瞬间，观察者可能看到一边新值、另一边旧值。它保证的是"最终两侧要么都新、要么都旧"，以及在协调器记录决定后的**提交/撤销一定会被驱动到底**。
* 每边的整组业务变更与其事务终态在同一个 SQLite 事务中提交，因此单边内部不会出现"改了值但没记终态"或"记了终态但没改值"。

---

## 7. SQLite 同步与锁配置

每个连接（含只读查询）：

```
PRAGMA busy_timeout = 10000;     -- 独立进程共享文件，等写锁而非立即失败
PRAGMA synchronous = FULL;       -- 每次 COMMIT 都 fsync：决定落盘后才下发
PRAGMA temp_store = MEMORY;
PRAGMA foreign_keys = ON;        -- staging/占用 与 participant_txn 的外键
PRAGMA trusted_schema = OFF;
```

每个文件（持久属性）：

```
PRAGMA journal_mode = WAL;       -- 读者快照不阻塞写者；模式写入文件
PRAGMA wal_autocheckpoint = 1000;
PRAGMA journal_size_limit = 67108864;
```

写事务一律 `BEGIN IMMEDIATE`：先拿写锁，保证"读旧值 → 校验 → 写暂存/终态"期间没有其它写者插入。只读查询使用 `PRAGMA query_only = ON`，杜绝误写。

> WAL 下若数据库旁没有 `-shm` 文件，sqlite3 的 `mode=ro` URI 会失败（打开器需要建共享内存索引），因此读取路径采用普通连接 + `query_only`，不改变只读语义。

---

## 8. 故障点与可复现场景

CLI 的 `--fault` 取值（`submit`/`recover` 可用）：

| fault | 含义 |
| --- | --- |
| `after-prepare:p1` / `after-prepare:p2` | 参与者 prepare 已持久化 YES 后 `os._exit`，回执丢失 |
| `before-decision` | 两边 prepare 完成后、写决定前协调器退出 |
| `after-decision` | 决定已持久化后、任何下发前协调器退出 |
| `after-first-delivery` | COMMIT/ABORT 只下发并完成一边后协调器退出 |
| `after-apply:p1` / `after-apply:p2` | 参与者已应用并写终态后、回执前 `os._exit` |

`scripts/demo_faults.py` 用公开 CLI 跑通下列全部场景（6/6）：

1. 一边 YES、一边 NO → ABORT，两侧可见值不变，YES 边的占用被解除；
2. 两边准备后协调器退出 → 恢复记录 ABORT，两侧保持旧值，占用清零；
3. COMMIT 已保存但只通知一边 → `result` 报未完成；恢复用记录中的 COMMIT 继续驱动另一边；
4. COMMIT 已保存、未通知任何一边 → 恢复把两边都驱动到 COMMITTED；
5. 参与者提交后回执丢失 → 恢复重新确认（幂等），再次恢复为 no-op；
6. 重复恢复 5 次 → 业务表与 `change_audit` 行数完全不变。

---

## 9. 为什么可以安全重试

* **决定前**的任何失败（prepare 回执丢失、协调器在决定前退出）：没有 COMMIT 记录，恢复写 ABORT。参与者即使已 PREPARED，也尚未提交，回滚安全。
* **决定后**的任何失败（下发丢失、参与者回执丢失）：决定已成事实，恢复只做同一方向的重复投递。参与者对同一 `(txid, decision, order)` 幂等；相反方向会被拒绝。
* **编号复用**：内容摘要绑定 `txid`，不同内容无法复用同一编号；完全相同内容的重复 submit/prepare/commit/abort 均为幂等重放。
* 每一次"重试"读取的都是**协调器持久决定**，而不是连接状态、超时或内存标志。
