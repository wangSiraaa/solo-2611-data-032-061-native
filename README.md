# 可中断的两阶段提交演示（独立 SQLite 协调器 + 两个参与者）

本仓库演示**跨两个独立数据库**的两阶段提交（2PC），专门用于理解"什么时候可以安全重试"。
协调器、参与者 p1、参与者 p2 各用**独立 SQLite 文件**，不使用单一合并库，也**不使用 ATTACH 或跨库事务**来假装协议。

* 协议细节、状态表、SQLite 同步配置、可核验决定的摘要公式：见 [PROTOCOL.md](PROTOCOL.md)。
* 全部命令输出一行 JSON；诊断信息写 stderr。失败的协议操作通过 `ok:false` / 非零退出码表达。
* 测试数据全部本地生成（固定种子，可重复）。

## 环境

只依赖 Python 3.11 标准库（`sqlite3`、`unittest`）。无需 pip 安装。

```sh
python3 --version          # 3.11.x
```

## 快速开始

```sh
# 1. 创建三个独立 SQLite 文件（协调器 + 两个参与者）
python3 -m twopc init --force

# 2. 生成一个请求（这里用 realistic 模板）
python3 -m twopc generate --kind realistic --seed 11
# -> .runtime/demo/requests/realistic-11-01.json

# 3. 正常提交
python3 -m twopc submit --request-file .runtime/demo/requests/realistic-11-01.json

# 4. 查看结果 / 独立验证
python3 -m twopc result realistic-11-01
python3 -m twopc verify realistic-11-01 --use-recorded-request
```

默认演示目录是 `.runtime/demo`（已在 `.gitignore` 中忽略）。所有命令都接受 `--base DIR` 指定另一个目录，便于并行做多组故障实验。

## 命令一览

| 命令 | 作用 |
| --- | --- |
| `init [--force] [--no-seed]` | 创建协调器与两个参与者的独立 SQLite 文件；`--force` 先删除已有文件 |
| `generate --kind {basic,coverage,realistic} --seed N [--count N]` | 本地生成确定性请求 JSON（每边 ≤10 键、同边不重复、含 ABSENT 新增） |
| `submit --request-file F` / `--request-json J` / `--stdin` `[--fault POINT ...]` | 登记 → 两边 prepare → 记录决定 → 下发；可按需注入故障点 |
| `participant-status TXID [--participant p1\|p2\|both]` | 读取参与者持久状态、暂存变更、占用；对未解除占用给出解释 |
| `recover [--txid TXID] [--report-impact] [--fault POINT ...]` | 按恢复规则处理：无决定→记录 ABORT；有 COMMIT→继续驱动；重复执行幂等 |
| `result TXID` | 报告请求是否完成；一边完成一边仍 PREPARED 时明确返回未完成 |
| `verify TXID [--request-file F \| --use-recorded-request]` | 独立检查：无半笔更新、无悬挂占用、无矛盾决定；失败时非零退出 |
| `journal [--txid TXID]` | 打印协调器/参与者的事务日志（可直接手查） |
| `locks` | 列出并解释所有未解除的逻辑占用；`unresolved` 计数 |
| `ordinary-set --participant p1\|p2 --key K --value V [--expected E\|ABSENT]` | 普通（非 2PC）写入，用来演示已准备键被占用时会被拒绝 |
| `inspect` | 导出三个文件的业务表、事务、占用、审计与日志 |
| `sql --target {coordinator,p1,p2} --query "..."` | 对任一文件执行**只读** SQL（环境里没有 sqlite3 CLI 时的替代） |
| `config` | 显示三个文件生效的 SQLite 同步/日志设置 |

### 请求格式

```json
{
  "txid": "tx-0001",
  "participants": {
    "p1": [
      {"key": "checking", "expected": 1000, "new": 942},
      {"key": "brand-new", "expected": "ABSENT", "new": 5}
    ],
    "p2": [
      {"key": "partner-checking", "expected": 800, "new": 860}
    ]
  }
}
```

