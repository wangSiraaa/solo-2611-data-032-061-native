// TraceRelay 前端：原生 JS + SVG/DIV 泳道；所有数据走分页 API，不拉全量原文。
const $ = (id) => document.getElementById(id);
const state = { offset: 0, limit: 100, current: null };

const CAT_CN = { self: "自身活动", await: "显式等待", queue: "排队等待", gap: "不可解释空档" };
const TYPE_CN = { client: "client", server: "server", internal: "internal",
  producer: "producer", consumer: "consumer" };
const NS = (n) => n == null ? "unknown" : n >= 1e9 ? (n / 1e9).toFixed(2) + "s"
  : n >= 1e6 ? (n / 1e6).toFixed(2) + "ms" : n >= 1e3 ? (n / 1e3).toFixed(1) + "us" : n + "ns";

async function api(path, params) {
  const u = new URL(path, location.origin);
  if (params) Object.entries(params).forEach(([k, v]) => { if (v !== "" && v != null) u.searchParams.set(k, v); });
  const r = await fetch(u);
  if (!r.ok) throw new Error(`${path} -> ${r.status}`);
  return r.json();
}

function showModal(obj) {
  $("modal-pre").textContent = JSON.stringify(obj, null, 2);
  $("modal").classList.remove("hidden");
}
$("modal-close").onclick = () => $("modal").classList.add("hidden");

async function loadHealth() {
  try { const h = await api("/api/health"); $("health").textContent = "DB: " + h.db; }
  catch (e) { $("health").textContent = "未连接数据库"; }
}

async function loadTraces() {
  const p = { offset: state.offset, limit: state.limit, q: $("q").value,
    complete: $("f-complete").checked ? 1 : "", gaps: $("f-gaps").checked ? 1 : "" };
  const data = await api("/api/traces", p);
  const el = $("trace-list");
  el.innerHTML = "";
  data.rows.forEach((t) => {
    const div = document.createElement("div");
    div.className = "trace-item" + (state.current === t.trace_id ? " active" : "");
    const flag = t.complete_flag
      ? '<span class="pill ok">完整</span>'
      : (t.has_unknown_time ? '<span class="pill unknown">未知时间</span>'
         : '<span class="pill gap">缺口</span>');
    div.innerHTML = `<div class="tid">${t.trace_id}${flag}</div>
      <div class="meta">${t.request_type} · 窗口 ${NS(t.root_window_ns)}
      · self ${NS(t.self_ns)} / await ${NS(t.await_ns)} / queue ${NS(t.queue_ns)}
      / gap ${NS(t.gap_ns)}</div>
      <div class="meta">span ${t.n_spans} · event ${t.n_events} · await ${t.n_awaits}
      ${t.background_ns ? " · 后台 " + NS(t.background_ns) : ""}
      ${t.conservation_ok ? "" : " · <b style='color:#dc2626'>守恒失败</b>"}</div>`;
    div.onclick = () => openTrace(t.trace_id);
    el.appendChild(div);
  });
  $("pager").innerHTML =
    `<button ${state.offset === 0 ? "disabled" : ""} id="pg-prev">上一页</button>
     <span class="small muted">${state.offset + 1}–${Math.min(state.offset + data.limit, data.total)} / ${data.total}</span>
     <button ${state.offset + data.limit >= data.total ? "disabled" : ""} id="pg-next">下一页</button>`;
  $("pg-prev")?.addEventListener("click", () => { state.offset -= state.limit; loadTraces(); });
  $("pg-next")?.addEventListener("click", () => { state.offset += state.limit; loadTraces(); });
}

["q", "f-complete", "f-gaps"].forEach((id) =>
  $(id).addEventListener("input", () => { state.offset = 0; loadTraces(); }));

async function openTrace(tid) {
  state.current = tid;
  $("welcome").classList.add("hidden");
  $("report-view").classList.add("hidden");
  $("detail").classList.remove("hidden");
  await loadTraces();
  const [detail, timeline, lanes, awaits, rpc, diags] = await Promise.all([
    api("/api/trace", { trace_id: tid }), api("/api/timeline", { trace_id: tid }),
    api("/api/lanes", { trace_id: tid, limit: 500 }),
    api("/api/awaits", { trace_id: tid }), api("/api/rpc", { trace_id: tid }),
    api("/api/diagnostics", { trace_id: tid, limit: 500 })]);
  renderSummary(detail);
  renderCoverage(detail.summary, timeline.slices);
  renderLanes(lanes, detail.summary);
  renderAwaits(awaits.rows);
  renderRpc(rpc.rows);
  renderDiags(diags.rows);
  renderIssues(detail.issues);
}

