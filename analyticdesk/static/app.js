// 小型联动分析台前端。
//
// 并发安全的不变量：
// - 每次筛选变更生成新的 state_id；每个请求/响应都携带它。
// - 每次请求递增 generation（视图代次）。只有 generation 与 state_id 都匹配当前视图的
//   响应才会渲染；旧请求即使晚到也被丢弃，不会覆盖新筛选的结果。
// - 每个面板标注它展示数据的 state_id；单部件失败显示“未完成 + 重试”，不显示旧数字。

(() => {
  "use strict";

  const COMPONENTS = {
    metrics: ["metrics", "metrics-count"],
    trend: ["trend"],
    category_distribution: ["category_distribution"],
    category_candidates: ["category_candidates"],
    detail: ["detail"],
  };
  const COMPONENT_LABELS = {
    metrics: "指标",
    trend: "趋势",
    category_distribution: "类别分布",
    category_candidates: "类别候选",
    detail: "明细",
  };

  const ui = {
    dateFrom: document.getElementById("date-from"),
    dateTo: document.getElementById("date-to"),
    dateHint: document.getElementById("date-hint"),
    regionChecks: document.getElementById("region-checks"),
    categoryChecks: document.getElementById("category-checks"),
    undoBtn: document.getElementById("undo-btn"),
    drillBackBtn: document.getElementById("drill-return-btn"),
    resetBtn: document.getElementById("reset-btn"),
    refreshBtn: document.getElementById("refresh-btn"),
    delayMs: document.getElementById("delay-ms"),
    stateId: document.getElementById("state-id"),
    stateFp: document.getElementById("state-fp"),
    globalPending: document.getElementById("global-pending"),
    prevPage: document.getElementById("prev-page"),
    nextPage: document.getElementById("next-page"),
    pageInfo: document.getElementById("page-info"),
    pageSize: document.getElementById("page-size"),
    detailBody: document.getElementById("detail-body"),
    sortNote: document.getElementById("sort-note"),
    logBody: document.getElementById("request-log"),
    checkTemplate: document.getElementById("check-template"),
  };

  const app = {
    options: null,
    regionValues: [], // [{kind, value, label}]
    categoryValues: [],
    view: null, // { filters, stateId, page, pageSize, generation }
    undoSnapshot: null,
    drillReturn: null,
    seq: 0,
    log: [],
    stateCounter: 0,
    pageData: null,
    lastFailures: new Set(),
  };

  // ---------- 规范化与状态 ----------

  function selectionKey(sel) {
    return sel.kind === "null" ? "n:" : "v:" + sel.value;
  }

  function normalizeSelections(list) {
    const seen = new Set();
    const out = [];
    for (const sel of list || []) {
      const norm = sel.kind === "null" ? { kind: "null" } : { kind: "value", value: String(sel.value) };
      const key = selectionKey(norm);
      if (seen.has(key)) continue;
      seen.add(key);
      out.push(norm);
    }
    out.sort((a, b) => {
      const an = a.kind === "null" ? 0 : 1;
      const bn = b.kind === "null" ? 0 : 1;
      if (an !== bn) return an - bn;
      // 与 Python 的码点排序保持一致，避免 locale 差异导致指纹误判。
      const av = a.value || "";
      const bv = b.value || "";
      return av < bv ? -1 : av > bv ? 1 : 0;
    });
    return out;
  }

  // 与后端 Filters.as_json 的顺序/形状一致，用于比对响应中的 filters。
  function normalizeFilters(filters) {
    return {
      date_from: filters.date_from,
      date_to: filters.date_to,
      region: normalizeSelections(filters.region),
      category: normalizeSelections(filters.category),
    };
  }

  function filtersSignature(filters) {
    return JSON.stringify(normalizeFilters(filters));
  }

  function cloneFilters(filters) {
    return normalizeFilters(JSON.parse(JSON.stringify(filters)));
  }

  function newStateId() {
    app.stateCounter += 1;
    return "s" + app.stateCounter;
  }

  function sameSelections(a, b) {
    return selectionKey(a) === selectionKey(b);
  }

  function hasSelection(filters, dimension, sel) {
    return (filters[dimension] || []).some((s) => sameSelections(s, sel));
  }

  function toggleSelection(filters, dimension, sel) {
    const current = filters[dimension] || [];
    if (current.some((s) => sameSelections(s, sel))) {
      filters[dimension] = current.filter((s) => !sameSelections(s, sel));
    } else {
      filters[dimension] = current.concat([sel.kind === "null" ? { kind: "null" } : { kind: "value", value: sel.value }]);
    }
  }

  // ---------- 筛选输入 ----------

  function readFiltersFromInputs() {
    return normalizeFilters({
      date_from: ui.dateFrom.value,
      date_to: ui.dateTo.value,
      region: collectChecks(ui.regionChecks),
      category: collectChecks(ui.categoryChecks),
    });
  }

  function collectChecks(container) {
    const out = [];
    container.querySelectorAll("input[type=checkbox]").forEach((input) => {
      if (!input.checked) return;
      out.push({
        kind: input.dataset.kind,
        value: input.dataset.kind === "value" ? input.dataset.value : undefined,
      });
    });
    return out;
  }

  function syncInputsFromState(filters) {
    ui.dateFrom.value = filters.date_from;
    ui.dateTo.value = filters.date_to;
    applyChecks(ui.regionChecks, filters.region);
    applyChecks(ui.categoryChecks, filters.category);
    updateDateHint();
  }

  function applyChecks(container, selections) {
    const keys = new Set((selections || []).map(selectionKey));
    container.querySelectorAll("input[type=checkbox]").forEach((input) => {
      const key = input.dataset.kind === "null" ? "n:" : "v:" + input.dataset.value;
      input.checked = keys.has(key);
    });
  }

  function buildCheck(container, option, dimension) {
    const node = ui.checkTemplate.content.cloneNode(true);
    const label = node.querySelector(".check");
    const input = node.querySelector("input");
    input.dataset.kind = option.kind;
    if (option.kind === "value") input.dataset.value = option.value;
    input.dataset.dimension = dimension;
    node.querySelector(".check-label").textContent = option.label;
    node.querySelector(".check-count").textContent = option.kind === "value" && option.value === "" ? "" : "";
    label.title = option.kind === "null" ? "SQL NULL" : option.value === "" ? "空字符串" : option.value;
    input.addEventListener("change", () => {
      const filters = readFiltersFromInputs();
      commitFilters(filters, "toggle " + dimension);
    });
    container.appendChild(node);
  }

  function renderOptionChecks() {
    ui.regionChecks.innerHTML = "";
    ui.categoryChecks.innerHTML = "";
    for (const option of app.options.region) buildCheck(ui.regionChecks, option, "region");
    for (const option of app.options.category) buildCheck(ui.categoryChecks, option, "category");
    if (!app.options.region.length) ui.regionChecks.innerHTML = '<span class="empty">无</span>';
    if (!app.options.category.length) ui.categoryChecks.innerHTML = '<span class="empty">无</span>';
  }

  function updateDateHint() {
    const from = ui.dateFrom.value;
    const to = ui.dateTo.value;
    if (!from || !to) {
      ui.dateHint.textContent = "请选择起止日期";
      return;
    }
    if (from > to) {
      ui.dateHint.textContent = "结束日不得早于起始日";
      return;
    }
    ui.dateHint.textContent = from === to ? "区间为空：[起始, 结束) 不含任何日期" : `区间：[${from}, ${to})`;
  }

  // ---------- 状态与请求 ----------

  function commitFilters(filters, reason, options = {}) {
    const norm = normalizeFilters(filters);
    if (app.view) {
      const prev = cloneFilters(app.view.filters);
      if (!options.silentUndo && filtersSignature(prev) !== filtersSignature(norm)) {
        app.undoSnapshot = prev;
      }
    }
    const stateId = newStateId();
    app.view = {
      filters: norm,
      stateId,
      page: 1,
      pageSize: parseInt(ui.pageSize.value, 10) || 20,
      generation: 0,
    };
    if (options.drillReturn) {
      app.drillReturn = cloneFilters(options.drillReturn);
    }
    syncInputsFromState(norm);
    updateControls();
    refresh(reason || "filters");
  }

  function detailOf(data) {
    return data && data.detail ? data.detail : null;
  }

  function updateControls() {
    ui.undoBtn.disabled = !app.undoSnapshot;
    ui.drillBackBtn.hidden = !app.drillReturn;
    const detail = detailOf(app.pageData);
    const total = detail ? detail.total_pages : 0;
    const page = app.view ? app.view.page : 1;
    const pages = Math.max(1, total);
    ui.pageInfo.textContent = detail
      ? `第 ${page} / ${pages} 页 · 共 ${detail.total_rows} 条`
      : "—";
    ui.prevPage.disabled = !detail || page <= 1;
    ui.nextPage.disabled = !detail || page >= total;
  }

  function readDelay() {
    const value = parseInt(ui.delayMs.value, 10);
    return Number.isFinite(value) && value > 0 ? Math.min(value, 30000) : 0;
  }

  function readFailures() {
    const out = [];
    document.querySelectorAll('input[name="fail"]:checked').forEach((input) => out.push(input.value));
    return out;
  }

  function consumeFaults() {
    const delay = readDelay();
    const failures = readFailures();
    ui.delayMs.value = "0";
    document.querySelectorAll('input[name="fail"]').forEach((input) => {
      input.checked = false;
    });
    return { delay, failures };
  }

  function setBusy(on, stateId) {
    document.body.dataset.busy = on ? "1" : "0";
    if (stateId) document.body.dataset.viewState = stateId;
  }

  function setGlobalPending(on) {
    ui.globalPending.hidden = !on;
    if (on) ui.globalPending.textContent = "请求中…"; // 清除上一次的“未完成”残留文案
    if (app.view) {
      ui.stateId.textContent = on ? app.view.stateId + "（请求中）" : app.view.stateId;
    }
  }

  function markAllPending() {
    for (const key of Object.keys(COMPONENTS)) {
      for (const panelName of COMPONENTS[key]) {
        setOverlay(panelName, "loading", "加载中…", app.view ? app.view.stateId : "—");
      }
    }
  }

  function beginRequestLog(entry) {
    app.log.push(entry);
    renderLog();
  }

  function finishLog(entry, status, extra) {
    entry.status = status;
    entry.finished = clock();
    entry.extra = extra || "";
    renderLog();
  }

  function clock() {
    return new Date().toLocaleTimeString("zh-CN", { hour12: false });
  }

  function clearOverlays() {
    document.querySelectorAll(".panel-overlay").forEach((overlay) => {
      overlay.hidden = true;
      overlay.innerHTML = "";
    });
  }

  async function refresh(reason) {
    const view = app.view;
    if (!view) return;
    view.generation += 1;
    const generation = view.generation;
    const faults = consumeFaults();

    // 立即把旧内容清空/遮罩到“未完成”，避免把上一状态数字当作当前状态。
    markAllPending();
    setGlobalPending(true);
    setBusy(true, view.stateId);
    setChips(view.stateId, "pending");

    // 不主动 abort：让旧请求真实地在后端延后完成，由 generation/state_id 决定丢弃。

    app.seq += 1;
    const entry = {
      seq: app.seq,
      started: clock(),
      stateId: view.stateId,
      page: view.page,
      delay: faults.delay,
      status: "pending",
      finished: "",
      extra: reason || "",
    };
    beginRequestLog(entry);

    const params = new URLSearchParams();
    if (faults.delay) params.set("delay_ms", String(faults.delay));
    if (faults.failures.length) params.set("fail", faults.failures.join(","));
    const url = "/api/query" + (params.toString() ? "?" + params.toString() : "");

    let response;
    let payload;
    try {
      response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          state_id: view.stateId,
          filters: view.filters,
          page: view.page,
          page_size: view.pageSize,
        }),
      });
      payload = await response.json();
    } catch (err) {
      if (isStale(generation, view.stateId)) {
        finishLog(entry, "superseded", "丢弃：视图已更新");
        return;
      }
      entry.extra = String(err);
      finishLog(entry, "error", "网络错误");
      showWholePageError(entry.extra, view.stateId);
      return;
    }

    if (isStale(generation, view.stateId)) {
      finishLog(entry, "superseded", "丢弃：视图已更新（旧响应晚到）");
      return;
    }

    if (!response.ok) {
      const message = payload && payload.error ? payload.error.message : "HTTP " + response.status;
      finishLog(entry, "error", message);
      showWholePageError(message, payload && payload.state_id ? payload.state_id : view.stateId);
      return;
    }

    if (payload.state_id !== view.stateId) {
      finishLog(entry, "error", "状态标识不匹配：" + payload.state_id);
      showWholePageError("响应状态标识与当前筛选不一致，已拒绝渲染", view.stateId);
      return;
    }
    if (filtersSignature(payload.filters) !== filtersSignature(view.filters)) {
      finishLog(entry, "error", "筛选指纹不匹配");
      showWholePageError("响应筛选与当前筛选不一致，已拒绝渲染", view.stateId);
      return;
    }

    app.pageData = payload;
    setGlobalPending(false);
    setBusy(false, view.stateId);
    finishLog(entry, "ok", payload.partial ? "部分部件未完成" : "完成");
    renderPayload(payload, view);
  }

  function isStale(generation, stateId) {
    return !app.view || app.view.generation !== generation || app.view.stateId !== stateId;
  }

  function retryComponent(component) {
    const view = app.view;
    if (!view || !app.pageData) return;
    const generation = view.generation; // 同一视图代次，不递增：重试只补该部件
    retryComponentRequest(component, view, generation);
  }

  async function retryComponentRequest(component, view, generation) {
    const params = new URLSearchParams();
    const url = "/api/component" + (params.toString() ? "?" + params.toString() : "");
    setOverlayForComponent(component, "loading", "重试中…", view.stateId);
    app.seq += 1;
    const entry = {
      seq: app.seq,
      started: clock(),
      stateId: view.stateId,
      page: view.page,
      delay: 0,
      status: "pending",
      finished: "",
      extra: "重试 " + COMPONENT_LABELS[component],
    };
    beginRequestLog(entry);
    try {
      const response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          state_id: view.stateId,
          filters: view.filters,
          component,
          page: view.page,
          page_size: view.pageSize,
        }),
      });
      const payload = await response.json();
      if (isStale(generation, view.stateId)) {
        finishLog(entry, "superseded", "丢弃：视图已更新");
        return;
      }
      if (!response.ok) {
        finishLog(entry, "error", payload.error ? payload.error.message : "HTTP " + response.status);
        setOverlayForComponent(component, "error", "重试失败：" + (payload.error ? payload.error.message : response.status), view.stateId);
        return;
      }
      if (payload.state_id !== view.stateId) {
        finishLog(entry, "error", "状态标识不匹配");
        setOverlayForComponent(component, "error", "状态标识不匹配，已拒绝渲染", view.stateId);
        return;
      }
      applyComponent(component, payload.data, view.stateId);
      finishLog(entry, "ok", "重试完成");
      if (app.pageData && app.pageData.component_errors) delete app.pageData.component_errors[component];
      updatePartialBanner();
    } catch (err) {
      if (isStale(generation, view.stateId)) {
        finishLog(entry, "superseded", "丢弃：视图已更新");
        return;
      }
      finishLog(entry, "error", String(err));
      setOverlayForComponent(component, "error", "重试网络错误", view.stateId);
    }
  }

  // ---------- 渲染 ----------

  function renderPayload(payload, view) {
    app.pageData = payload;
    const errors = payload.component_errors || {};
    clearOverlays();

    if (errors.metrics) {
      setOverlay("metrics", "error", "指标未完成", view.stateId, "metrics");
      setOverlay("metrics-count", "error", "指标未完成", view.stateId, "metrics");
    } else {
      renderMetrics(payload.metrics, view.stateId);
    }

    if (errors.trend) {
      setOverlay("trend", "error", "趋势未完成", view.stateId, "trend");
      document.querySelector('[data-panel="trend"] [data-chart]').innerHTML = "";
    } else {
      renderTrend(payload.trend, view.stateId);
    }

    if (errors.category_distribution) {
      setOverlay("category_distribution", "error", "类别分布未完成", view.stateId, "category_distribution");
      document.querySelector('[data-panel="category_distribution"] [data-chart]').innerHTML = "";
    } else {
      renderCategory("category_distribution", payload.category_distribution.buckets, view.stateId);
    }

    if (errors.category_candidates) {
      setOverlay("category_candidates", "error", "类别候选未完成", view.stateId, "category_candidates");
      document.querySelector('[data-panel="category_candidates"] [data-chart]').innerHTML = "";
    } else {
      renderCategory("category_candidates", payload.category_candidates.buckets, view.stateId);
    }

    if (errors.detail) {
      setOverlay("detail", "error", "明细未完成", view.stateId, "detail");
      ui.detailBody.innerHTML = "";
      ui.pageInfo.textContent = "—";
    } else {
      renderDetail(payload.detail, view.stateId);
    }

    ui.stateFp.textContent = "指纹 " + (payload.filter_fingerprint || "").slice(0, 96);
    setChips(view.stateId, payload.partial ? "partial" : "fresh");
    updateControls();
    updatePartialBanner();
  }

  function updatePartialBanner() {
    const errors = (app.pageData && app.pageData.component_errors) || {};
    const names = Object.keys(errors).map((k) => COMPONENT_LABELS[k] || k);
    if (names.length) {
      ui.globalPending.hidden = false;
      ui.globalPending.textContent = "未完成：" + names.join("、") + "（可单独重试）";
    } else {
      ui.globalPending.hidden = true;
      ui.globalPending.textContent = "请求中…";
    }
  }

  function applyComponent(component, data, stateId) {
    if (component === "metrics") {
      renderMetrics(data, stateId);
    } else if (component === "trend") {
      renderTrend(data, stateId);
    } else if (component === "category_distribution") {
      renderCategory("category_distribution", data.buckets, stateId);
    } else if (component === "category_candidates") {
      renderCategory("category_candidates", data.buckets, stateId);
    } else if (component === "detail") {
      renderDetail(data, stateId);
    }
    // 单部件恢复后，取消该面板的覆盖层。
    for (const panelName of COMPONENTS[component] || []) {
      const panel = document.querySelector(`[data-panel="${panelName}"]`);
      if (panel) panel.querySelector("[data-overlay]").hidden = true;
    }
  }

  function renderMetrics(metrics, stateId) {
    for (const panelName of COMPONENTS.metrics) {
      const panel = document.querySelector(`[data-panel="${panelName}"]`);
      panel.querySelector('[data-field="amount_cents"]');
      const chip = panel.querySelector("[data-chip]");
      if (chip) {
        chip.textContent = stateId;
        chip.className = "state-chip fresh";
      }
      const overlay = panel.querySelector("[data-overlay]");
      overlay.hidden = true;
      if (panelName === "metrics") {
        panel.querySelector('[data-field="amount_cents"]').textContent = String(metrics.amount_cents);
        panel.querySelector('[data-field="amount_display"]').textContent = formatCents(metrics.amount_cents);
      } else {
        panel.querySelector('[data-field="row_count"]').textContent = String(metrics.row_count);
      }
    }
  }

  function formatCents(cents) {
    const sign = cents < 0 ? "-" : "";
    const abs = Math.abs(cents);
    const yuan = Math.floor(abs / 100);
    const fen = String(abs % 100).padStart(2, "0");
    return `${sign}¥${yuan}.${fen}`;
  }

  function formatBucketLabel(bucket) {
    if (bucket.kind === "null") return "NULL（空值）";
    if (bucket.value === "") return '""（空字符串）';
    return bucket.value;
  }

  function renderCategory(panelName, buckets, stateId) {
    const panel = document.querySelector(`[data-panel="${panelName}"]`);
    const chart = panel.querySelector("[data-chart]");
    const chip = panel.querySelector("[data-chip]");
    chip.textContent = stateId;
    chip.className = "state-chip fresh";
    panel.querySelector("[data-overlay]").hidden = true;

    if (!buckets.length) {
      chart.innerHTML = '<div class="trend-empty">当前口径无类别数据</div>';
      return;
    }
    const max = Math.max(1, ...buckets.map((b) => b.row_count));
    const wrap = document.createElement("div");
    wrap.className = "bars";
    for (const bucket of buckets) {
      const row = document.createElement("div");
      row.className = "bar-row" + (bucket.selected ? " selected" : "");
      const btn = document.createElement("button");
      btn.type = "button";
      btn.dataset.testid = `${panelName}-bar-${bucket.kind}-${bucket.value || ""}`;
      btn.dataset.kind = bucket.kind;
      if (bucket.kind === "value") btn.dataset.value = bucket.value;

      const label = document.createElement("span");
      label.className = "bar-label";
      label.textContent = formatBucketLabel(bucket);
      label.title = label.textContent;

      const track = document.createElement("span");
      track.className = "bar-track";
      const fill = document.createElement("span");
      fill.className = "bar-fill" + (bucket.row_count === 0 ? " zero" : bucket.kind === "null" ? " null" : "");
      fill.style.width = Math.round((bucket.row_count / max) * 100) + "%";
      track.appendChild(fill);

      const meta = document.createElement("span");
      meta.className = "bar-meta";
      meta.textContent = `${bucket.row_count} 条 · ${bucket.amount_cents} 分`;
      meta.title = `${bucket.row_count} 条 · ${bucket.amount_cents} 分`;

      btn.append(label, track, meta);
      btn.addEventListener("click", () => onCategoryBarClick(panelName, bucket));
      row.appendChild(btn);
      wrap.appendChild(row);
    }
    chart.innerHTML = "";
    chart.appendChild(wrap);
  }

  function onCategoryBarClick(panelName, bucket) {
    const view = app.view;
    if (!view) return;
    const filters = cloneFilters(view.filters);
    const sel = bucket.kind === "null" ? { kind: "null" } : { kind: "value", value: bucket.value };
    if (panelName === "category_candidates") {
      // 候选口径：点击 = 增加该类别（同维度并集），继续缩小视图。
      toggleSelection(filters, "category", sel);
      commitFilters(filters, "drill-add-category");
      scrollToDetail();
    } else {
      // 当前分布口径：点击 = 钻取到该类别明细。
      const previous = cloneFilters(view.filters);
      filters.category = [sel];
      commitFilters(filters, "drill-category", { drillReturn: previous, silentUndo: true });
      scrollToDetail();
    }
  }

  function renderTrend(trend, stateId) {
    const panel = document.querySelector('[data-panel="trend"]');
    const chart = panel.querySelector("[data-chart]");
    const chip = panel.querySelector("[data-chip]");
    chip.textContent = stateId;
    chip.className = "state-chip fresh";
    panel.querySelector("[data-overlay]").hidden = true;

    if (!trend.length) {
      chart.innerHTML = '<div class="trend-empty">日期区间为空</div>';
      return;
    }
    const width = Math.max(720, trend.length * 22);
    const height = 150;
    const padTop = 12;
    const padBottom = 22;
    const chartH = height - padTop - padBottom;
    const max = Math.max(1, ...trend.map((d) => d.row_count));
    const ns = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(ns, "svg");
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.setAttribute("preserveAspectRatio", "none");
    svg.setAttribute("data-testid", "trend-svg");

    const step = width / trend.length;
    trend.forEach((day, index) => {
      const h = day.row_count === 0 ? 2 : Math.max(3, Math.round((day.row_count / max) * chartH));
      const rect = document.createElementNS(ns, "rect");
      rect.setAttribute("x", String(Math.round(index * step + step * 0.15)));
      rect.setAttribute("y", String(padTop + chartH - h));
      rect.setAttribute("width", String(Math.max(2, Math.round(step * 0.7))));
      rect.setAttribute("height", String(h));
      rect.setAttribute("fill", day.row_count === 0 ? "#c6d0da" : "#2563a8");
      rect.setAttribute("data-testid", "trend-bar-" + day.date);
      const title = document.createElementNS(ns, "title");
      title.textContent = `${day.date} · ${day.row_count} 条 · ${day.amount_cents} 分`;
      rect.appendChild(title);
      rect.style.cursor = "pointer";
      rect.addEventListener("click", () => onTrendDayClick(day.date));
      svg.appendChild(rect);
      if (index % Math.ceil(trend.length / 12) === 0 || index === trend.length - 1) {
        const text = document.createElementNS(ns, "text");
        text.setAttribute("x", String(Math.round(index * step + step / 2)));
        text.setAttribute("y", String(height - 6));
        text.setAttribute("font-size", "9");
        text.setAttribute("text-anchor", "middle");
        text.setAttribute("fill", "#65727f");
        text.textContent = day.date.slice(5);
        svg.appendChild(text);
      }
    });
    const holder = document.createElement("div");
    holder.className = "trend-chart";
    holder.style.overflowX = "auto";
    holder.appendChild(svg);
    chart.innerHTML = "";
    chart.appendChild(holder);
  }

  function onTrendDayClick(date) {
    const view = app.view;
    if (!view) return;
    const previous = cloneFilters(view.filters);
    const next = nextDay(date);
    const filters = cloneFilters(view.filters);
    filters.date_from = date;
    filters.date_to = next;
    commitFilters(filters, "drill-day", { drillReturn: previous, silentUndo: true });
    scrollToDetail();
  }

  function nextDay(iso) {
    const d = new Date(iso + "T00:00:00Z");
    d.setUTCDate(d.getUTCDate() + 1);
    return d.toISOString().slice(0, 10);
  }

  function renderDetail(detail, stateId) {
    const panel = document.querySelector('[data-panel="detail"]');
    panel.querySelector("[data-chip]").textContent = stateId;
    panel.querySelector("[data-chip]").className = "state-chip fresh";
    panel.querySelector("[data-overlay]").hidden = true;
    ui.sortNote.textContent = "稳定排序：" + detail.sort.join(" → ") + "（唯一记录号裁决）";

    const rows = detail.rows;
    ui.detailBody.innerHTML = "";
    for (const row of rows) {
      const tr = document.createElement("tr");
      tr.dataset.testid = "detail-row-" + row.record_id;
      tr.appendChild(cell(row.record_id, "drill-id"));
      tr.appendChild(cell(row.business_date));
      tr.appendChild(dimCell(row.region));
      tr.appendChild(dimCell(row.category));
      tr.appendChild(cell(String(row.quantity), "num"));
      tr.appendChild(cell(String(row.amount_cents), "num"));
      ui.detailBody.appendChild(tr);
    }
    if (!rows.length) {
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = 6;
      td.textContent = "无命中记录";
      tr.appendChild(td);
      ui.detailBody.appendChild(tr);
    }
    updateControls();
  }

  function cell(text, cls) {
    const td = document.createElement("td");
    if (cls) td.className = cls;
    td.textContent = text;
    return td;
  }

  function dimCell(dim) {
    const td = document.createElement("td");
    if (dim.kind === "null") {
      td.textContent = "NULL";
      td.className = "dim-null";
      td.title = "SQL NULL";
    } else if (dim.value === "") {
      td.textContent = '""（空）';
      td.className = "dim-empty";
      td.title = "空字符串";
    } else {
      td.textContent = dim.value;
    }
    return td;
  }

  function scrollToDetail() {
    document.querySelector('[data-panel="detail"]').scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // ---------- 覆盖层 / 状态标记 ----------

  function setChips(stateId, kind) {
    document.querySelectorAll("[data-chip]").forEach((chip) => {
      chip.textContent = stateId;
      chip.className = "state-chip " + (kind === "fresh" ? "fresh" : kind === "partial" ? "stale" : "");
    });
  }

  function setOverlay(panelName, kind, message, stateId, retryComponentName) {
    const panel = document.querySelector(`[data-panel="${panelName}"]`);
    if (!panel) return;
    const overlay = panel.querySelector("[data-overlay]");
    overlay.hidden = false;
    overlay.innerHTML = "";
    const box = document.createElement("div");
    if (kind === "error") {
      const title = document.createElement("div");
      title.className = "bad";
      title.textContent = "未完成";
      box.appendChild(title);
    }
    const p = document.createElement("p");
    p.textContent = message + (stateId ? " · 状态 " + stateId : "");
    box.appendChild(p);
    if (kind === "error" && retryComponentName) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.textContent = "重试该部件";
      btn.dataset.testid = "retry-" + retryComponentName;
      btn.addEventListener("click", () => retryComponent(retryComponentName));
      box.appendChild(btn);
    }
    overlay.appendChild(box);
    const chip = panel.querySelector("[data-chip]");
    if (chip) {
      chip.textContent = stateId;
      chip.className = "state-chip " + (kind === "error" ? "stale" : "");
    }
  }

  function setOverlayForComponent(component, kind, message, stateId) {
    for (const panelName of COMPONENTS[component]) {
      setOverlay(panelName, kind, message, stateId, component);
    }
  }

  function showWholePageError(message, stateId) {
    setGlobalPending(false);
    setBusy(false, stateId);
    clearOverlays();
    for (const key of Object.keys(COMPONENTS)) {
      for (const panelName of COMPONENTS[key]) {
        setOverlay(panelName, "error", "整页未完成：" + message, stateId, "all");
      }
    }
    // 整页重试
    for (const overlay of document.querySelectorAll(".panel-overlay")) {
      const btn = overlay.querySelector("button");
      if (btn) {
        btn.textContent = "重试整页";
        btn.replaceWith(btn.cloneNode(true));
        overlay.querySelector("button").addEventListener("click", () => refresh("retry-page"));
      }
    }
    // 清掉可能残留的数值
    ui.detailBody.innerHTML = "";
    ui.pageInfo.textContent = "—";
    document.querySelectorAll("[data-chart]").forEach((el) => (el.innerHTML = ""));
    document.querySelectorAll(".metric-main").forEach((el) => (el.textContent = "—"));
    app.pageData = null;
    updateControls();
  }

  // ---------- 请求日志 ----------

  function renderLog() {
    ui.logBody.innerHTML = "";
    for (const entry of app.log.slice().reverse()) {
      const tr = document.createElement("tr");
      tr.dataset.testid = "log-" + entry.seq;
      tr.appendChild(cell(String(entry.seq)));
      tr.appendChild(cell(entry.started));
      const stateTd = document.createElement("td");
      const code = document.createElement("code");
      code.textContent = entry.stateId || "—";
      stateTd.appendChild(code);
      tr.appendChild(stateTd);
      tr.appendChild(cell(String(entry.page)));
      tr.appendChild(cell(entry.delay ? entry.delay + " ms" : "—"));
      const statusTd = document.createElement("td");
      statusTd.className = "log-status " + entry.status;
      statusTd.textContent =
        entry.status === "pending" ? "等待中" : entry.status === "ok" ? "完成" : entry.status === "error" ? "失败" : "已丢弃";
      if (entry.extra) statusTd.textContent += " · " + entry.extra;
      tr.appendChild(statusTd);
      tr.appendChild(cell(entry.finished || "—"));
      ui.logBody.appendChild(tr);
    }
  }

  // ---------- 事件绑定 ----------

  function bindEvents() {
    ui.dateFrom.addEventListener("change", () => {
      updateDateHint();
      commitFilters(readFiltersFromInputs(), "date");
    });
    ui.dateTo.addEventListener("change", () => {
      updateDateHint();
      commitFilters(readFiltersFromInputs(), "date");
    });
    ui.pageSize.addEventListener("change", () => {
      if (!app.view) return;
      app.view.pageSize = parseInt(ui.pageSize.value, 10) || 20;
      app.view.page = 1;
      app.view.generation += 1;
      refresh("page-size");
    });
    ui.prevPage.addEventListener("click", () => gotoPage(-1));
    ui.nextPage.addEventListener("click", () => gotoPage(1));
    ui.undoBtn.addEventListener("click", () => {
      if (!app.undoSnapshot) return;
      const snap = app.undoSnapshot;
      app.undoSnapshot = null;
      const filters = cloneFilters(snap);
      commitFilters(filters, "undo", { silentUndo: true });
    });
    ui.drillBackBtn.addEventListener("click", () => {
      if (!app.drillReturn) return;
      const filters = cloneFilters(app.drillReturn);
      app.drillReturn = null;
      commitFilters(filters, "drill-return", { silentUndo: true });
    });
    ui.resetBtn.addEventListener("click", () => {
      const filters = normalizeFilters({
        date_from: app.options.date_min,
        date_to: app.options.date_max,
        region: [],
        category: [],
      });
      app.drillReturn = null;
      commitFilters(filters, "reset");
    });
    ui.refreshBtn.addEventListener("click", () => refresh("manual"));
  }

  function gotoPage(delta) {
    const detail = detailOf(app.pageData);
    if (!app.view || !detail) return;
    const total = detail.total_pages;
    const target = app.view.page + delta;
    if (target < 1 || target > total) return;
    app.view.page = target;
    app.view.generation += 1;
    refresh("page");
  }

  // ---------- 启动 ----------

  async function boot() {
    const resp = await fetch("/api/options");
    app.options = await resp.json();
    app.regionValues = app.options.region;
    app.categoryValues = app.options.category;
    renderOptionChecks();

    ui.dateFrom.min = app.options.date_min;
    ui.dateTo.min = app.options.date_min;
    ui.dateFrom.max = app.options.date_max;
    ui.dateTo.max = app.options.date_max;

    const filters = normalizeFilters({
      date_from: app.options.date_min,
      date_to: app.options.date_max,
      region: [],
      category: [],
    });
    app.view = {
      filters,
      stateId: newStateId(),
      page: 1,
      pageSize: parseInt(ui.pageSize.value, 10) || 20,
      generation: 0,
    };
    syncInputsFromState(filters);
    updateControls();
    bindEvents();
    refresh("boot");
  }

  window.__analyticdesk = {
    get view() {
      return app.view;
    },
    get pageData() {
      return app.pageData;
    },
    get log() {
      return app.log;
    },
    refresh,
    retryComponent,
    commitFilters,
    cloneFilters,
    normalizeFilters,
    filtersSignature,
  };

  boot().catch((err) => {
    document.body.insertAdjacentHTML("afterbegin", `<pre style="color:#a82424">启动失败：${String(err)}</pre>`);
  });
})();
