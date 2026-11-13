# cachefetch — 本地 HTTP 模拟与 Vary 感知缓存采集器

纯 Python 3.11 标准库 + SQLite 实现。项目自带模拟 HTTP 源站、场景 JSON 生成器、
缓存采集 CLI 和自动验收；不访问外部网络、不需要账号或 API。

目标：离线采集时同一 URL 因 `Accept-Language` / `Accept` 返回不同内容。
实现按 **URL + Vary 规则 + Vary 取值** 隔离变体，既减少重复下载，也保证
一个变体的内容绝不会被交给另一个请求。

---

## 1. 快速开始

```sh
cd /workspace

# 生成场景 JSON（确定、可重复；仓库中已包含生成结果）
python3 -m cachefetch generate

# 启动模拟源站（--port 0 自动选端口；启动行打印实际地址）
python3 -m cachefetch serve --scenario scenarios/language.json

# 另开终端，用整数测试时钟采集
python3 -m cachefetch fetch --db .runtime/cache.db \
    --url http://127.0.0.1:PORT/text --time 100 \
    --header 'Accept-Language: en'

python3 -m cachefetch fetch --db .runtime/cache.db \
    --url http://127.0.0.1:PORT/text --time 101 \
    --header 'accept-language: zh-CN'

# 104 秒内再次请求同一变体：source=cache、requests=0，不访问服务
python3 -m cachefetch fetch --db .runtime/cache.db \
    --url http://127.0.0.1:PORT/text --time 104 \
    --header 'ACCEPT-LANGUAGE:   en  '

# 查看库中该 URL 的所有变体与 Vary 键
python3 -m cachefetch inspect --db .runtime/cache.db --all

# 一键自动验收（10 组检查，含进程中断/截断/协议错误）
python3 -m cachefetch verify
```

`fetch` 输出 JSON，关键字段：

| 字段 | 含义 |
| --- | --- |
| `source` | `network`（网络）、`cache`（新鲜缓存）、`revalidation`（304 重验证） |
| `requests` | 本次调用实际发出的 HTTP 请求数（0 或 1） |
| `body_sha256` | 返回给调用方的响应体 SHA-256 |
| `body_bytes` | 响应体字节数 |
| `network_status` | 实际网络状态码（`200`/`304`/错误码），缓存命中为 `null` |
| `stored` | 本次结果是否形成持久缓存 |
| `cache_variant` | 命中的 Vary 规则与规范化的 Vary 键 |
| `note` | 人类可读的决策说明 |

`--show-body` 会在 JSON 之后追加原始响应体字节，便于逐字节比对。

---

## 2. 命令

```
python3 -m cachefetch serve    --scenario FILE [--host H] [--port P]
python3 -m cachefetch fetch    --db DB --url URL --time NOW
                               [--header 'Name: Value']... [--header-json JSON]
                               [--timeout SEC] [--show-body]
python3 -m cachefetch inspect  --db DB (--url URL | --all)
python3 -m cachefetch generate [--output DIR]
python3 -m cachefetch verify   [--output DIR] [--keep-artifacts]
```

退出码：`0` 成功；`1` 协议/传输/服务端错误；`2` 用法错误。
错误以 `{"ok": false, "error": {"kind": ..., "message": ...}}` 输出。
`kind` 取值：`protocol_error`、`transport_error`、`server_error`、`usage_error`。

---

## 3. 协议边界（明确限定）

### 3.1 方法

* 只支持 `GET`。其他方法返回 `405`（`Allow: GET`）。
* 每个请求使用新连接，请求头自带 `Connection: close`，不实现重定向、
  压缩协商、HTTP/2 或持久连接。

### 3.2 变体键与 Vary

* `Vary` 只允许 `Accept-Language`、`Accept`（头名大小写不敏感），或单独的 `*`。
  其他头名、或 `*` 与其它字段混用 → **协议错误**，不写缓存。
* 头名大小写不敏感；值先去掉首尾 SP/HTAB，再**精确比较**；缺少头与空值
  等价为空字符串。