function renderSummary(detail) {
  const t = detail.summary;
  const rows = [
    ["trace / 根 span", `${t.trace_id} / ${t.root_span_id}`],
    ["请求类型", t.request_type],
    ["根窗口 (ns)", `${t.root_window_ns ?? "unknown"}（${NS(t.root_window_ns)}）`],
    ["覆盖守恒", t.conservation_ok ? "✅ 时间片总和 = 根窗口" : "❌ 失败"],
    ["完整度", t.complete_flag ? "完整追踪" : `有缺口（gap=${NS(t.gap_ns)}，未知时间=${t.has_unknown_time ? "是" : "否"}）`],
    ["根后后台工作", NS(t.background_ns) + (t.background_open ? "（含开放区间，仅下界）" : "")],
    ["span / event / await", `${t.n_spans} / ${t.n_events} / ${t.n_awaits}`],
  ];
  $("summary-card").innerHTML = "<h3>Trace 摘要</h3><div class='kv'>" +
    rows.map(([k, v]) => `<b>${k}</b><span>${v}</span>`).join("") + "</div>";
}

function renderCoverage(t, slices) {
  const win = t.root_window_ns || 1;
  const parts = [["self", t.self_ns], ["await", t.await_ns], ["queue", t.queue_ns], ["gap", t.gap_ns]];
  $("coverage-bar").innerHTML = parts.map(([k, v]) =>
    `<div class="seg ${k}" style="width:${(v / win * 100).toFixed(2)}%" title="${CAT_CN[k]} ${NS(v)}">`
    + (v / win > 0.07 ? `${CAT_CN[k]} ${NS(v)}` : "") + "</div>").join("");
  const rows = slices.map((s, i) => `<tr class="clickable" data-ref='${JSON.stringify([s.ref_kind, s.ref_id])}'>
    <td>${i}</td><td>${(s.start_ns / 1e6).toFixed(2)}</td><td>${(s.end_ns / 1e6).toFixed(2)}</td>
    <td>${NS(s.duration_ns)}</td><td class="cat-${s.category}">${CAT_CN[s.category]}</td>
    <td>${s.sublabel}</td><td>${s.service}/${s.operation}</td>
    <td class="small muted">${s.ref_kind}:${s.ref_id}</td></tr>`).join("");
  $("timeline-table").innerHTML = `<table><tr><th>#</th><th>起 ms</th><th>止 ms</th><th>时长</th>
    <th>类别</th><th>子标签/原因</th><th>服务/操作</th><th>证据引用</th></tr>${rows}</table>`;
  $("timeline-table").querySelectorAll("tr.clickable").forEach((tr) => {
    tr.onclick = async () => {
      const [kind, id] = JSON.parse(tr.dataset.ref);
      await jumpToRaw(kind, id);
    };
  });
}

// 通过证据 id 反查 source_file/line_no，再取原始 JSONL 全文（点击定位原始事件）
async function jumpToRaw(kind, id) {
  if (!id) { showModal({ note: "该时间片没有单条证据引用（扫描器自动补出的空档）" }); return; }
  try {
    const rec = await api("/api/ref-loc", { trace_id: state.current, kind, id });
    showModal(rec);
  } catch (e) {
    showModal({ error: "无法定位原文", kind, id });
  }
}

function renderLanes(data, summary) {
  window.__laneCache = data;
  const lo = summary.root_start_ns, hi = summary.root_end_ns || lo + 1;
  const span = hi - lo || 1;
  const x = (ns) => ((ns - lo) / span * 100).toFixed(2);
  const bySvc = {};
  data.spans.forEach((s) => (bySvc[s.service] ??= []).push(s));
  const lanes = Object.entries(bySvc).map(([svc, spans]) => {
    const boxes = spans.map((s) => {
      const s0 = s.start_ns == null ? lo : Math.max(s.start_ns, lo);
      const e0 = s.end_ns == null ? hi : Math.min(s.end_ns, hi);
      const left = x(s0), w = Math.max(0.4, (e0 - s0) / span * 100);
      return `<div class="span-box t-${s.span_type}" style="left:${left}%;width:${w}%"
        title="${s.operation} [${s.start_ns ?? "?"}, ${s.end_ns ?? "?"}] ${s.span_id}">
        ${s.operation}${s.attempt_no ? " #" + s.attempt_no : ""}</div>`;
    }).join("");
    const dots = data.events.filter((e) => e.span_id && spans.some((s) => s.span_id === e.span_id))
      .map((e) => `<div class="event-dot" style="left:${x(e.at_ns)}%" title="${e.name} @${NS(e.at_ns)}"></div>`).join("");
    const aws = data.awaits.map((a) =>
      `<div class="await-line" style="left:${x(a.start_ns)}%;width:${Math.max(.3, ((a.end_ns ?? hi) - a.start_ns) / span * 100)}%"
        title="await ${a.mode} ${a.await_id}"></div>`).join("");
    return `<div class="lane-row"><div class="lane-label" title="${svc}">${svc} (${spans.length})</div>
      <div class="lane-track">${boxes}${dots}${aws}</div></div>`;
  }).join("");
  $("lanes").innerHTML = lanes + (data.truncated
    ? '<p class="small" style="color:#d97706">泳道按限量返回，完整数据请用导出/CLI 查看</p>' : "");
}

