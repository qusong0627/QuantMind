/**
 * 因子挖掘「结果区 + 因子库」机构级 E2E（真实后端 / 真数据 / 真 POST）。
 *
 * 用法：QM_BASE=http://localhost:3080 node tests/alpha-research-results.e2e.mjs
 *      QM_BT_TIMEOUT_MS=600000 可放宽真实回测终态等待（默认 10 分钟）
 *
 * 为什么主战场是「因子库」而不是「本轮挖掘因子」：
 *   TaskContext 只在后端存在 running 任务时恢复挖掘台（HEAD 既有策略，见
 *   context-v2/TaskContext.tsx 的 miningRecoveredRef 段）——已完成任务的结果区
 *   属于会话内视图。因子库是持久面，且与挖掘结果区共用同一套
 *   FactorTable / MaterializeBar / RunQueueContext 组件与数据口径，
 *   用户动作可在此确定性复现；挖掘结果区本身由 vitest（TaskContext /
 *   FactorTable / MiningDashboardPage.quickbacktest）覆盖。
 *
 * 覆盖（对应用户实测的每一条不满）：
 *  1. 全量清单：DOM 行数 == GET /factors 响应行数（且 >10），不再是 Top10/20 截断
 *  2. 指标口径：不再出现旧实现的伪值「N/A」；缺失行有「—」；manifest chips 如实
 *  3. 两个被移除的入口（「AI 解读」「导出IDE」）不复现
 *  4. 视图切换：默认列表 → 卡片 → 刷新后仍卡片 → 切回列表（localStorage 持久）
 *  5. 物化真链路：选中「已物化」+「重复被拒」行 → POST /factors/materialize →
 *     started:false（未起进程）+ 跳过明细摊开（已物化 / 值级重复被拒（需 force 重跑））
 *  6. 行级回测失败原文：page.route 桩 400 detail → 行内「失败」+ title 原文
 *  7. 行级回测真链路：真发 POST → 行内「回测中」→ 终态「已完成」→「看图表」
 *  8. pageerror / console.error 为 0（**自登录页首屏全程采集**：未登录时不得
 *     出现无 token 的 /tasks 轮询——MiningTaskMonitor 的 enabled 闸门）；
 *     未预期 4xx/5xx（除桩）为 0
 */
import { chromium } from 'playwright';

const BASE = process.env.QM_BASE || 'http://localhost:3080';
const BT_TIMEOUT_MS = Number(process.env.QM_BT_TIMEOUT_MS || 10 * 60 * 1000);

let pass = 0;
let total = 0;
const check = (ok, label) => {
  total++;
  if (ok) pass++;
  console.log(`${ok ? '✓' : '✗'} ${label}`);
};
const flat = (s) => s.replace(/\s+/g, ' ');

const pageErrors = [];
const apiErrors = [];
/**
 * 噪音采集自登录页起全程开启——包括未登录首屏。
 * 这条纪律抓过一个真 bug（2026-10-09）：壳上常驻的 MiningTaskMonitor 在
 * 公开路由（登录页）也挂载，无 token 每 5 秒打一枪 GET /alpha-agent/tasks
 * → 401 + 控制台报错。修复 = 壳传 enabled={isAuthenticated}（该组件第四条
 * 取舍），本探针从此不设「登录前豁免」。
 */
/** 桩 400 的 URL 片段（收集 API 错误时豁免——这是被测行为本身） */
let stubUrlPart = null;
/**
 * 桩生效窗口。桩 400 必然连带两条 console 噪音（Chrome 资源日志 + 应用全局
 * axios 拦截器日志，后者不带 URL），只在该窗口内按「写着 400」豁免；
 * 行内「失败 + detail 原文」的行为断言在 Step 4 单独覆盖，不靠 console。
 */
let stubArmed = false;

const browser = await chromium.launch({
  executablePath: '/usr/bin/google-chrome-stable',
  args: ['--no-sandbox'],
});
const page = await (
  await browser.newContext({ viewport: { width: 1720, height: 1100 } })
).newPage();
page.on('pageerror', (e) => pageErrors.push('[PAGEERROR] ' + (e.message || '').slice(0, 250)));
page.on('console', (m) => {
  if (m.type() !== 'error') return;
  const text = m.text();
  const loc = (m.location()?.url) || '';
  if (stubArmed && (loc.includes(stubUrlPart || '\u0000') || /status code 400|status of 400/.test(text))) {
    return; // 桩 400 的连带噪音（预期行为本身）
  }
  pageErrors.push('[console] ' + text.slice(0, 250));
});

