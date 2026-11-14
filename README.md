# 语音片段生产 CLI（题 133）

把已有的 16 kHz / 16 位有符号 PCM WAV 与讲话区间标注，规划并产出适合人工复核的片段、片段清单和局部标注。

- 只读原音频，**不识别、不降噪、不重采样、不改声道数**；单声道输入产出单声道，双声道输入产出双声道。
- 只支持 16 kHz、16 位有符号 PCM、单声道或双声道；其他 WAV 直接报错。
- 仅使用 Python 3.11 标准库（`wave`、`Decimal`、`json`、`argparse`）。NumPy 可选，未使用。
- 标注区间按 **左闭右开** `[start, end)` 解释。

## 快速开始

```sh
# 1) 校验标注并生成计划（同时写出被拒记录）
python3 -m clipgen plan \
  --annotations fixtures/annotations.jsonl \
  --config fixtures/config.json \
  --recordings fixtures \
  --plan out/plan.json
#    被拒记录默认写到 out/plan.json.rejected.jsonl，可用 --rejected 指定

# 2) 按计划产出 WAV、清单和局部标注
python3 -m clipgen build --plan out/plan.json --recordings fixtures --out out/build

# 3) 独立核对：逐段按源采样区间比对字节，并按区间交集核对标注覆盖
python3 -m clipgen verify --plan out/plan.json --recordings fixtures --out out/build
```

只跑测试与样例端到端核对（不使用 `numpy`/`pytest`）：

```sh
python3 -m unittest discover -s tests      # 26 个单元测试
python3 scripts/run_fixtures.py            # 样例 plan→build→verify + 独立字节/覆盖核对
```

## 配置（config.json）

```json
{
  "max_clip_seconds": "3.0",
  "merge_silence_seconds": "0.25",
  "context_seconds": "0.10"
}
```

字段都是十进制秒字符串，与标注时间使用同一套「精确十进制 × 16000 → 四舍五入到整数采样」规则。
未知字段、缺失字段、非法十进制（负号、指数、多个小数点、空串）都会导致 `plan` 失败。

## 时间 → 采样边界与舍入规则

- 每个时间字符串都用 `Decimal` 解析，乘以精确的 `16000`，再一次性 **四舍五入（ROUND_HALF_UP）** 到最近采样边界。
- **不做浮点累加**：不存在「前一秒浮点误差累积到后段」的问题；任何位置的起止都独立计算，后段不会错位。
- 恰好落在 0.5 采样处时一律向上（例如 `0.00003125 s = 0.5` 采样 → 1 号采样）。
- 计划、清单里同时记录十进制秒与整数采样号；规则原文写在 `plan.json` 的 `rounding` 字段。

示例（fixture 中）：`0.10003 × 16000 = 1600.48 → 1600`；`7.70003 × 16000 = 123200.48 → 123200`；`1.20000 × 16000 = 19200`。

## 标注校验与被拒诊断

标注每行一个 JSON 对象，必需字段：`recording`、`annotation_id`、`speaker`、`start`、`end`。

逐行诊断，**原行文本原样保留**到被拒记录 JSONL，附 `line`、`code`、`reason`：

| code | 含义 |
| --- | --- |
| `invalid_json` / `invalid_record` | 非 JSON / 非对象 |
| `missing_field` / `invalid_field_type` | 缺字段 / 字段不是非空字符串 |
| `invalid_recording_name` | 录音名不是录音目录内的纯文件名（拒绝路径穿越） |
| `missing_recording` / `invalid_recording` | 文件不存在 / 不是受支持的 16 kHz 16 位 PCM 单/双声道 WAV |
| `invalid_time` | 时间不是合法十进制秒字符串 |
| `invalid_range` | start 映射后晚于 end |
| `zero_duration` | 映射到空采样区间 `[n, n)` |
| `out_of_range` | 采样区间超出录音长度（**不裁剪**，直接拒绝） |
| `duplicate_annotation_id` | 同一 ID（**跨录音全局**）出现多次；**所有出现都被拒绝**，故结果不依赖行顺序 |

