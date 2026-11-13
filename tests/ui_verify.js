// 浏览器端端到端验证：真实操作页面，而不是只测接口。
//
// 覆盖：
// 1. 地区/类别多选（并集）、不同维度交集、NULL 与空字符串分别可选。
// 2. 日期左闭右开（改变结束日只影响其后的记录）。
// 3. 两种类别口径：分布（含类别筛选）与候选（移除类别筛选）。
// 4. 点击柱状图：分布口径钻取明细；候选口径增加类别。
// 5. 钻取后“返回钻取前筛选”、以及一次性撤销。
// 6. 快速连续操作：慢请求后发先至，旧响应晚到不得覆盖新视图。
// 7. 单图失败：显示未完成 + 重试，且不显示旧数字；恢复后数值与状态一致。
// 8. 页面显示的状态标识与请求日志一致。
//
// 运行：node tests/ui_verify.js（需要先启动服务）
//      环境变量 BASE_URL 覆盖默认 http://127.0.0.1:8000
//      需要 Playwright + Chromium。

"use strict";

const { chromium } = require("playwright");

const BASE = process.env.BASE_URL || "http://127.0.0.1:8000";
const RESULTS = [];
let FAILS = 0;

function check(cond, message) {
  if (!cond) {
    FAILS += 1;
    console.log("  FAIL  " + message);
  }
  RESULTS.push({ ok: !!cond, message });
}

function section(name) {
  console.log("\n== " + name);
}

const S = {
  panel: (name) => `[data-panel="${name}"]`,
  chip: (name) => `[data-panel="${name}"] [data-chip]`,
  overlay: (name) => `[data-panel="${name}"] [data-overlay]`,
  metricAmount: `[data-panel="metrics"] [data-field="amount_cents"]`,
  metricCount: `[data-panel="metrics-count"] [data-field="row_count"]`,
  distBars: '[data-panel="category_distribution"] .bar-row',
  candBars: '[data-panel="category_candidates"] .bar-row',
  detailRows: "#detail-body tr[data-testid^='detail-row']",
  pageInfo: "#page-info",
  dateFrom: "#date-from",
  dateTo: "#date-to",
  region: "#region-checks",
  category: "#category-checks",
};

function checkSelector(container, kind, value) {
  const base = `${container} input[data-kind="${kind}"]`;
  return kind === "value" ? `${base}[data-value="${value}"]` : base;
}

async function waitSettled(page) {
  // 页面通过 body.dataset.busy 明确暴露“当前视图的整页请求是否已完成”。
  await page.waitForFunction(
    () => {
      const view = window.__analyticdesk && window.__analyticdesk.view;
      return (
        document.body.dataset.busy === "0" &&
        view &&
        document.body.dataset.viewState === view.stateId
      );
    },
    null,
    { timeout: 15000 }
  );
  await page.waitForTimeout(120);
}

async function stateId(page) {
  return (await page.locator("#state-id").textContent()).replace("（请求中）", "").trim();
}

async function waitStateChange(page, previous) {
  await page.waitForFunction(
    (prev) => {
      const view = window.__analyticdesk && window.__analyticdesk.view;
      if (!view) return false;
      return (
        view.stateId !== prev &&
        document.body.dataset.busy === "0" &&
        document.body.dataset.viewState === view.stateId
      );
    },
    previous,
    { timeout: 15000 }
  );
  await page.waitForTimeout(120);
}

async function clickCheck(page, container, kind, value) {
  await page.locator(checkSelector(container, kind, value)).first().click();
}

async function setDates(page, from, to) {
  // Playwright 的 fill 已同时触发 input 与 change；不要再手动 dispatch，否则会发两次请求。
  await page.locator(S.dateFrom).fill(from);
  await page.locator(S.dateTo).fill(to);
}

async function readMetrics(page) {
  return {
    amount: Number(await page.locator(S.metricAmount).textContent()),
    count: Number(await page.locator(S.metricCount).textContent()),
  };
}

async function readChip(page, panel) {
  return (await page.locator(S.chip(panel)).textContent()).trim();
}

async function filtersOf(page) {
  return page.evaluate(() => JSON.parse(JSON.stringify(window.__analyticdesk.view.filters)));
}

