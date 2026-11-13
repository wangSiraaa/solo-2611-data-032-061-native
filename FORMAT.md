# 交付格式说明

全部文件均为本机文件，UTF-8 编码，无任何网络依赖。哈希固定 SHA256，
摘要按原始字节解释，不做 Unicode 归一化。

## 1. 输入 JSONL

- 每行一个 JSON 对象，字段恰好为 `key`、`value`，二者均为字符串。
- `key` 非空；单字段 UTF-8 字节长度 ≤ 64 KiB（65536 字节，恰好等长合法）。
- 不允许空行、重复键、额外字段或非字符串字段；任一非法行使整次构建失败，
  且不会留下树数据库产物（先写 `.tmp`，成功后原子替换）。

## 2. 哈希与树结构

记 `u32be(n)` 为 n 的 4 字节大端编码：

| 对象 | 构造 |
| --- | --- |
| 叶摘要 | `SHA256(0x00 ‖ u32be(len(key)) ‖ key ‖ u32be(len(value)) ‖ value)` |
| 内部节点 | `SHA256(0x01 ‖ left(32B) ‖ right(32B))`，左右次序固定 |
| 补齐叶（常量） | `SHA256(0x03)` |
| 空数据根（常量） | `SHA256(0x02)` |

- 叶子按键的 UTF-8 字节序严格升序排列，原始叶下标从 0 开始。
- 非空数据补齐到不小于记录数的最小二次幂叶数；补齐叶不是真实记录，
  其下标永远 ≥ `record_count`，证明与验证均不得把它当作真实叶披露。
- 层号：叶层为 0，根在最高层（树高 = log2(补齐后叶数)，单叶树高为 0）。

## 3. SQLite 树数据库（`build --db`）

WITHOUT ROWID 表，BLOB 列使用 BINARY 排序规则（原始字节序）：

- `staging(key PRIMARY KEY, value)`：构建暂存，主键冲突即重复键。
- `records(ord PRIMARY KEY, key UNIQUE, value)`：排序迁移后的正式表，
  `ord` 由窗口函数 `ROW_NUMBER() OVER (ORDER BY key) - 1` 一次性生成。
- `nodes(level, idx, digest, PRIMARY KEY(level, idx))`：全树摘要，支持随机访问。
- `meta(key, value)`：`format_version / record_count / leaf_count / height / root_hex`。

加载（`load_tree` / `inspect --db` / `prove`）时执行完整重算核验：
元数据取值、记录数、连续下标、严格字节序、叶摘要（含补齐常量）、
逐层内部摘要直到根；提供 `--manifest` 时再比对版本/记录数/根三元组。
文件存在本身不构成接受理由。

## 4. 可信根清单（`build --manifest`）

```json
{
  "type": "ordered-merkle-manifest",
  "format_version": 1,
  "record_count": 100000,
  "root_hex": "<64 个十六进制字符>"
}
```

- 清单由带外渠道单独交付给验证者，是已知可信输入。
- 证明文件不含根摘要字段，无法替换清单；证明自带的 `record_count`
  必须与清单相等，否则验证拒绝。
- 空数据集清单的根固定为 `SHA256(0x02)`，读写两侧都强制校验。

## 5. 证明文件（`prove --output`）

```json
{
  "type": "ordered-merkle-proof",
  "format_version": 1,
  "record_count": 100000,
  "height": 17,
  "answers": [
    {"query": "k", "present": true,  "index": 3, "key": "k", "value": "v"},
    {"query": "q", "present": false,
     "predecessor": null | {"index": 2, "key": "..", "value": ".."},
     "successor":   null | {"index": 3, "key": "..", "value": ".."}}
  ],
  "nodes": [{"level": 0, "index": 1, "digest_hex": "<64 hex>"}, ...]
}
```

- 答案顺序与查询集合一一对应；存在答案给出键值与真实叶下标。
- 缺失答案给出严格相邻的前驱/后继；边界缺失允许一侧为 `null`，
  此时另一侧必须分别是下标 0（最小键之前）或 `record_count-1`（最大键之后）。
- 两侧都存在时要求 `successor.index == predecessor.index + 1`，
  结合记录数即可证明两片真实叶之间没有遗漏叶。
- `nodes` 是全部披露叶到根路径上**兄弟节点**的去重并集，按
  `(level, index)` 标识并排序；多查询共享的上层节点只保存一次。
  根节点（`level == height`）永远不在证明内。

## 6. 验证器信任边界与检查清单

`verify` 只打开三个文件：可信清单、原查询集合、证明；不打开数据集或树。
检查顺序：

1. 信封类型/版本；证明 `record_count`、`height` 必须与清单推导一致。
2. 查询集合非空且互不重复；答案集合与查询集合完全相等（不多不少、不重复）。
3. 空数据集：只允许双侧为 `null` 的缺失结论且节点表为空。
4. 每个答案：字段集合、布尔标记、下标落在 `[0, record_count)`
   （补齐叶下标被拒）、键与查询的严格字节序关系、前后驱次序与邻接、
   边界下标规则。
5. 全部披露叶的下标序与键字节序严格同构；同一下标不允许映射不同键。
6. 按路径规则推导所需兄弟坐标集合，节点表必须**恰好**等于它：
   缺节点、重复节点、越界节点、根节点入证、任何未使用冗余节点均拒绝。
7. 从每个披露叶重算叶摘要并沿兄弟节点重建到根，与可信根比较。

安全结论仅在“清单可信 + SHA256 抗碰撞”的假设下成立；本工具不接入网络服务。

## 7. 体积口径

- `proof_byte_size`：证明按落盘序列化方式计算的 UTF-8 字节数。
- `shared_node_count`：去重后的共享节点数。
- `naive_path_digest_count`：假设为每条披露叶机械复制整条路径时的
  32 字节摘要总数（= 披露叶数 × 树高），仅作对照，不要求固定压缩比例。