约束：`txid` 唯一；必须恰好有 `p1`、`p2`；每边 ≤10 项；同边键不重复；`expected` 为整数或 `"ABSENT"`；`new` 为整数。结构合法但内容非法的请求会让对应参与者 `prepare` 返回 **NO**，协议 ABORT，且不改变可见值。

## 故障注入与场景复现

`submit` / `recover` 的 `--fault` 取值：

| fault | 注入点 |
| --- | --- |
| `after-prepare:p1` / `after-prepare:p2` | 参与者 prepare 已持久化后进程退出，YES 回执丢失 |
| `before-decision` | 两边都 PREPARED、协调器写决定前退出 |
| `after-decision` | COMMIT/ABORT 已持久化、对所有参与者下发前退出 |
| `after-first-delivery` | 决定只完成一边的下发后协调器退出 |
| `after-apply:p1` / `after-apply:p2` | 参与者已应用并写终态后、回执前退出（"提交后回执丢失"） |

一键跑完题目要求的全部场景（每个场景使用全新目录）：

```sh
python3 scripts/demo_faults.py          # 6/6 scenarios passed
python3 scripts/demo_faults.py --scenario 3
```

自动化测试：

```sh
python3 -m unittest discover -s tests -v   # 33 个测试
```

### 手工复现（逐条命令）

下面以 `--base .runtime/lab` 为例。每条命令都可直接原样运行。

#### 场景 A：一边 YES、一边 NO

```sh
python3 -m twopc --base .runtime/lab init --force
cat > .runtime/lab/req-yn.json <<'EOF'
{"txid":"tx-yes-no","participants":{
  "p1":[{"key":"a","expected":1,"new":10},
        {"key":"new-key","expected":"ABSENT","new":5}],
  "p2":[{"key":"x","expected":999,"new":20}]}}
EOF
python3 -m twopc --base .runtime/lab submit --request-file .runtime/lab/req-yn.json
# p1 YES，p2 NO（expected 999 与实际 1 不符）→ 协调器记录 ABORT
python3 -m twopc --base .runtime/lab participant-status tx-yes-no
python3 -m twopc --base .runtime/lab verify tx-yes-no --request-file .runtime/lab/req-yn.json
# 结果：p1 ABORTED、占用为空；p2 从未 prepare；业务值均为旧值
```

#### 场景 B：两边准备后协调器退出（无最终决定）

```sh
python3 -m twopc --base .runtime/lab generate --kind realistic --seed 11
python3 -m twopc --base .runtime/lab submit \
  --request-file .runtime/lab/requests/realistic-11-01.json \
  --fault before-decision            # 退出码 70（模拟进程死亡）
python3 -m twopc --base .runtime/lab result realistic-11-01
# complete=false，coordinator_state=PREPARED_BOTH，decision=null
python3 -m twopc --base .runtime/lab locks          # unresolved=6
python3 -m twopc --base .runtime/lab ordinary-set \
  --participant p1 --key checking --value 999       # 被占用，拒绝
python3 -m twopc --base .runtime/lab recover --txid realistic-11-01
# 恢复记录 ABORT 并下发；两边 ABORTED，业务保持旧值，占用清零
python3 -m twopc --base .runtime/lab verify realistic-11-01 \
  --request-file .runtime/lab/requests/realistic-11-01.json
python3 -m twopc --base .runtime/lab recover --txid realistic-11-01   # 再跑一次：no-op
```

#### 场景 C：COMMIT 已保存，只通知一边

```sh
python3 -m twopc --base .runtime/lab generate --kind basic --seed 4
python3 -m twopc --base .runtime/lab submit \
  --request-file .runtime/lab/requests/basic-4-01.json \
  --fault after-first-delivery     # 协调器退出码 70
python3 -m twopc --base .runtime/lab result basic-4-01
# decision=COMMIT，coordinator_state=DECIDED_COMMIT，complete=false（p2 仍 PREPARED）
python3 -m twopc --base .runtime/lab recover --txid basic-4-01
# 恢复只继续驱动 COMMIT，不改向；两边 COMMITTED
```

#### 场景 D：COMMIT 已保存、未通知任何一边