async function allLandedStates(page) {
  return page.locator("#request-log tr").evaluateAll((rows) =>
    rows.map((tr) => ({
      state: tr.children[2] ? tr.children[2].textContent.trim() : "",
      status: tr.children[5] ? tr.children[5].textContent.trim() : "",
      finished: tr.children[6] ? tr.children[6].textContent.trim() : "",
    }))
  );
}

async function main() {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1400, height: 1000 } });
  const pageErrors = [];
  page.on("pageerror", (err) => pageErrors.push(err.message));

  await page.goto(BASE + "/");
  await page.waitForSelector(S.detailRows, { timeout: 15000 });
  await waitSettled(page);
  const options = await page.evaluate(async () => (await fetch("/api/options")).json());

  // ------------------------------------------------------------------ //
  section("1. 初始状态与状态标识");
  const initial = await readMetrics(page);
  const s1 = await stateId(page);
  check(/^s\d+$/.test(s1), `初始 state_id 应形如 sN，得到 ${s1}`);
  for (const panel of ["metrics", "trend", "category_distribution", "category_candidates", "detail"]) {
    check((await readChip(page, panel)) === s1, `${panel} 面板状态标识应与全局一致`);
  }
  check(initial.count === 360, `初始应 360 条，得到 ${initial.count}`);
  check((await page.locator(S.detailRows).count()) === 20, "默认每页 20 条");
  check((await page.locator(S.pageInfo).textContent()).includes("360"), "分页信息应显示 360 条");

  // ------------------------------------------------------------------ //
  section("2. 多选：地区并集");
  const beforeRegion = s1;
  await clickCheck(page, S.region, "value", "华北");
  await waitStateChange(page, beforeRegion);
  const s2 = await stateId(page);
  check(s2 !== beforeRegion, "筛选后 state_id 应变化");
  check((await readMetrics(page)).count === 72, "仅华北应 72 条");

  await clickCheck(page, S.region, "value", "华东");
  await waitStateChange(page, s2);
  const union = await readMetrics(page);
  check(union.count === 144, `华北+华东并集应 144 条，得到 ${union.count}`);
  check((await page.locator(S.pageInfo).textContent()).includes("144"), "分页信息应反映并集 144 条");

  const s2b = await stateId(page);
  await clickCheck(page, S.region, "value", "华东");
  await waitStateChange(page, s2b);
  check((await readMetrics(page)).count === 72, "取消华东后应恢复 72 条");

  // ------------------------------------------------------------------ //
  section("3. NULL 与空字符串分别可选，且并集不被合并");
  const s3 = await stateId(page);
  await clickCheck(page, S.region, "value", "华北"); // 取消华北
  await clickCheck(page, S.region, "null");
  await waitStateChange(page, s3);
  const nullOnly = await readMetrics(page);
  check(nullOnly.count === 72, `仅地区 NULL 应 72 条，得到 ${nullOnly.count}`);
  const nullRows = await page.locator(S.detailRows).count();
  const nullCells = await page.locator("#detail-body td.dim-null").count();
  check(nullCells >= nullRows, `地区 NULL 明细应显示独立 NULL 样式（${nullCells}/${nullRows}）`);

  const s3b = await stateId(page);
  await clickCheck(page, S.region, "null");
  await clickCheck(page, S.region, "value", "");
  await waitStateChange(page, s3b);
  const emptyOnly = await readMetrics(page);
  check(emptyOnly.count === 72, `仅地区空字符串应 72 条，得到 ${emptyOnly.count}`);
  check((await page.locator("#detail-body td.dim-empty").count()) >= 1, "空字符串地区应显示可区分样式");
  const s3c = await stateId(page);
  await clickCheck(page, S.region, "null");
  await waitStateChange(page, s3c);
  check((await readMetrics(page)).count === 144, "NULL 与空串并集应 144 条（二者未被合并，也不是全部 360）");

  // 清空地区
  const s3d = await stateId(page);
  await clickCheck(page, S.region, "null");
  await clickCheck(page, S.region, "value", "");
  await waitStateChange(page, s3d);
  check((await readMetrics(page)).count === 360, "清空地区后应恢复 360 条");

  // ------------------------------------------------------------------ //
  section("4. 不同维度取交集");
  const s4 = await stateId(page);
  await clickCheck(page, S.category, "null");
  await waitStateChange(page, s4);
  const nullCat = await readMetrics(page);
  check(nullCat.count === 99, `仅类别 NULL 应 99 条，得到 ${nullCat.count}`);
  const s4b = await stateId(page);
  await clickCheck(page, S.region, "value", "华北");
  await waitStateChange(page, s4b);
  const intersect = await readMetrics(page);
  check(intersect.count === 7, `华北 ∩ 类别NULL 应 7 条，得到 ${intersect.count}`);
  check(intersect.count < nullCat.count, "不同维度应为交集（结果变小）");

  // 清空类别与地区，准备两口径对照
  const s4c = await stateId(page);
  await clickCheck(page, S.category, "null");
  await clickCheck(page, S.region, "value", "华北");
  await clickCheck(page, S.category, "value", "办公");
  await waitStateChange(page, s4c);
  check((await readMetrics(page)).count === 65, "仅办公应 65 条");

  // ------------------------------------------------------------------ //
  section("5. 两种类别口径：完整筛选 vs 移除类别自身筛选");
  check((await page.locator(S.distBars).count()) === 1, "分布口径应只显示选中类别（办公）");
  const candCount = await page.locator(S.candBars).count();
  check(candCount === 5, `候选口径应列出全部可继续添加的类别（5），得到 ${candCount}`);
  const candLabels = await page.locator('[data-panel="category_candidates"] .bar-label').allTextContents();
  check(candLabels.includes("服务"), "候选应包含未选中的“服务”");
  check(candLabels.some((t) => t.includes("NULL")), "候选应包含 NULL");
  const candSet = new Set(candLabels);
  check(
    ["办公", "服务", "维修", "运输"].every((v) => candSet.has(v)),
    `候选应包含全部普通类别，实际 ${JSON.stringify(candLabels)}`
  );
  check(candLabels.filter((t) => t.includes("NULL")).length === 1, "候选应包含且仅包含一个 NULL 桶");
  const candidateMeta = await page.locator('[data-panel="category_candidates"] .bar-meta').allTextContents();
  check(candidateMeta.length === 5, "候选每个类别都有条数/金额");

  const s5 = await stateId(page);
  await page.locator('[data-testid="category_candidates-bar-value-服务"]').click();
  await waitStateChange(page, s5);
  const afterAdd = await readMetrics(page);
  check(afterAdd.count === 131, `办公 ∪ 服务 应 131 条，得到 ${afterAdd.count}`);
  const selectedCats = await page.locator(S.category + " input:checked").evaluateAll((els) =>
    els.map((e) => (e.dataset.kind === "null" ? "NULL" : e.dataset.value))
  );
  check(
    selectedCats.includes("办公") && selectedCats.includes("服务"),
    `类别选择应为并集，实际 ${JSON.stringify(selectedCats)}`
  );
  // 候选口径仍在，且现在选中两个
  const selectedInCand = await page.locator('[data-panel="category_candidates"] .bar-row.selected').count();
  check(selectedInCand === 2, `候选图应标记两个已选类别，得到 ${selectedInCand}`);

  // ------------------------------------------------------------------ //
  section("6. 分布口径点击 = 钻取明细；返回钻取前筛选");
  const s6 = await stateId(page);
  const preDrill = await filtersOf(page);
  await page.locator('[data-panel="category_distribution"] .bar-row button').first().click();
  await waitStateChange(page, s6);
  check(await page.locator("#drill-return-btn").isVisible(), "钻取后应显示“返回钻取前筛选”");
  const drillFilters = await filtersOf(page);
  check(drillFilters.category.length === 1, `钻取后类别应只含被点击类别，得到 ${JSON.stringify(drillFilters.category)}`);
  check((await readChip(page, "detail")) === (await stateId(page)), "钻取后明细状态标识与全局一致");
  // 钻取后分布口径应只剩该类别的桶
  check((await page.locator(S.distBars).count()) === 1, "钻取后分布口径只含钻取类别");

  await page.locator("#drill-return-btn").click();
  await waitSettled(page);
  const back = await filtersOf(page);
  check(JSON.stringify(back) === JSON.stringify(preDrill), "返回钻取前筛选应恢复原类别与日期");
  check(!(await page.locator("#drill-return-btn").isVisible()), "返回后按钮应隐藏");

  // ------------------------------------------------------------------ //
  section("7. 撤销一次");
  const s7 = await stateId(page);
  const undoBefore = await readMetrics(page);
  await page.locator(S.dateFrom).fill("2025-01-10");
  await waitStateChange(page, s7);
  const afterDate = await readMetrics(page);
  check(afterDate.count !== undoBefore.count, "改起始日后条数应变化");
  check(await page.locator("#undo-btn").isEnabled(), "撤销按钮应可用");
  const s7b = await stateId(page);
  await page.locator("#undo-btn").click();
  await waitStateChange(page, s7b);
  const undone = await readMetrics(page);
  check(
    undone.count === undoBefore.count && undone.amount === undoBefore.amount,
    `撤销应恢复上次数值：${JSON.stringify(undone)} vs ${JSON.stringify(undoBefore)}`
  );
  check((await page.locator(S.dateFrom).inputValue()) === options.date_min, "撤销应恢复日期输入");
  check(!(await page.locator("#undo-btn").isEnabled()), "撤销一次后按钮应不可用（只保留一次）");

  // ------------------------------------------------------------------ //
  section("8. 日期左闭右开边界");
  await page.locator("#reset-btn").click();
  await waitSettled(page);
  check((await readMetrics(page)).count === 360, "重置后应 360 条");
  const fullDates = await readMetrics(page);
  await page.locator(S.dateTo).fill("2025-02-14");
  await waitSettled(page);
  const rightOpen = await readMetrics(page);
  check(rightOpen.count < fullDates.count, `结束日不含当天时条数应减少：${rightOpen.count} vs ${fullDates.count}`);
  const trendDates = await page
    .locator('[data-testid^="trend-bar-"]')
    .evaluateAll((els) => els.map((e) => e.getAttribute("data-testid").replace("trend-bar-", "")));
  check(!trendDates.includes("2025-02-14"), "右开区间不应包含结束日 2025-02-14");
  const detailDatesA = await page.locator("#detail-body td:nth-child(2)").allTextContents();
  check(
    detailDatesA.length > 0 && detailDatesA.every((d) => d < "2025-02-14"),
    "明细不应出现结束日当天记录"
  );

  await setDates(page, "2025-01-02", "2025-01-03");
  await waitSettled(page);
  const single = await readMetrics(page);
  const detailDatesB = await page.locator("#detail-body td:nth-child(2)").allTextContents();
  check(
    detailDatesB.length > 0 && detailDatesB.every((d) => d === "2025-01-02"),
    `单日区间明细应全为 2025-01-02，实际 ${JSON.stringify(detailDatesB)}`
  );
  check(single.count === detailDatesB.length, "单日区间条数与明细一致");
  check((await page.locator('[data-testid^="trend-bar-"]').count()) === 1, "单日区间趋势应 1 个点");

  // ------------------------------------------------------------------ //
  section("9. 快速连续操作：快请求先完成、慢请求后完成且被丢弃");
  await setDates(page, "2025-01-01", "2025-02-15");
  await waitSettled(page);
  for (const value of ["办公", "服务"]) {
    const box = page.locator(checkSelector(S.category, "value", value));
    if (await box.isChecked()) await box.click();
  }
  await waitSettled(page);
  check((await readMetrics(page)).count === 360, "清空类别后应 360 条");

  await page.locator("#delay-ms").fill("900");
  const baseState = await stateId(page);
  await page.locator(checkSelector(S.region, "value", "华北")).click();
  // 等慢请求确实在途
  await page.waitForFunction(
    () => document.body.dataset.busy === "1",
    null,
    { timeout: 5000 }
  );
  const slowState = await page.evaluate(() => window.__analyticdesk.view.stateId);

  // 立刻改选华南：先取消华北，再选中华南（两次都快）
  await page.locator(checkSelector(S.region, "value", "华北")).click();
  await page.locator(checkSelector(S.region, "value", "华南")).click();
  await waitSettled(page);
  const fastSettledAt = Date.now();

  const finalState = await stateId(page);
  const finalMetrics = await readMetrics(page);
  const checkedRegions = await page.locator(S.region + " input:checked").evaluateAll((els) =>
    els.map((e) => e.dataset.value)
  );
  check(finalState !== slowState, `最终状态应是后发的华南，而不是慢的华北：final=${finalState} slow=${slowState}`);
  check(
    checkedRegions.includes("华南") && !checkedRegions.includes("华北"),
    `最终只应选中华南，实际 ${JSON.stringify(checkedRegions)}`
  );
  check(finalMetrics.count === 72, `华南应 72 条，得到 ${finalMetrics.count}`);
  check((await readChip(page, "metrics")) === finalState, "最终指标状态标识应为最终状态");
  check((await readChip(page, "category_candidates")) === finalState, "最终候选状态标识应为最终状态");

  // 慢响应应当在快请求之后才落地
  await page.waitForTimeout(1100);
  await waitSettled(page);
  const logRows = await allLandedStates(page);
  const slowLog = logRows.find((r) => r.state === slowState);
  check(!!slowLog, `请求日志应包含慢请求状态 ${slowState}`);
  check(!!slowLog && slowLog.status.includes("已丢弃"), `慢请求应被标记丢弃，实际 ${slowLog && slowLog.status}`);
  check(!!slowLog && slowLog.finished !== "—", "慢请求应真实完成（证明是晚到被丢弃，而非被取消）");
  check((await readMetrics(page)).count === 72, "慢请求落地后页面仍是华南 72 条");
  check((await stateId(page)) === finalState, "慢请求落地后状态标识仍为最终状态");

  // 恢复地区全选
  await page.locator(checkSelector(S.region, "value", "华南")).click();
  await waitSettled(page);

  // ------------------------------------------------------------------ //
  section("10. 单图失败：未完成 + 重试，不显示旧数值");
  await page.locator("#delay-ms").fill("0");
  await page.locator('input[name="fail"][value="category_distribution"]').check();
  const preFailState = await stateId(page);
  const preFailMetrics = await readMetrics(page);
  const preFailDistBars = await page.locator(S.distBars).count();
  check(preFailDistBars > 0, "失败前分布图应有柱子");

  const s10 = preFailState;
  await page.locator(S.dateFrom).fill("2025-01-05");
  await waitStateChange(page, s10);
  const failState = await stateId(page);
  const failMetrics = await readMetrics(page);
  check(failMetrics.count !== preFailMetrics.count, "指标应随新筛选更新");

  const overlayVisible = await page.locator(S.overlay("category_distribution")).isVisible();
  check(overlayVisible, "失败图应显示未完成覆盖层");
  const overlayText = await page.locator(S.overlay("category_distribution")).textContent();
  check(overlayText.includes("未完成"), "覆盖层应明确标记未完成");
  check(overlayText.includes(failState), `覆盖层应显示失败响应所属状态标识 ${failState}`);
  check((await page.locator(S.distBars).count()) === 0, "失败图不应显示旧柱子");
  check(await page.locator('[data-testid="retry-category_distribution"]').isVisible(), "失败图应提供重试按钮");
  check((await page.locator(S.candBars).count()) > 0, "候选图应正常显示");
  for (const panel of ["category_candidates", "trend", "detail"]) {
    check((await readChip(page, panel)) === failState, `${panel} 状态应为新状态`);
  }
  const compErrors = await page.evaluate(() => window.__analyticdesk.pageData.component_errors);
  check(Object.keys(compErrors).includes("category_distribution"), "页面数据应标记失败部件");
  check(await page.evaluate(() => window.__analyticdesk.pageData.category_distribution) === null, "失败部件数据应为 null");

  await page.locator('[data-testid="retry-category_distribution"]').click();
  await page.waitForFunction(
    () => document.querySelectorAll('[data-panel="category_distribution"] .bar-row').length > 0,
    null,
    { timeout: 8000 }
  );
  await waitSettled(page);
  check((await page.locator(S.distBars).count()) > 0, "重试后分布图应恢复");
  check((await readChip(page, "category_distribution")) === failState, "重试后仍显示同一状态标识");
  check(!(await page.locator(S.overlay("category_distribution")).isVisible()), "重试后覆盖层应消失");
  const distSum = await page.locator('[data-panel="category_distribution"] .bar-meta').evaluateAll((els) =>
    els.reduce((acc, el) => acc + Number(el.textContent.trim().split("条")[0]), 0)
  );
  check(distSum === failMetrics.count, `重试后分布柱条数之和 (${distSum}) 应等于指标条数 (${failMetrics.count})`);

  // ------------------------------------------------------------------ //
  section("11. 整页失败：不拼接数值，可整页重试");
  await page.locator('input[name="fail"][value="all"]').check();
  const allStateBefore = await stateId(page);
  await page.locator("#refresh-btn").click();
  // 先等整页请求真正结束（不能把加载中的遮罩误当成失败态）。
  await page.waitForFunction(
    () => document.body.dataset.busy === "0",
    null,
    { timeout: 8000 }
  );
  await page.waitForFunction(
    () => {
      const el = document.querySelector('[data-panel="metrics"] [data-overlay]');
      return el && !el.hidden && el.textContent.includes("整页未完成");
    },
    null,
    { timeout: 5000 }
  );
  const allText = await page.locator(S.overlay("metrics")).textContent();
  check(allText.includes("整页未完成"), "整页失败应明确标记");
  check((await page.locator(S.metricAmount).textContent()) === "—", "整页失败不应保留旧指标数值");
  check((await page.locator(S.detailRows).count()) === 0, "整页失败不应保留旧明细");
  const allState = await page.evaluate(() => window.__analyticdesk.view.stateId);
  check(allText.includes(allState), "整页失败覆盖层应显示当前状态标识");

  await page.locator(`${S.overlay("metrics")} button`).first().click();
  await page.waitForFunction(
    () => document.querySelector('[data-panel="metrics"] [data-overlay]').hidden,
    null,
    { timeout: 8000 }
  );
  await waitSettled(page);
  check((await readMetrics(page)).count > 0, "整页重试后应恢复数值");
  check((await readChip(page, "metrics")) === allState, "整页重试后仍是同一状态标识");

  // ------------------------------------------------------------------ //
  section("12. 分页不重不漏");
  await setDates(page, "2025-01-01", "2025-02-15");
  await page.locator("#page-size").selectOption("50");
  await waitSettled(page);
  const totalMatch = (await page.locator(S.pageInfo).textContent()).match(/共\s*(\d+)/);
  const totalRows = totalMatch ? Number(totalMatch[1]) : -1;
  check(totalRows === 360, `恢复全量应 360 条，得到 ${totalRows}`);
  const collected = [];
  let guard = 0;
  while (true) {
    const rows = await page
      .locator(S.detailRows)
      .evaluateAll((els) => els.map((e) => e.dataset.testid.replace("detail-row-", "")));
    collected.push(...rows);
    const info = await page.locator(S.pageInfo).textContent();
    const m = info.match(/第\s*(\d+)\s*\/\s*([0-9]+)/);
    if (!m || Number(m[1]) >= Number(m[2])) break;
    await page.locator("#next-page").click();
    await waitSettled(page);
    if (++guard > 20) break;
  }
  check(collected.length === totalRows, `翻页收集 ${collected.length} 条应等于 ${totalRows}`);
  check(new Set(collected).size === collected.length, "翻页出现重复记录");
  // 与固定排序（日期→记录号）的前几条一致性：验证稳定排序。
  const sortedFirst = collected.slice(0, 20);
  check(
    sortedFirst.includes("R0000") || sortedFirst.includes("R0001"),
    "首页应包含最早日期的记录（稳定排序）"
  );

  // ------------------------------------------------------------------ //
  section("13. 页面无 JS 异常");
  check((await page.locator("#request-log tr").count()) > 0, "请求日志应有记录");
  check(pageErrors.length === 0, `不应有页面 JS 异常：${pageErrors.join(";")}`);

  await browser.close();

  console.log("\n" + "=".repeat(60));
  if (FAILS) {
    console.log(`UI 验证失败 ${FAILS} 项 / 共 ${RESULTS.length} 项`);
    process.exit(1);
  }
  console.log(`UI 验证全部通过：${RESULTS.length} 项`);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
