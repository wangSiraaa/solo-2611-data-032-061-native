# bdelta — 纯标准库离线差量工具

仅依赖 **Python 3.11 标准库**（`sqlite3 / zlib / hashlib / base64 / json` 等），
在离线环境下对大文件生成可严格验证的 `COPY` / `ADD` 补丁，并能流式重建、
原子发布。所有测试数据均由本仓库代码本地合成，不访问网络。

## 1. 命令一览

```bash
python -m bdelta signature --old 旧文件 --sig 签名.sqlite [--block-size B]
python -m bdelta delta     --sig 签名.sqlite --new 新文件 --patch 补丁.patch
python -m bdelta apply     --old 旧文件 --patch 补丁.patch --out 目标文件
python -m bdelta verify    --old 旧文件 --patch 补丁.patch --new 期望目标
python -m bdelta generate  {corpus|large} --dir 目录 [--block-size B] [--size N]
```

- 块大小 `B` 允许范围 **[256, 65536]** 字节，越界直接失败。
- 每个子命令把统计打印为单行 JSON；失败走 stderr 并返回非零退出码。
- 一键测试与大文件演示：`./run_all.sh`（产物全部在 `.runtime/`，已被 git 忽略）。

## 2. 签名（signature）

旧文件按 B 切分，**只有长度恰好为 B 的完整块进入复用候选**；不足 B 的末块
不入库，但旧文件的完整大小与整体 SHA256 仍记录在签名中。

签名是一个 SQLite 文件：

| 表 | 内容 |
| --- | --- |
| `meta` | `version / block_size / old_size` |
| `meta_text` | `old_sha256`（旧文件整体摘要） |
| `blocks` | 每个完整块一行：`offset, length, adler32, sha256`，并在 `adler32` 上建索引 |

弱校验（Adler32）的候选查找走 SQLite 落盘索引，块数很多时也不需要把
索引整体放入内存。签名先写临时库再 `os.replace`，不会留下半截文件。

## 3. delta 的确定性贪心规则（不追求全局最小）

delta **只读取签名与新文件，不读取旧文件正文**（测试
`test_delta_never_reads_old_file_body` 在签名后删除旧文件，delta 仍成功）。

从新文件当前位置开始，维护长度恒为 B 的滑动窗口：

1. 窗口每移动 **一个字节**，用 O(1) 滚动公式更新 Adler32，
   绝不为每步重算整个窗口（`bdelta/checksums.py`，并有与 `zlib.adler32`
   逐窗口比对的测试，含取模回绕场景）；
2. 弱值在 SQLite 索引中命中后，先用 `zlib.adler32` 独立复核弱值，
   再用 **SHA256 强摘要**逐候选核对——弱值相等绝不直接复用；
3. 一个窗口有多个候选块时，候选按旧偏移升序返回，**选择旧偏移最小者**；
4. 命中：输出 `COPY(旧偏移, B)` 并前进 B 字节；
   未命中：当前字节并入 `ADD` 并前进 1 字节；
5. 末尾不足 B 的字节全部作为 `ADD`。

这是题面固定的**确定性贪心**规则：同样的输入必然产生字节相同的补丁
（有专门的确定性测试），但它**不保证补丁全局最小**，也不做跨块的最优
排布搜索。实际处理字节数与候选强校验次数都在 delta 的 JSON 统计中报告
（`bytes_scanned`、`strong_checks`、`weak_hits`）。

### 指令合并规则（保证序列确定）

- 相邻 `ADD` 合并为一条逻辑 ADD；逻辑 ADD 可拆成多个物理数据块，
  每块解码后 **不超过 64 KiB**，以支持流式读写；
- 相邻 `COPY` 仅当它们在新流中位置相邻、且引用的旧偏移也连续时才合并；
  重复旧块被连续复用时旧偏移不连续，会保留为独立 COPY。

## 4. 补丁格式（JSON Lines, UTF-8）

第 1 行头部，之后每行一条指令，最后一行必须是 `{"op":"END"}`：

```json
{"format":"bdelta-patch","version":1,"block_size":4096,"old_size":..,"old_sha256":"..","new_size":..,"new_sha256":".."}
{"op":"COPY","old_offset":12288,"length":4096}
{"op":"ADD","data_b64":"严格 base64"}
{"op":"END"}
```

