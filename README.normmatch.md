# normmatch —— 大型 UTF-8 文本规范化关键词流式匹配

纯 Python 3.11 标准库实现，无第三方依赖、无外部服务。在远大于内存的
UTF-8 文本上，按统一规范化规则一次扫描匹配至多 1000 个关键词，输出
全局有序的命中及可回读原文件的字节证据区间，内存占用与文件大小无关。

## 规则摘要（详见 `python -m normmatch rules`）

逐**原始 Unicode 码点**执行：`NFKD → casefold → 删除 Mn/Mc/Me →
isspace 统一为 ASCII 空格 → 连续空格折叠为一个（保留首尾空格）`。
关键词用同一规则，规范化后为空即配置失败。

* 严格 UTF-8 流式解码：非法字节报告**绝对字节偏移**并使本文件失败，
  绝不使用替换字符伪造定位；仅块边界结构不完整的序列允许跨块等待。
* Aho-Corasick 一次扫描匹配全部关键词，允许重叠；不同 `pattern_id`
  规范化为同形词时分别报告；一个原始码点展开出多个规范化字符时
  共享同一原始字节区间，可在同一字节区间产生多个命中，全部保留。
* 每条结果含 `pattern_id`、规范化起止码点下标（左闭右开）和原文件
  字节区间 `[byte_start, byte_end)`；被折叠的整段空白作为该空格的
  完整来源；夹在命中之间被删除的重音自然落入连续区间，命中前后的
  删除字符不扩张区间。
* 结果按 `(norm_start, norm_end, pattern_id)` 排序，写临时文件后
  `os.replace` 原子发布；失败清理临时文件，不留半成品。

## 使用

```bash
# 1. 生成测试语料、关键词、非法 UTF-8 样本与 >128MiB 合成文本
python -m normmatch generate ./.runtime/data

# 2. 匹配（JSON Lines 原子落盘；--chunk-size 默认 1MiB）
python -m normmatch match INPUT.txt keywords.json OUTPUT.jsonl [--chunk-size N]

# 3. 全量验证（朴素参考核对、分块一致、逐条证据、非法输入、峰值 RSS）
python -m normmatch verify ./.runtime/data

# 4. 规则说明
python -m normmatch rules
```

关键词配置格式：

```json
{"patterns": [
  {"pattern_id": "german", "keyword": "STRASSE"},
  {"pattern_id": "german_dup", "keyword": "strasse"}
]}
```

输出行示例：

```json
{"pattern_id": "german", "norm_start": 12, "norm_end": 19, "byte_start": 40, "byte_end": 47}
```

退出码：`0` 成功；`2` 配置失败；`3` 非法 UTF-8；`4` 输入文件缺失；
`1` 验证套件发现不一致。

## 内存模型（为何与文件大小无关）

* 解码器只保留块边界处不完整序列（至多 4 字节）与当前块；
* 规范化器只保留当前空格折叠段；
* 来源环形缓冲只保留最近 `max_len+1` 个规范化位置的字节区间
  （`max_len ≤ 4000`）；命中起点桶同样在落后 `max_len` 后立即刷盘，
  不保存全部命中、全文或全部规范化文本；
* 自动机规模受关键词规范化总长（≤100000）约束，是有限状态结构。

验证器对大文件采用流式逐行比对与逐命中顺序证据核查；峰值 RSS 由
子进程 `RUSAGE_SELF.ru_maxrss` 自报（Linux 单位 KiB），只统计进程
驻留物理页，内核页缓存不计入。参考实测：140 MiB 文本峰值约 19 MiB。

## 验证覆盖

分块长度 1/2/3/7/4096/1MiB 结果逐字节一致；独立朴素匹配器
（一次性规范化 + 保留逐码点来源 + 朴素子串扫描）逐条核对；每条命中
实际回读字节区间核查严格可解码且来源边界精确覆盖；覆盖德语 ß/ẞ
展开、预组合与分解重音、全角字符、中文、多空白折叠与首尾保留、
空/重复/结构非法配置、重叠词、同形不同编号、同一来源字节多命中、
Mn/Mc/Me 标记的区间规则，以及 >128MiB 文本的跨块重叠与峰值 RSS。

## 模块

| 模块 | 职责 |
| --- | --- |
| `normalize.py` | 逐码点规范化、空格折叠与逐输出来源 |
| `codec.py` | 严格 UTF-8 流式解码与非法绝对偏移 |
| `automaton.py` | Aho-Corasick 多模式自动机 |
| `engine.py` | 有界来源环形缓冲、起点桶排序、原子落盘 |
| `config.py` | 关键词配置加载与上限校验 |
| `naive.py` | 独立朴素参考匹配器（仅验证用） |
| `generate.py` | 测试数据生成器 |
| `verify.py` | 端到端验证与 RSS 测量 |
| `cli.py` / `__main__.py` | 命令行入口 |