> 关于越界与末端的取舍：题面要求「越界给原行诊断」，因此超出末端的区间按原样拒绝，而不是静默截断到录音末尾。fixture 的 `bad-range`（4.5–5.2 s，录音 5 s）即触发此诊断。

## 切片规划

规划只使用通过校验的标注，只做整数采样运算，且先按内容排序再规划，**因此不依赖标注输入顺序**（对照见 `tests` 中的 `test_order_independence`）。

1. **保留重叠**：不同说话人可重叠；先把所有标注取并集，重叠区间合并、互不删除。重叠讲话若落在同一片段，就在该片段的局部标注里保留多条记录。
2. **按配置合并短静音**：相邻并集块之间静音 `≤ merge_silence_seconds` 时连成一个「连续讲话段」。
3. **切分连续讲话段**（每段长 `> max_clip` 时才切）：
   - 候选切点 = 段内所有**标注边界**（标注起点/终点）与**短静音中点**；
   - 优先取「不超过最大长度且至少填充半个片段」的**最靠后自然边界**；
   - 若没有可用自然边界（例如单条长标注内部无边界），才在精确最大采样数处切（`max_length`）；若这样会留下退化尾片，则在剩余区间均分（`balanced_tail`）。
   - 每次切分都记录 `cut_reason`（`annotation_boundary` / `silence_midpoint` / `max_length` / `balanced_tail` / `sliver_guard`）与 `cut_detail`，以及切点落在哪些标注内部（`cut_inside_annotation_ids`）。
4. **上下文**：每个基片段向两侧扩展 `context_seconds`，但被限制在：
   - 录音 `[0, nframes)` 内；
   - 与相邻片段基区间之间的**中点**处，保证相邻片段的输出**永不重叠**（即同一原始采样不会分配给两个片段）。
5. **超长标注**：允许切开，但每个片段内的局部标注带 `part_index` / `part_count` / `continuation` / `continues_in_next`；片段清单标 `is_continuation`、`split_annotation_ids`、`continuation_annotation_ids`。计划顶层 `continuations` 说明切分理由。

不变量在生成计划时和 `build` 读取计划时都会检查（`assert_plan_invariants`）：片段 ID 唯一、区间合法、`base ≤ output`、单片段基区间不超上限、同录音输出互不重叠、每条标注被片段**无缝且完全**覆盖、每条局部标注等于真实交集。

## 输出

`build` 在 `--out` 下产出：

- `clips/<clip_id>.wav`：原始声道数的 PCM WAV，字节就是源 WAV 对应采样区间的连续字节。
- `manifest.json`：每段的 `recording`、精确 `start_sample`/`end_sample`（左闭右开）、帧数、基础区间、上下文采样数、`nchannels` 与 `channel_layout`、`sample_width`、`sample_rate`、切分原因、是否续片。
- `local_annotations.jsonl`：每条 = 原 `annotation_id` + 原 `speaker` + 片段内 `[start_sample, end_sample)` + 源区间 + 片段区间 + 续片元数据。**同一片段内的重叠标注会同时出现多条。**
- `build-status.json`：`building` / `ok` / `failed`。

构建采用暂存目录，全部 WAV 成功后才原子替换；构建前先删除旧 `manifest.json`，**失败时不会把旧文件冒充成本次成功产物**，并以非零退出码结束。

## 可复现性与顺序无关

- 计划中不含行号（行号只属于被拒记录），避免同一批标注换个行序就得到不同计划。
- `annotation_id` 冲突采用「全部拒绝」而非「先到先得」。
- 片段 ID 由录音文件名派生（多名冲突时加哈希后缀），再按起点的 4 位序号命名；同一录音内唯一。

## 局限（按题面要求省略）

- 不做语料版本平台、播放器与大规模音频性能基准。
- 不做识别、降噪、增益、重采样、声道转换。
- 仅接受 16 kHz / 16 位 / 单或双声道 PCM WAV。