- ADD 数据使用**严格 base64**：读取时 `validate=True` 且重新编码必须与
  原文逐字符一致，省略填充等非规范写法被拒绝；
- COPY 的偏移/长度必须是非负整数、整体落在旧文件范围内；
- 任何零长指令（零长 COPY、零长 ADD）在读取阶段即被拒绝。

## 5. apply 的验证与原子发布

1. 先流式验证旧文件的大小与 SHA256 与补丁头部一致——
   **旧版本替换（内容不同的“旧文件”）在此被拒绝**；
2. 指令逐条写入与输出同目录的临时文件，COPY 直接从旧文件流式复制，
   全程不缓存整个旧文件或目标文件；
3. 写完后核对目标长度与 SHA256；
4. 只有全部成功才 `os.replace` 原子发布。任何失败都会删除临时文件，
   **此前已成功发布的文件不会被错误补丁覆盖**（有专门测试验证）。

`verify` 额外把重建结果与给定目标做流式逐字节比对。

## 6. 冻结输入要求

`signature` 与 `delta` 通过 `FrozenFile` 包装输入：每次读取前后用
`fstat` 比对文件大小与纳秒修改时间（`st_mtime_ns`），扫描期间任何变化
都会抛出 `InputChangedError` 并失败。测试用后台线程持续追加内容 /
`utime` 触碰文件来验证这两条失败路径。

> 仅靠 stat 无法区分“两次检查之间被改写但大小、mtime_ns 恰好不变”的
> 极端竞态，因此规范同时要求**使用者提供冻结输入**（例如先把文件放到
> 专用目录、扫描期间不写入）。

## 7. 内存与实测指标（70 MiB，仅 39 字节插入，B=4096）

新文件按 1 MiB 流式读入，窗口缓冲有界；旧/新文件都不会被整体装入内存。
`tests/large_demo.py` 的一次实测：

| 指标 | 数值 |
| --- | --- |
| 旧/新文件大小 | 70 MiB / 70 MiB + 39 B |
| 补丁体积 | **658 字节** |
| 复用字节（COPY） | 73,400,320（复用率 0.999999） |
| ADD 字节 / 物理块 | 39 / 3 |
| 候选强校验次数 | 17,920 |
| 实际扫描字节数 | 73,400,359 |
| 进程峰值 RSS | **约 28.6 MiB**（delta 单阶段约 27 MiB） |
| 重建逐字节一致 | 是（另核对 SHA256） |

复现：`python -m tests.large_demo --dir .runtime/large3 --block-size 4096`。
正确性以逐字节重建与摘要核对为准，**不以压缩率作为唯一判断**。

## 8. 测试覆盖（`tests/test_bdelta.py`，28 项）

- 头部插入一字节、中间插入与删除、重复旧块并列（选最小旧偏移）、
  完全不同、空文件（旧空/新空/双空）、B 边界与尾块不足；
- 连续 COPY 合并、非连续 COPY 保持独立、末块不进入复用候选；
- **独立补丁解释器**（`tests/independent.py`，不 import 生产 apply/patch
  代码）逐字节重建并核对每条 COPY 的真实来源；
- **Adler32 碰撞**：四点代数构造（`sum(d)=0` 且 `sum(i·d_i)=0`）得到
  弱值相同、SHA256 不同的两个块，证明强校验拒绝错误复用；另附生日悖论
  搜索得到的天然碰撞；
- 破坏 ADD、越界 COPY、零长指令、非规范 base64、坏目标摘要、
  旧版本替换、坏签名版本都必须失败；
- 扫描期间输入增长 / mtime 变化必须失败；
- 大于 64 MiB 大文件端到端指标（`tests/large_demo.py`）。

## 9. 目录结构

```
bdelta/
  __init__.py     常量与版本
  checksums.py    滚动 Adler32、SHA256
  frozen.py       冻结输入检查
  signature.py    SQLite 签名构建/读取
  patch.py        补丁格式、确定性写入与严格读取
  delta.py        滑动窗口贪心引擎
  apply.py        apply / verify、原子发布
  generator.py    本地合成测试数据
  cli.py          命令行入口
tests/
  independent.py  独立补丁解释器
  test_bdelta.py  单元/集成测试
  large_demo.py   大文件指标演示
run_all.sh        一键测试 + 演示
```