```sh
python3 -m twopc --base .runtime/lab submit \
  --request-file .runtime/lab/requests/basic-4-01.json \
  --fault after-decision
python3 -m twopc --base .runtime/lab recover --txid basic-4-01
python3 -m twopc --base .runtime/lab verify basic-4-01 \
  --use-recorded-request
```

#### 场景 E：参与者提交后回执丢失

```sh
python3 -m twopc --base .runtime/lab generate --kind realistic --seed 7
python3 -m twopc --base .runtime/lab submit \
  --request-file .runtime/lab/requests/realistic-7-01.json \
  --fault after-apply:p1
# p1 已 COMMITTED（回执丢失），p2 正常完成
python3 -m twopc --base .runtime/lab recover --txid realistic-7-01
python3 -m twopc --base .runtime/lab recover --txid realistic-7-01   # 再次：already_complete
python3 -m twopc --base .runtime/lab verify realistic-7-01 --use-recorded-request
```

## 手查事务日志

无需 sqlite3 CLI：

```sh
python3 -m twopc journal --txid realistic-11-01        # 协调器 + 两个参与者日志
python3 -m twopc sql --target coordinator \
  --query "select txid,state,decision,decision_order from coordinator_txn"
python3 -m twopc sql --target p1 \
  --query "select txid,state,decision,decision_order from participant_txn"
python3 -m twopc sql --target p1 --query "select * from change_audit"
python3 -m twopc sql --target p1 --query "select * from reservation"
python3 -m twopc inspect
```

也可以直接用任意 SQLite 客户端打开三个文件：

```
.runtime/demo/coordinator.sqlite3
.runtime/demo/participant-p1.sqlite3
.runtime/demo/participant-p2.sqlite3
```

关键表：`coordinator_txn`、`coordinator_log`、`participant_reply`；参与者侧 `business`、`prepared_change`、`reservation`、`participant_txn`、`participant_log`、`change_audit`。

## 参与者独立进程

协调器通过 `python3 -m twopc.worker ...` 子进程驱动每个参与者（prepare / apply / status），每次调用都是一次独立的 OS 进程，状态只落在该参与者自己的文件。
也可手工运行（示例）：

```sh
printf '%s' '{"updates":[{"key":"a","expected":1,"new":10}],"request_digest":"<digest>"}' \
  | python3 -m twopc.worker prepare \
      --db .runtime/demo/participant-p1.sqlite3 --name p1 --txid manual-1
python3 -m twopc.worker status \
  --db .runtime/demo/participant-p1.sqlite3 --name p1 --txid manual-1
```

## 文件布局

```
twopc/
  cli.py          公开命令
  coordinator.py  协调器：请求/决定日志、状态机、结果
  participant.py  参与者：prepare/apply、占用、审计、日志
  driver.py       编排：子进程 prepare/下发/恢复
  worker.py       参与者子进程入口
  digest.py       request/updates/decision 摘要
  request.py      请求校验
  verify.py       独立验证器
  workload.py     确定性数据生成
  layout.py, util.py
PROTOCOL.md       协议状态表、同步配置、恢复规则
scripts/demo_faults.py  全部故障场景一键复现
tests/test_twopc.py     自动化测试
```

## 协议保证与不保证

保证：

* 每笔事务在协调器有**唯一且不可更改**的最终决定；决定先持久化（`synchronous=FULL`）再下发。
* 每边整组业务变更与其终态在**同一个 SQLite 事务**中提交。
* 恢复后两侧要么都采用新值、要么都保留旧值；无半笔更新、无悬挂占用、无矛盾决定。
* 重复 prepare / commit / abort / 恢复均为幂等；`txid` 内容不同则拒绝复用。
* `PREPARED` 不会因断连、超时、重启而被擅自回滚；只能由可核验决定解除。

不保证：

* **不提供跨库瞬时一致读取**。决定下发期间允许"一边完成、另一边仍 PREPARED"。`result` 只有两边都完成才报告 `complete:true`。
* 不接入任何真实外部系统；所有数据本地生成。
