# 离线流重组器（reasm.py）

按到达顺序采集的日志片段可能乱序、重复、重叠。`reasm.py` 在不访问网络的前提下，
离线重建每个 flow 的连续字节区间，并明确指出无法恢复的位置。仅依赖 Python 3.11
标准库（SQLite 内置），所有数据仅在本地生成。

## 数据定义

- **配置 `config.json`**：`{"flows": [{"flow_id", "base_seq", "total_length"}, ...]}`。
  `base_seq` 为 32 位无符号起始序号；`total_length` 为期望字节长度，`0 ≤ total_length < 2^31`。
- **输入 `segments.jsonl`**：每行一个 JSON 对象，字段恰好为
  `segment_id`（全局唯一）、`flow_id`、`seq`（32 位无符号）、`payload`（严格十六进制，
  偶数长度、无空格/前缀，解码后 ≤ 64 KiB）。空 payload 合法，但不覆盖任何位置。
- 片段起始相对偏移 `offset = (seq - base_seq) mod 2^32`，必须 `< 2^31`，且整个片段
  `[offset, offset+len)` 落在 `[0, total_length)` 内。回绕次数只由该公式决定，
  绝不按到达顺序猜测。

## 分类与输出

每个字节位置被分为三类之一：

| 类别 | 含义 |
|---|---|
| `determined` | 被覆盖且所有覆盖字节一致（唯一可确定） |
| `gap` | 从未被任何片段覆盖 |
| `conflict` | 被覆盖但至少出现两个不同字节 |

相同位置收到相同字节是重复覆盖，不增加重组长度；一旦出现不同字节，该位置永久
标记为冲突，并保留全部涉及片段编号与各不同字节值（不选择先到或后到版本）。

每次成功 `assemble` 以**内容寻址版本**原子发布：

```
<out>/
  current -> versions/<manifest 内容 sha256>
  versions/<id>/
    manifest.json      # 每 flow：三类计数、最大连续确定区间/缺口/冲突区间、
                       # 确定区间清单（绝对相对偏移 offset、length、sha256、文件）、
                       # complete 标志、全部输出文件的 sha256
    segments.jsonl     # 逐片段：sole_covered / shared_covered / conflict_covered
    conflicts.jsonl    # 逐冲突位置：values、segments、detail（[segment_id, 字节]）
    files/             # 每个确定区间一个独立 .bin；完整时另有 f<N>_complete.bin
```

- 确定区间写成独立二进制文件，清单保留绝对相对偏移与 SHA256；连续同类位置已合并，
  不同 flow 不混合。
- 仅当整个期望范围都确定时生成完整流文件并 `complete=true`；否则 `complete=false`，
  绝不用零填洞冒充完整数据。
- 逐片段三项统计（仅本片覆盖 / 与他人相同内容共同覆盖 / 参与冲突）之和等于其负载
  长度；统计基于全部输入集合的最终状态，与处理顺序无关。
- 同一 `segment_id` 重复、负载非法、越界等属于**整次输入错误**：退出码 2，
  不发布任何部分结果。写入失败（退出码 1）同样不会替换旧成功版本——所有输出先在
  `<out>/.build-*` 暂存，全部成功后才原子改名并通过符号链接切换 `current`。
- 版本 id 是清单内容的哈希：同一输入（任意打乱行序）必然得到同一版本。

## 存储与内存

工作状态全部在临时 SQLite 库中：已覆盖位置按 64 KiB 页存储（每位置 1 字节状态 +
1 字节值），页只在有覆盖处存在；片段流式读取、逐段落盘，内存中只保留一个有界页
缓存（32 页）。因此磁盘与内存开销随**实际覆盖量**变化，而不是随 `total_length`。
实测：期望长度 2 GiB 的稀疏流（64 个 1 KiB 片段）峰值 RSS ≈ 26 MiB，发布版本
≈ 91 KiB；80 MiB 真实负载峰值 RSS ≈ 27 MiB。

## 运行说明

```bash
# 1. 生成样例 / 压力输入
python3 reasm.py generate --scenario small   --out gen/small
python3 reasm.py generate --scenario invalid --out gen/invalid
python3 reasm.py generate --scenario sparse  --out gen/sparse \
        --seed 5 --length 2147483647 --segments 64 --size 1024
python3 reasm.py generate --scenario big     --out gen/big --seed 7 --length 83886080

# 2. 重组并原子发布（退出码：0 成功；2 输入错误；1 其他失败）
python3 reasm.py assemble --config gen/small/config.json \
        --segments gen/small/segments.jsonl --out out/small

# 3. 查看已发布版本（摘要 / 单片段 / 冲突 / 机读 JSON）
python3 reasm.py inspect --out out/small
python3 reasm.py inspect --out out/small --flow conflict3
python3 reasm.py inspect --out out/small --segment c1
python3 reasm.py inspect --out out/small --conflicts --flow conflict3 --limit 10
python3 reasm.py inspect --out out/small --json

# 4. 校验：文件哈希 + 内部一致性 + 独立逐字节字典参考模型
#    （大输入默认跳过逐字节模型，可用 --deep / --force 强制；退出码 3 表示校验失败）
python3 reasm.py verify --out out/small --config gen/small/config.json \
        --segments gen/small/segments.jsonl --deep

# 5. 全部端到端测试（小样例逐字节核对、打乱一致性、非法输入拒绝、
#    稀疏大流与 >64 MiB 实测峰值 RSS）
python3 run_tests.py
```

## 实现要点

- **三遍流水线**：① 流式校验并入库存片段、用大整数位运算向量化地把负载折叠进页
  状态机（未覆盖→独占→共享→冲突）；② 基于**最终**页状态统计逐片段贡献并收集冲突
  明细（顺序无关）；③ 按页序流式归并同类连续区间，边扫描边写确定区间文件并增量
  计算 SHA256。
- **原子发布**：暂存目录构建 → fsync → `rename` 到 `versions/<内容哈希>` → 临时符号
  链接 `os.replace` 切换 `current`。任何一步失败仅清理暂存，旧版本不受影响。
- **确定性**：清单为规范化 JSON（键排序、无时间戳），`segments.jsonl` 按
  `segment_id` 排序、`conflicts.jsonl` 按 `(flow_id, offset)` 排序，因此输入任意
  打乱产生逐字节一致的发布版本。