/** 因子清单响应（因子库装载时抓）：{n, status, body} */
let factorsResp = { n: 0, status: 0, body: null };
/** 因子工厂产出（library 装载时 Promise.all 并发拉取；前端把它并进列表，只读） */
let factoryResp = { n: 0, status: 0, body: null };
page.on('response', async (r) => {
  const u = r.url();
  if (/\/api\/v1\/alpha-agent\/factors(\?|$)/.test(u)) {
    try {
      factorsResp = { n: factorsResp.n + 1, status: r.status(), body: await r.json() };
    } catch {
      /* 非 JSON 不管 */
    }
    return;
  }
  if (/\/api\/v1\/alpha-agent\/factory-factors/.test(u)) {
    try {
      factoryResp = { n: factoryResp.n + 1, status: r.status(), body: await r.json() };
    } catch {
      /* 非 JSON 不管 */
    }
    return;
  }
  if (/\/api\/v1\/alpha-agent\//.test(u) && r.status() >= 400) {
    if (stubUrlPart && u.includes(stubUrlPart)) return;
    apiErrors.push(r.status() + ' ' + u.slice(0, 160));
  }
});

/** 等因子清单（+工厂）响应计数到达 want 次（新装载必然重发） */
async function waitFactorsResp(want) {
  const t0 = Date.now();
  while (
    (factorsResp.n < want || factoryResp.n < want) &&
    Date.now() - t0 < 40000
  ) {
    await page.waitForTimeout(300);
  }
  return factorsResp.n >= want ? factorsResp : null;
}

// ---- 登录 ----
await page.goto(`${BASE}/`, { waitUntil: 'domcontentloaded', timeout: 40000 });
await page.waitForTimeout(3500);
if ((await page.locator('input[type=password]').count()) > 0) {
  await page.locator('input').nth(0).fill('admin');
  await page.locator('input[type=password]').first().fill('admin123');
  const btns = page.locator('button');
  for (let i = 0; i < (await btns.count()); i++) {
    const t = (await btns.nth(i).innerText().catch(() => '')).replace(/\s/g, '');
    if (/登录/.test(t)) {
      await btns.nth(i).click();
      break;
    }
  }
  await page.waitForTimeout(7000);
}
console.log('登录后:', page.url());

/** 首启「投资适当性评估」弹窗会遮住整页：走「稍后再答」 */
async function dismissRiskModal() {
  const later = page.locator('.ant-modal button', { hasText: '稍后再答' }).first();
  const shown = await later
    .waitFor({ state: 'visible', timeout: 4000 })
    .then(() => true)
    .catch(() => false);
  if (shown) {
    await later.click().catch(() => {});
    await page.waitForTimeout(800);
  }
}

/** 进 alpha-research（HashRouter 深链必须带 #/）→ 首页水合。
 *  注意：page.goto 到完全相同的 URL+hash 是同文档 no-op（不会重挂应用），
 *  必须显式 reload 才是「刷新」语义。 */
async function gotoHome() {
  await page.goto(`${BASE}/#/alpha-research`, { waitUntil: 'domcontentloaded' });
  await page.reload({ waitUntil: 'domcontentloaded' });
  await dismissRiskModal();
  await page.locator('text=因子库管理').first().waitFor({ state: 'visible', timeout: 30000 });
  await page.waitForTimeout(600);
}

/** 首页 → 因子库（列表/卡片视图页面） */
async function enterLibrary(expectNth) {
  await page.locator('text=因子库管理').first().click();
  await page.waitForTimeout(400);
  return waitFactorsResp(expectNth);
}

// ============================================================
// 1. 首页 + 因子库全量清单
// ============================================================
await gotoHome();
const homeText = flat(await page.locator('body').innerText());
check(homeText.includes('进入实时演化台'), '挖掘台入口存在（首页 → 演化台）');
check(!homeText.includes('AI 解读') && !homeText.includes('导出IDE'), '首页无「AI 解读」「导出IDE」');

const lib1 = await enterLibrary(1);
check(!!lib1, '进入因子库触发 GET /factors');
const rows = lib1?.body?.data?.factors ?? [];
check(rows.length > 10, `全量清单响应 ${rows.length} 行（>10，不再是 Top10 截断）`);
const factoryCount = factoryResp.body?.data?.factors?.length ?? 0;
console.log(`  · 工厂只读因子 ${factoryCount} 个（并入列表）`);

await page.locator('tbody tr').first().waitFor({ state: 'visible', timeout: 30000 });
const domRows = await page.locator('tbody tr').count();
check(
  domRows === rows.length + factoryCount,
  `DOM 行数 ${domRows} == 挖掘 ${rows.length} + 工厂 ${factoryCount}（挖多少显示多少）`,
);

let bodyText = flat(await page.locator('body').innerText());
check(bodyText.includes(`共 ${domRows} 行`), `工具栏计数「共 ${domRows} 行」如实`);
check(
  !bodyText.includes('AI 解读') && !bodyText.includes('导出IDE'),
  '因子库无「AI 解读」「导出IDE」（两个入口已移除）',
);
check(!/没有可回测因子/.test(bodyText), '（信息）无异常空态文案');