function renderAwaits(rows) {
  $("awaits").innerHTML = rows.length ? rows.map((r) =>
    `<div class="resp"><code>${r.await_id}</code> mode=<b>${r.mode}</b>
      waiter=${r.waiter_span_id} 窗口=[${NS(r.start_ns)}, ${NS(r.end_ns)}]
      解除结果=${r.resolved_by} <br>真实阻塞阶段：<b>${r.blocker_phase}</b>
      <span class="muted">${r.blocker_detail}</span>
      ${r.upper_await_id ? `<br><span class="small" style="color:#7c3aed">嵌套于 ${r.upper_await_id}（只解释不重复计时）</span>` : ""}
      <details><summary class="small">关键证据</summary><pre class="small">${r.evidence}</pre></details></div>`).join("")
    : '<p class="muted">无 await 记录</p>';
}

function renderRpc(rows) {
  $("rpc").innerHTML = `<table><tr><th>client span</th><th>server span</th><th>匹配依据</th><th>置信</th></tr>` +
    rows.map((r) => `<tr><td>${r.client_span_id ?? "—"}</td><td>${r.server_span_id ?? "—"}</td>
      <td>${r.match_basis}</td><td>${r.confidence}</td></tr>`).join("") + "</table>";
}

function renderDiags(rows) {
  $("diags").innerHTML = rows.length ? rows.map((d) =>
    `<div class="diag ${d.certainty}"><b>[${d.certainty}]</b> ${d.diag_code} — ${d.title}
      <details><summary class="small">证据（点击展开）</summary><pre class="small">${d.evidence}</pre></details></div>`).join("")
    : '<p class="muted">无诊断</p>';
}

function renderIssues(issues) {
  $("issues").innerHTML = issues.length ? `<table><tr><th>级别</th><th>代码</th><th>证据</th></tr>` +
    issues.map((i) => `<tr><td>${i.severity}</td><td>${i.code}</td>
      <td><code class="small">${i.evidence}</code></td></tr>`).join("") + "</table>"
    : '<p class="muted">无验证问题</p>';
}

$("btn-report").onclick = async () => {
  $("welcome").classList.add("hidden"); $("detail").classList.add("hidden");
  const rep = await api("/api/report");
  const o = rep.overview;
  const svc = rep.by_service_operation.map((r) =>
    `<tr><td>${r.service}</td><td>${r.operation}</td><td>${r.execution.n ?? 0}</td>
     <td>${NS(r.execution.p50_ns)}</td><td>${NS(r.execution.p95_ns)}</td>
     <td>${NS(r.queue_wait.p95_ns)}</td><td>${r.open_ended_trace_count}</td><td class="small">${r.note}</td></tr>`).join("");
  const rt = rep.by_request_type.map((r) =>
    `<tr><td>${r.request_type}</td><td>${r.n}</td><td>${(r.await_share_pct ?? 0).toFixed(1)}%</td>
     <td>${(r.queue_share_pct ?? 0).toFixed(1)}%</td><td>${(r.gap_share_pct ?? 0).toFixed(1)}%</td>
     <td>${(r.incomplete_rate).toFixed(1)}%</td></tr>`).join("");
  $("report-view").classList.remove("hidden");
  $("report-view").innerHTML = `<h2>批量报告（池化分位数，禁止分位数相加）</h2>
    <p class="muted">${rep.generated_note}</p>
    <p>trace ${o.n_traces}：完整 ${o.n_complete}，有缺口 ${o.n_with_gaps}，
    不完整率 ${(o.incomplete_rate ?? 0).toFixed(2)}，守恒失败 ${o.conservation_violations.length}</p>
    <h3>按服务/操作</h3><table><tr><th>服务</th><th>操作</th><th>n</th><th>exec p50</th>
    <th>exec p95</th><th>queue p95</th><th>开放区间</th><th>说明</th></tr>${svc}</table>
    <h3>按请求类型</h3><table><tr><th>请求类型</th><th>n</th><th>await 占比</th>
    <th>queue 占比</th><th>gap 占比</th><th>不完整率</th></tr>${rt}</table>
    <h3>诊断计数</h3><table><tr><th>代码</th><th>确定性</th><th>次数</th></tr>` +
    rep.diagnostics.map((d) => `<tr><td>${d.diag_code}</td><td>${d.certainty}</td><td>${d.count}</td></tr>`).join("")
    + "</table>";
};

loadHealth();
loadTraces();
