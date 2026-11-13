# 本地有序 Merkle 数据集 CLI

离线数据交付场景下，复核方只需检查少量键是否存在/缺失，无需下载整个数据集。
本项目用 Python 3.11 标准库实现有序 Merkle 树：SQLite 承担外排与随机访问，
SHA256 承担完整性，证明按查询批生成并对共享树节点去重。全程无网络服务、
无第三方依赖。格式细节见 [FORMAT.md](FORMAT.md)。

## 目录结构

```
merkle_dataset/          # 实现包（仅标准库）
  hashing.py             # 叶/内部/补齐/空根摘要规则
  manifest.py            # 可信根清单
  storage.py             # SQLite 外排构建、随机访问、加载重算核验
  proving.py             # 存在/缺失证明与共享节点去重
  verifier.py            # 独立验证器（不接触数据集/树）
  cli.py / __main__.py   # generate/build/prove/verify/inspect
tests/                   # unittest 用例
demo_100k.py             # 十万条合成记录端到端演示
FORMAT.md                # 交付格式与信任边界说明
.runtime/                # 本地产物（已被 .gitignore 忽略）
```

## 本机命令

```sh
# 0. 运行测试（在仓库根目录）
python3.11 -m unittest discover -s tests -t .

# 1. 生成合成 JSONL（也可自备：每行 {"key":..,"value":..}）
python3.11 -m merkle_dataset generate --count 1000 --output .runtime/d.jsonl

# 2. 构建 SQLite 树与可信根清单（重复键/非法输入整体失败，无残留产物）
python3.11 -m merkle_dataset build \
  --input .runtime/d.jsonl --db .runtime/tree.db --manifest .runtime/manifest.json

# 3. 准备查询键（JSON 字符串数组，互不重复、非空）
printf '["key-0000000001","不存在的键"]' > .runtime/queries.json

# 4. 出证：输出逐项存在/缺失结论与证明体积、共享对照
python3.11 -m merkle_dataset prove \
  --db .runtime/tree.db --manifest .runtime/manifest.json \
  --queries .runtime/queries.json --output .runtime/proof.json

# 5. 独立验证：只给清单、查询集合、证明，不访问数据或树
python3.11 -m merkle_dataset verify \
  --manifest .runtime/manifest.json \
  --queries .runtime/queries.json --proof .runtime/proof.json

# 6. 检视（inspect 树会触发加载期完整重算核验）
python3.11 -m merkle_dataset inspect --manifest .runtime/manifest.json
python3.11 -m merkle_dataset inspect --db .runtime/tree.db --manifest .runtime/manifest.json
python3.11 -m merkle_dataset inspect --proof .runtime/proof.json --json

# 十万条记录演示（构建耗时、证明体积与批量共享/逐条路径对照）
python3.11 demo_100k.py
```

退出码：`0` 成功；`1` 业务或 IO 错误（含验证拒绝）；`2` 参数错误。

## 设计要点

- **字节序与归一化**：键值按原始 UTF-8 字节解释与排序，长度即字节长度，
  不做任何 Unicode 归一化（NFC/NFD 的同一视觉字符被视为不同键）。
- **外排构建**：记录流式写入 SQLite WITHOUT ROWID 表，B 树排序 + 窗口函数
  分配原始叶下标；主键约束检测重复键，构建在 `.tmp` 上完成后原子替换。
- **补齐规则**：非空数据补齐到最小二次幂，补齐叶固定 `SHA256(0x03)`；
  空数据根固定 `SHA256(0x02)`，不生成任何节点。
- **缺失证明**：给出严格相邻的前驱/后继叶及其 Merkle 路径；边界缺失一侧
  允许为空并强制边界下标；`successor.index = predecessor.index + 1` 结合
  清单记录数证明中间不存在遗漏叶。
- **共享去重**：证明节点表是所有披露叶路径上兄弟坐标的去重并集，
  以 `(level, index)` 标识；验证方推导同一坐标集合并要求节点表恰好匹配，
  冗余节点、缺节点、根节点入证一律拒绝。
- **信任边界**：根清单是带外交付的可信输入，证明无法替换它；验证器不打开
  数据集或树。结论仅在“清单可信 + SHA256 抗碰撞”假设下成立。

## 测试覆盖

`tests/` 使用与实现独立的 Python 排序 + `bisect` 参照集交叉核对答案，覆盖：
空集、单叶、1/2/3/7/8/9 条、非二次幂补齐、最小键之前、最大键之后、
相邻键之间、中文键与 NFC/NFD 不归一化、大批量路径共享；并主动篡改值、
叶下标（含指向补齐叶）、记录数（证明侧与清单侧）、前后驱次序与邻接、
内部摘要、根摘要，以及追加无用/重复节点、删除必要节点、根节点入证、
查询集合不匹配等，全部要求验证拒绝；另含直接篡改 SQLite 后加载期重算拒绝。

## 环境

仅需 Python 3.11 标准库（含 SQLite），无需安装依赖、无需 PostgreSQL。
原生环境初始化示例见 [environment/README.md](environment/README.md)；
数据库、日志及临时验证数据统一放在 `.runtime/`（已在 `.gitignore` 忽略）。