const tbodyText = await page.locator('tbody').innerText();
check(!tbodyText.includes('N/A'), '旧实现伪值「N/A」不再出现（缺失一律「—」）');
const hasMissing = rows.some(
  (r) => r.rank_ic == null && (r.metadata?.rank_ic ?? null) == null,
);
if (hasMissing) {
  check(tbodyText.includes('—'), `缺失指标渲染为「—」（本批有 ${rows.filter((r) => r.rank_ic == null && (r.metadata?.rank_ic ?? null) == null).length} 个缺 RankIC）`);
}
const matChips = tbodyText.match(/(已物化|重复被拒|门禁拒入|物化失败)/g) ?? [];
check(matChips.length > 0, `manifest 状态 chips 如实渲染（${matChips.length} 个）`);

// localStorage 默认列表
const view0 = await page.evaluate(() => localStorage.getItem('qa_factor_lib_view'));
check(view0 === 'list', `首次进入默认列表视图（qa_factor_lib_view=${view0}）`);

// ============================================================
// 2. 视图切换 + 刷新持久化
// ============================================================
await page.getByTitle('卡片视图').click();
const view1 = await page.evaluate(() => localStorage.getItem('qa_factor_lib_view'));
check(view1 === 'cards', '切到卡片视图 → localStorage=cards');
check((await page.locator('tbody').count()) === 0, '卡片视图下密集表消失');

await gotoHome();
const lib2 = await enterLibrary(2);
check(!!lib2, '重新进入因子库（刷新模拟）再次装载');
await page.getByTitle('卡片视图').waitFor({ state: 'visible', timeout: 20000 });
const pressed = await page.getByTitle('卡片视图').getAttribute('aria-pressed');
const view2 = await page.evaluate(() => localStorage.getItem('qa_factor_lib_view'));
check(pressed === 'true' && view2 === 'cards', `刷新后仍是卡片视图（aria-pressed=${pressed}）`);

await page.getByTitle('列表视图').click();
await page.locator('tbody tr').first().waitFor({ state: 'visible', timeout: 20000 });
const view3 = await page.evaluate(() => localStorage.getItem('qa_factor_lib_view'));
check(view3 === 'list', '切回列表视图 → localStorage=list');

// ============================================================
// 3. 物化真链路（选「已物化」+「重复被拒」→ 真 POST → 如实摘要）
// ============================================================
let rowMat = page.locator('tbody tr', { hasText: '已物化' }).first();
let rowDup = page.locator('tbody tr', { hasText: '重复被拒' }).first();
if ((await rowDup.count()) === 0) rowDup = rowMat; // 数据波动兜底：仅测单行
await rowMat.locator('input[type=checkbox]').click();
if ((await rowDup.count()) > 0) await rowDup.locator('input[type=checkbox]').click();
await page.waitForTimeout(300);
bodyText = flat(await page.locator('body').innerText());
check(/已选 \d+/.test(bodyText), '勾选后操作条显示已选数');

const [matResp] = await Promise.all([
  page.waitForResponse(
    (r) => r.url().includes('/alpha-agent/factors/materialize') && r.request().method() === 'POST',
    { timeout: 30000 },
  ),
  page.locator('button', { hasText: '物化选中' }).click(),
]);
const matBody = await matResp.json().catch(() => null);
check(matResp.status() === 200 && matBody?.data, `POST /factors/materialize → ${matResp.status()}`);
check(
  matBody?.data?.started === false,
  '终态因子不会被重复物化（started:false，未起进程）',
);
await page.waitForTimeout(500);
bodyText = flat(await page.locator('body').innerText());
check(bodyText.includes('没有可物化的因子'), '物化条如实给出「没有可物化的因子（原因见明细）」');
check(/跳过 \d+ 个/.test(bodyText), '跳过计数摊开（不静默吞）');
check(
  bodyText.includes('已物化') &&
    (bodyText.includes('值级重复被拒（需 force 重跑）') || (await rowDup.count()) === 0),
  '跳过明细展开到具体原因（已物化 / 值级重复被拒（需 force 重跑））',
);
check(!bodyText.includes('物化运行中'), '未谎报「物化运行中」（只信服务端确认）');

await page.locator('button', { hasText: '清除' }).click();
await page.waitForTimeout(300);
check(
  await page.locator('button', { hasText: '物化选中' }).isDisabled(),
  '无选中时「物化选中」禁用',
);

