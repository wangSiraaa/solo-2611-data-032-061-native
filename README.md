# 输入素材

accounts.bin 是带四字节总长度前缀的 EBCDIC/packed-decimal 记录，布局由 account.cpy 描述。没有提供解码后的行表、字段偏移表或转换程序。encoding.json 声明存储约定。

本目录只包含输入和运行依赖声明，不含求解器、预期结果或验收答案。程序需对同一声明子集内的其他有效输入工作，不能硬编码样例。

# 实现：`cobol_arrow` 包

受限布局编译器 + Arrow/Parquet 转换器。Python 3.11，依赖 PyArrow（见 requirements.txt；运行测试另需 pytest）。

## 支持的 copybook 子集

- 一个 `01` 根组，嵌套级号 02–49（可跳级，如 `05` 下直接 `15`）
- `FILLER`（基本项或组；占存储、不进 schema、不可按路径寻址）
- `PIC X(n)`、`PIC 9(n)`（无符号 DISPLAY 整数）、`PIC S9(n)[V9(m)] COMP-3`（有符号压缩十进制、隐含小数）
- 固定 `OCCURS k TIMES`（基本项或组均可）
- 恰好一个位于记录末尾的 `OCCURS 0 TO n TIMES DEPENDING ON <计数>`；计数字段必须是其前方可见的、不在任何 OCCURS 内的无符号整数，可用 `OF`/`IN` 限定
- `*` 注释行、`*>` 行尾注释、跨行声明

其余语法（REDEFINES、SIGN/overpunch、其他 USAGE、VALUE、66/77/88 级等）在编译期带行列号拒绝。级号错误、重名限定路径、计数前向引用/二义、动态区域不在末尾或嵌在 OCCURS 内，也都在编译期拒绝。

## 命令

```sh
# 编译并输出 explain-layout（加 --json 输出可机器检查的布局：字段树/长度/计数依赖/路径）
python -m cobol_arrow compile --copybook fixtures/account.cpy [--json]

# 转换：好记录写 --out，坏记录写拒绝表（默认 <out 去后缀>.rejects.parquet）
python -m cobol_arrow convert --copybook fixtures/account.cpy \
    --input fixtures/accounts.bin --encoding fixtures/encoding.json \
    --out out/accounts.parquet

# 按记录号（0 起）和字段路径查看原始字节与解码过程；重复字段用 1 起下标
python -m cobol_arrow explain-record --copybook fixtures/account.cpy \
    --input fixtures/accounts.bin --record 0 --field 'DETAIL-LINES[2].PRICE'
```

`--encoding` 可省略，缺省值与 fixtures/encoding.json 相同。

## 存储与解码规则

- 记录 = 长度前缀 + 数据；前缀按 encoding.json（4 字节、大端、含自身）。先校验前缀 ≤ max_record_bytes 再分配/读取，截断只产生一条拒绝、不产出半条记录；前缀无法重新同步时停止扫描。
- 字符与 DISPLAY 数字按 cp037 解码；DISPLAY 每位必须是数字，否则拒绝。
- COMP-3 严格校验：数字半字节须 ≤ 9，符号半字节只接受 C/F（正）与 D（负）；由半字节直接构造 `Decimal`，不经过 float。
- 动态尾部：实际记录长 = 固定区 + 次数 × 项长，必须整除、次数 ≤ 上限，且与计数字段的值一致，否则整条拒绝。
- 拒绝表列：`record_index, file_offset, field_path, error_code, message, record_hex`；`field_path` 为限定路径（数组带 1 起下标），记录级错误为 NULL。

## Arrow 类型映射

- `X(n)` → `string`（定长原文，保留尾部空格）；`9(n)` → `int64`（n ≤ 18，否则 `decimal128(n,0)`）
- `S9(a)V9(m) COMP-3` → `decimal128(a+m, m)`；组 → `struct`；固定 OCCURS → `fixed_size_list`；ODO → `list`
- FILLER 组的名字被跳过，其子字段提升到父 struct

## 测试

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest tests/ -q
```

测试用独立编码逻辑生成零次/最大次数/负值/坏数字半字节/坏符号/计数不符/过短/截断/坏前缀等样例，回读核对字段位置与值；再用独立 Arrow 消费者（pyarrow.parquet）检查产物类型、值与拒绝数量。