* 缓存键 = `(URL, Vary 规则规范形, Vary 头取值规范形)`。URL 只是变体组的名字，
  不是所有内容的唯一键。
* 同一 URL 的所有变体共享一个“当前 Vary 规则”。某次 200 的 Vary 规则与已存
  规则不同时，先在**同一个事务**里删除旧规则下的全部变体，再保存新规则下的
  变体。

### 3.3 新鲜度

* 只支持 `Cache-Control: max-age=<非负整数>` 与 `no-store`。
* 新鲜判定：`now < received_at + max-age`。`now` 是 `fetch --time` 传入的整数。
  **等于边界必须重验证**。
* 时间从成功接收 200 或 304 的时刻计算；`now` 由调用方提供，不使用真实时间
  等待来判定验收。
* 不实现 `Date`、`Age`、启发式缓存、`Expires`。
* 未知指令（如 `public`、`private`、`no-cache`、`must-revalidate`、`s-maxage`、
  `stale-while-revalidate`）**忽略**；已知但格式错误（如 `max-age=-1`、
  `max-age` 无值）→ 协议错误，不写缓存。

### 3.4 响应处理

| 情况 | 行为 |
| --- | --- |
| 新鲜命中 | 直接返回原响应体，`requests=0`，不访问服务 |
| 过期/无 `max-age` 的已有条目 | 带 `If-None-Match: <旧ETag>` 条件 GET |
| 200 | 校验强 ETag；原子保存“响应体 + ETag + Vary 规则 + 接收时钟 + max-age” |
| 304 | **必须**有可用旧条目；保留旧响应体；`Cache-Control` 带 `max-age` 则更新，否则沿用旧值；接收时钟更新为本次 |
| 304 的 ETag 与旧值不一致 | 协议错误，不修改缓存 |
| 304 提供了 Vary 且与旧规则不一致 | 协议错误，不修改缓存 |
| 304 未提供 Vary | 沿用旧规则 |
| 无旧条目的 304 | 协议错误，不创建缓存 |
| `no-store` | 不持久化，删除“本请求”对应的旧条目 |
| `Vary: *` | 不持久化；使该 URL 旧规则下所有变体失效并记录 `*` |
| 5xx | `server_error`；不写缓存，绝不把过期内容冒充新鲜成功 |
| 200 无强 ETag | 不作为缓存条目保存（本次仍返回 200 响应体），并删除本请求旧条目 |

传输层同样严格：响应中途截断、`Content-Length` 少于实际体（多出字节）、
超过声明长度、既无长度也非 chunked 等，一律失败且不改缓存。

### 3.5 存储与原子性

* SQLite，WAL + `synchronous=FULL`，所有写入在单个 `BEGIN IMMEDIATE`/`COMMIT`
  中完成；响应体以 BLOB 存储。
* 进程在 `COMMIT` 前被终止 → 完整旧状态保留；`COMMIT` 后终止 → 重试可读取
  完整新状态。
* 为验证这一点，`store._commit()` 支持测试钩子环境变量
  `CACHEFETCH_TEST_CRASH_POINT=before_commit|after_commit`（仅测试使用，
  `verify` 会调用；正常使用不设置）。

---

## 4. 场景 JSON 格式

```jsonc
{
  "name": "scenario-name",
  "resources": {
    "/path": {
      "content": "文字内容",            // 或 "content_b64": "base64..."
      "etag": "\"strong-tag\"",        // 必须是强 ETag（带引号、无 W/）
      "cache_control": "max-age=12, public",
      "vary": ["Accept-Language"],
      "variants": [
        {"match": {"Accept-Language": "zh-CN"}, "content": "...", "etag": "\"...\""}
      ],
      "steps": [
        {},                                        // 第 1 次请求使用当前表示
        {"cache_control": "max-age=20"},             // 后续持久生效
        {"content": "...", "etag": "\"...\""},
        {"truncate": 5},                             // 仅本次：截断响应体
        {"error": 500},                              // 仅本次：返回 5xx
        {"conditional": "etag_mismatch"},            // 仅本次：违规 304
        {"force_200": true}                          // 仅本次：即使条件命中也不回 304
      ]
    }
  }
}
```