// ============================================================
// 4+5. 行级回测：桩 400 失败原文 + 真链路三态
// ============================================================
// 两条取行纪律（都被真数据咬过）：
// 1. 行定位按 factor_name 标题匹配，重名会让点击落到别的行（实测 185 行里
//    9 组重名：Momentum_5D ×3 等）——只选名字在清单里唯一的因子；
// 2. 清单以**最近一次**响应为准：第 1 步的 rows 快照之后还有刷新模拟/
//    物化结算/回测结算的后台重拉，且后台回填在持续改 rank_ic 排序。
const rowsNow = factorsResp.body?.data?.factors ?? rows;
const nameCount = new Map();
for (const r of rowsNow) nameCount.set(r.factor_name, (nameCount.get(r.factor_name) ?? 0) + 1);
const eligible = rowsNow.filter(
  (r) =>
    r.user_id != null &&
    !r.readOnly &&
    nameCount.get(r.factor_name) === 1 &&
    ((r.factor_code ?? '').length > 0 || (r.factor_formulation ?? '').length > 0),
);
check(eligible.length >= 2, `可回测因子 ≥2（${eligible.length} 个）`);
const pickB = eligible[0];
const pickA = eligible[1];
const rowOf = (name) => page.locator('tbody tr').filter({ has: page.getByTitle(`${name}（点击查看详情）`) });

// -- 4. 桩 400：失败原文必须上屏（不能只吞）--
stubUrlPart = `/alpha-agent/factors/${pickB.factor_id}/backtest`;
stubArmed = true;
await page.route(`**${stubUrlPart}*`, (route) =>
  route.fulfill({
    status: 400,
    contentType: 'application/json',
    body: JSON.stringify({ detail: '因子不存在或无权访问' }),
  }),
);
const rowB = rowOf(pickB.factor_name);
await rowB.locator('button', { hasText: '回测' }).first().click();
const chipB = rowB.locator('span').filter({ hasText: /^失败$/ }).first();
await chipB.waitFor({ state: 'visible', timeout: 20000 }).catch(() => {});
const chipBText = await chipB.innerText().catch(() => '');
const chipBTitle = await chipB.getAttribute('title').catch(() => '');
check(
  chipBText === '失败' && (chipBTitle ?? '').includes('因子不存在或无权访问'),
  `桩 400：行内「失败」+ FastAPI detail 原文（title="${chipBTitle}"）`,
);
await page.waitForTimeout(1200); // 让桩的连带 console 噪音落地后再关窗
stubArmed = false;

// -- 5. 真链路 --
const rowA = rowOf(pickA.factor_name);
const [btResp] = await Promise.all([
  page.waitForResponse(
    (r) =>
      r.request().method() === 'POST' && r.url().includes(`/alpha-agent/factors/${pickA.factor_id}/backtest`),
    // 引擎 quick-backtest 是**同步**计算（POST 挂完整回测时长才回）：实测
    // 26–32s（71k 行 × 54k 样本，b59e8036 一次 31.6s）——30s 超时卡在临界点上
    // 会偶发假失败（2026-10-09 实测咬过一次）。给足余量；终态另有 BT_TIMEOUT_MS。
    { timeout: 180000 },
  ),
  rowA.locator('button', { hasText: '回测' }).first().click(),
]);
check(btResp.status() === 200, `POST /factors/{id}/backtest → ${btResp.status()}（真发起）`);

const chipRunning = rowA.locator('span').filter({ hasText: /^回测中$/ }).first();
await chipRunning.waitFor({ state: 'visible', timeout: 20000 }).catch(() => {});
check((await rowA.locator('span').filter({ hasText: /^回测中$/ }).count()) > 0, '行内状态「回测中」就地亮起');

const t0 = Date.now();
const term = rowA.locator('span').filter({ hasText: /^(已完成|失败)$/ }).first();
await term.waitFor({ state: 'visible', timeout: BT_TIMEOUT_MS }).catch(() => {});
const termText = await term.innerText().catch(() => '');
const elapsedS = ((Date.now() - t0) / 1000).toFixed(0);
if (termText === '失败') {
  const reason = await term.getAttribute('title').catch(() => '');
  console.log(`  · 回测失败原因: ${reason}`);
}
check(termText === '已完成', `真实回测终态「${termText || '超时'}」（用时 ${elapsedS}s）`);
if (termText === '已完成') {
  check(
    (await rowA.locator('button', { hasText: '看图表' }).count()) > 0,
    '完成后行内出现「看图表」（可跳回测页）',
  );
}

// ============================================================
// 6. 全局噪音
// ============================================================
if (pageErrors.length) console.log('  · pageerror/console.error 样例:', pageErrors.slice(0, 3));
check(pageErrors.length === 0, `pageerror / console.error 为 0（实测 ${pageErrors.length}）`);
if (apiErrors.length) console.log('  · API 错误样例:', apiErrors.slice(0, 3));
check(apiErrors.length === 0, `未预期 4xx/5xx 为 0（实测 ${apiErrors.length}，桩 400 已豁免）`);

console.log(`\n==== ${pass}/${total} 通过 ====`);
await browser.close();
process.exit(pass === total ? 0 : 1);