* `steps` 按该资源收到的请求顺序消费；缓存命中不访问服务，因此不消耗 step。
* step 中的持久字段更新当前表示；瞬态字段只影响该次响应。
* `conditional` 取值：`force_200`、`no_entry`、`etag_mismatch`、`vary_mismatch`、
  `etag_absent`、`no_vary`、`no_cache_control`、`raw_bad_length`、`raw_extra_body`。
* 模拟服务另有只读观测端点 `GET /_requests`（可用 `?clear=1` 清空）和
  `GET /_health`，它们不会被当作普通资源，也不进入缓存逻辑。

`python3 -m cachefetch generate` 从 `cachefetch/generate.py` 重新生成
`scenarios/*.json`；输出是确定的，重复生成字节一致。

---

## 5. 自动验收覆盖

`python3 -m cachefetch verify` 会重建场景、为每组检查启动全新模拟服务与全新
SQLite 库，并同时断言：返回值、**服务端请求日志**（请求次数、是否带
`If-None-Match`、step 序号、服务端实际发出的字节摘要）以及**逐字节响应体**。

| 检查 | 覆盖点 |
| --- | --- |
| `language_variants_and_header_rules` | 两个语言变体隔离；头名大小写；值去空白后精确比较；缺失=空值；重启客户端后仍按缓存命中且内容不同 |
| `freshness_boundary_and_directives` | `now < received+max-age` 新鲜；恰好边界重验证；304 后时钟重置；未知指令忽略；`max-age=0` 永不新鲜；无 `max-age` 永远重验证 |
| `conditional_update_and_replacement` | 304 保留原体并更新有效期；不带新 max-age 沿用旧值；200 完整替换体/ETag/max-age |
| `vary_rule_switch` | Vary 规则切换时先使旧规则全部变体失效再保存；旧内容不可能再被返回 |
| `no_store_and_vary_star` | no-store / `Vary:*` 不留下持久条目并删除本请求旧条目；后续请求必须回源 |
| `bad_304_protocol` | ETag 不符、Vary 不符、无条目 304、304 缺 ETag 均报协议错误且缓存不变 |
| `broken_200_responses` | 中途截断、长度不符（少/多）、5xx 均不得成为成功缓存；不得用过期内容冒充新鲜 |
| `request_log_vs_bodies` | 客户端报告的次数/摘要与服务端请求日志和实际字节一致 |
| `interrupted_commit` | COMMIT 前终止保留完整旧状态；COMMIT 后终止重试读到完整新状态 |
| `generated_scenarios` | 所有场景由项目生成且可加载 |

输出为 JSON；`ok` 为 `true` 时退出码 0，否则非 0 并列出每项失败原因。
验收产物（各检查的 SQLite 库）默认写入 `.runtime/verify/`（已被 `.gitignore`
忽略），可作为证据复查。

---

## 6. 目录

```
cachefetch/
  __main__.py        python -m cachefetch 入口
  cli.py             serve / fetch / inspect / generate / verify
  headers.py         Vary、Cache-Control、头规范化、强 ETag 校验
  wire.py            标准库 socket 实现的极简 HTTP/1.1 客户端（严格分帧）
  server.py          JSON 场景驱动的模拟源站 + 请求日志
  scenarios.py       场景 JSON 解析/校验
  generate.py        确定性生成 scenarios/*.json
  store.py           SQLite 持久化、事务、Vary 规则/变体
  client.py          缓存决策与重验证流程
  selftest.py        verify 的 10 组自动检查
scenarios/           由 generate 生成的场景文件
```

---

## 7. 明确不在范围内

HTTPS、认证、代理、重定向、压缩、`Range`、`HEAD`、请求方法除 GET 以外、
`Date`/`Age`/`Expires`/启发式新鲜度、`private`/`no-cache` 等指令语义、
多进程共享写入的冲突合并、真实时间等待。未知缓存指令按上文忽略，不会报错。
