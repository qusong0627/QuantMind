/**
 * A6 死路处置验收探针（2026-09-30；分析触发复活后更新 2026-10-08）。
 *
 * 断的是改写后的**可观察行为**（全按 DOM 文本与网络请求，不靠肉眼）：
 *   1) 六个死通道取数函数（富途 account/account-both/orders/closed、IBKR
 *      account/orders）改为**直接失败**：hk/us 市场下全程 **零 futu/ibkr 请求**
 *      （不再打不存在的上游、不再产生 404 噪声）；
 *   2) hk/us 的「实盘」「详情」「已完成」tab 显示占位文案（通道已下线），不空白、
 *      不报错；「持仓」tab 走既有回退链（模拟盘回放）不炸；
 *   3) 两枚「立即分析」按钮（对话 tab / 新闻 tab）**已复活为可用**（2026-10-08）：
 *      对话按钮点击后必须真的发 `POST /analysis/trigger`（type=live）并 2xx，
 *      DOM 出现「已触发」回执；新闻按钮同样 enabled（点击会真跑一轮新闻管线，
 *      探针不点它——正向链路由对话按钮覆盖，避免每次跑探针都耗一轮 LLM）。
 *      旧的 `/live/analyze`、`/news/analyze` 仍是死路径（零请求）。
 *   4) 全程零 pageerror、arena 请求零 5xx。
 *
 * ## 反「假通过」硬规矩（沿用同目录 tabs/deep-click 探针的口径）
 *
 * - **市场切换必须验激活**：点 hk/us 后要复查按钮拿到 `active` 类，否则后面
 *   「找不到占位文案」可能只是没切过去——两件事必须分开证。
 * - **零请求不算数，除非有请求发生**：用 arena 请求计数器做正对照（>20 次），
 *   证明页面真的在拉数据；「零 futu 请求」是在这个前提下才成立的结论。
 * - **触发按钮查「真请求 + 真回执」**：只查 enabled 属性会放过「点了没反应」
 *   （旧 A6 的假通过形态）；必须抓到 /analysis/trigger 的 2xx 响应 + 按钮旁
 *   出现「已触发」文案。注意：点一下会真入队一轮模型对话（去重保证不堆积）。
 *
 * 打 3080（quantmind-web 容器，部署产物）还是 3000（vite dev）都行：
 *   PROBE_BASE=http://localhost:3000 node electron/tests/probe_arena_a6_deadpaths.mjs
 */
import { chromium } from 'playwright';

const BASE = process.env.PROBE_BASE || 'http://localhost:3080';
const USER = process.env.PROBE_USER || 'admin';
const PASS = process.env.PROBE_PASS || 'admin123';

const browser = await chromium.launch({
  executablePath: '/usr/bin/google-chrome-stable',
  args: ['--no-sandbox'],
});
const page = await browser.newPage({ viewport: { width: 1680, height: 1000 } });

const errs = [];
/** A6 声称「不发请求」的三条死路（命中任何一条即失败）：
 *  futu/ibkr 六函数与 live/news analyze 两触发，均经 /api/v1/agent-arena 前缀 */
const DEAD_PATH_RE = /\/api\/v1\/agent-arena\/(futu|ibkr)\/|\/api\/v1\/agent-arena\/(live|news)\/analyze/;
const deadCalls = [];
const arena5xx = [];
let arenaCalls = 0;
/** 「立即分析」正向证据：/analysis/trigger 的请求体与响应码（复活后必须有真请求） */
const triggerReqs = [];
const triggerResps = [];
page.on('pageerror', (e) => errs.push('[PAGEERROR] ' + (e.message || '').slice(0, 220)));
page.on('request', (r) => {
  const u = r.url();
  if (DEAD_PATH_RE.test(u)) deadCalls.push(u.replace(BASE, ''));
  if (/\/api\/v1\/agent-arena\/analysis\/trigger/.test(u)) {
    triggerReqs.push({ url: u.replace(BASE, ''), body: r.postData() || '' });
  }
  if (/\/api\/v1\/agent-arena\//.test(u)) arenaCalls += 1;
});
page.on('response', (r) => {
  const u = r.url();
  if (/\/api\/v1\/agent-arena\/analysis\/trigger/.test(u)) triggerResps.push(r.status());
  if (/\/api\/v1\/agent-arena\//.test(u) && r.status() >= 500) {
    arena5xx.push(`${r.status()} ${u.replace(BASE, '')}`);
  }
});

const text = async (loc) => ((await loc.innerText().catch(() => '')) || '').replace(/\s/g, '');
const clickByText = async (re, scope = 'button') => {
  const items = page.locator(scope);
  for (let i = 0, n = await items.count(); i < n; i++) {
    if (re.test(await text(items.nth(i)))) { await items.nth(i).click({ timeout: 4000 }); return true; }
  }
  return false;
};

const dismissModals = async () => {
  for (let round = 0; round < 8; round++) {
    const blocking = await page.evaluate(
      () => Array.from(document.querySelectorAll('.ant-modal-wrap'))
        .filter((m) => getComputedStyle(m).display !== 'none').length,
    );
    if (!blocking) return true;
    if (await clickByText(/稍后再答|同意|确认|我知道了|已阅读|知道了|跳过/, '.ant-modal:visible button')) {
      await page.waitForTimeout(800);
      continue;
    }
    await page.keyboard.press('Escape').catch(() => {});
    await page.waitForTimeout(600);
  }
  return false;
};

const results = [];
const check = (name, pass, detail = '') => {
  results.push({ name, pass });
  console.log(`${pass ? '✅' : '❌'} ${name}${detail ? `  — ${detail}` : ''}`);
};

/** arena 作用域内文本（去空白）；不存在时返回 '' */
const arenaText = async () =>
  page.evaluate(() => {
    const root = document.querySelector('.qm-arena-root');
    return root ? root.innerText.replace(/\s+/g, '') : '';
  });

/** 市场切换并**确认真的激活**（active 类） */
const switchMarket = async (m) => {
  const btn = page.locator(`.qm-arena-root .market-switcher button.${m}`);
  if (!(await btn.count())) return false;
  await btn.first().click({ timeout: 4000 }).catch(() => {});
  await page.waitForTimeout(2500);
  return page.evaluate(
    (mm) => !!document.querySelector(`.qm-arena-root .market-switcher button.${mm}.active`),
    m,
  );
};

/** 点右侧面板 tab（label 用不歧义子串：「实盘」「详情」「持仓」「已完成」「模型对话」「新闻」） */
const clickTradeTab = async (label, waitMs = 1800) => {
  const btn = page.locator('.qm-arena-root button.trade-tab').filter({ hasText: label }).first();
  if (!(await btn.count())) return false;
  await btn.click({ timeout: 4000 }).catch(() => {});
  await page.waitForTimeout(waitMs);
  return true;
};

// ── 登录 + 进实盘栏 ────────────────────────────────────────────────────────
await page.goto(`${BASE}/#/auth/login`, { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(4000);
const inputs = page.locator('input:visible');
if ((await inputs.count()) >= 2) {
  await inputs.nth(0).fill(USER);
  await inputs.nth(1).fill(PASS);
  await clickByText(/登录/);
  await page.waitForTimeout(6000);
}
await dismissModals();
await page.goto(`${BASE}/#/live`, { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(8000);
await dismissModals();

// 「实况」栏（arena Live 页：市场切换 + 右侧成交/持仓/实盘/详情/对话/新闻 tabs）
{
  const btn = page.locator('button').filter({ hasText: /^实况$/ }).first();
  await btn.click({ timeout: 8000 }).catch(async () => {
    await dismissModals();
    await btn.click({ force: true });
  });
  await page.waitForTimeout(14000);
  await dismissModals();
}
check('arena 根节点就位（.qm-arena-root）', (await arenaText()).length > 0);
check('市场切换器就位（.market-switcher）', (await page.locator('.qm-arena-root .market-switcher button').count()) === 3);

// ── 港股：死通道占位 ──────────────────────────────────────────────────────
check('切到港股且按钮真的激活', await switchMarket('hk'));
if (await clickTradeTab('实盘', 2500)) {
  const t = await arenaText();
  check('港股「实盘」tab 显示占位：「通道已下线：富途」', t.includes('通道已下线：富途'), t.includes('通道已下线：富途') ? '' : `作用域文本 ${t.length} 字`);
} else {
  check('港股「实盘」tab 存在', false, '未找到 trade-tab「实盘」');
}
if (await clickTradeTab('详情', 2500)) {
  const t = await arenaText();
  check('港股「详情」tab 通道状态：「通道已下线：未随平台迁移」', t.includes('通道已下线：未随平台迁移'));
} else {
  check('港股「详情」tab 存在', false, '未找到 trade-tab「详情」');
}
check('港股「持仓」tab 点得动（回退链不炸）', await clickTradeTab('持仓', 2500));
if (await clickTradeTab('已完成', 2500)) {
  const t = await arenaText();
  check('港股「已完成」空态：「港股富途通道已下线」', t.includes('港股富途通道已下线'));
} else {
  check('港股「已完成」tab 存在', false, '未找到 trade-tab「已完成」');
}

// ── 美股：死通道占位 ──────────────────────────────────────────────────────
check('切到美股且按钮真的激活', await switchMarket('us'));
if (await clickTradeTab('实盘', 2500)) {
  const t = await arenaText();
  check('美股「实盘」tab 显示占位：「通道已下线：盈透证券」', t.includes('通道已下线：盈透证券'), t.includes('通道已下线：盈透证券') ? '' : `作用域文本 ${t.length} 字`);
} else {
  check('美股「实盘」tab 存在', false, '未找到 trade-tab「实盘」');
}
if (await clickTradeTab('详情', 2500)) {
  const t = await arenaText();
  check('美股「详情」tab 通道状态：「通道已下线：未随平台迁移」', t.includes('通道已下线：未随平台迁移'));
} else {
  check('美股「详情」tab 存在', false, '未找到 trade-tab「详情」');
}

// 美股「新闻」tab 是**上游原设计**的 cn-only 分支：非 cn 只显示静态「关键词『…』」，
// 根本没有触发按钮（此断言把这些行为存档，防以后误读成 A6 漏改）
if (await clickTradeTab('新闻', 2200)) {
  const t = await arenaText();
  check('美股「新闻」tab 为关键词静态条（cn-only 设计，无按钮）', t.includes('关键词'), '');
} else {
  check('美股「新闻」tab 存在', false, '未找到 trade-tab「新闻」');
}

// ── 「立即分析」两按钮：复活后 = 对话点击真触发 + 新闻可用（仅 cn 有） ───────
check('切回 A 股且按钮真的激活', await switchMarket('cn'));
const analyzeBtnProbe = async (tabLabel) => {
  if (!(await clickTradeTab(tabLabel, 2200))) {
    // 现场取证：列出当时可见的 tab 文案，区分「tab 不存在」与「tab 在但按钮不在」
    const tabs = await page.locator('.qm-arena-root button.trade-tab').allInnerTexts().catch(() => []);
    return { found: false, tabs: tabs.map((s) => s.replace(/\s/g, '')) };
  }
  const btns = page.locator('.qm-arena-root button.analyze-trigger');
  if (!(await btns.count())) return { found: false };
  const b = btns.first();
  const disabled = await b.isDisabled().catch(() => false);
  const label = (await b.innerText().catch(() => '')).replace(/\s/g, '');
  return { found: true, disabled, label, btn: b };
};
{
  // 对话 tab：点一下必须真发 POST /analysis/trigger（type=live）→ 2xx +「已触发」回执。
  // 注意：这会真入队一轮模型对话（去重保证 pending 期间不堆积），是其功能本体验收。
  const a = await analyzeBtnProbe('模型对话');
  check('对话 tab「立即分析」按钮存在且可用', a.found && !a.disabled, JSON.stringify({ found: a.found, disabled: a.disabled, label: a.label }));
  check('对话 tab 按钮文案不再含「该能力未迁移」', !!a.found && !/该能力未迁移/.test(a.label || ''));
  if (a.found && !a.disabled) {
    const before = triggerReqs.length;
    await a.btn.click({ timeout: 4000 }).catch(() => {});
    await page.waitForTimeout(3500);
    const req = triggerReqs[triggerReqs.length - 1];
    check('点击后真发 POST /analysis/trigger（type=live）',
      triggerReqs.length > before && /"type"\s*:\s*"live"/.test(req?.body || ''),
      triggerReqs.length > before ? req?.body : '(无请求)');
    const t = await arenaText();
    check('触发后出现「已触发」回执文案', /已触发/.test(t), /已触发/.test(t) ? '' : `作用域文本 ${t.length} 字`);
    check('触发请求拿到 2xx', triggerResps.length > 0 && triggerResps.every((s) => s >= 200 && s < 300), `status=${triggerResps.join(',')}`);
  } else {
    check('点击后真发 POST /analysis/trigger（type=live）', false, '按钮不可用');
    check('触发后出现「已触发」回执文案', false, '按钮不可用');
    check('触发请求拿到 2xx', false, '按钮不可用');
  }
}
{
  // 新闻 tab：仅断可用（点击=真跑一轮新闻管线，探针不点它；正向链路由对话按钮覆盖）
  const a = await analyzeBtnProbe('新闻');
  check('新闻 tab「立即分析」按钮存在且可用', a.found && !a.disabled, JSON.stringify({ found: a.found, disabled: a.disabled, label: a.label }));
  check('新闻 tab 按钮文案为上游原件（无「该能力未迁移」）', !!a.found && a.label === '⚡立即分析', a.label);
}

// ── 总结断言 ──────────────────────────────────────────────────────────────
check('arena 请求有真实发生（正对照，防零项假通过）', arenaCalls > 20, `共 ${arenaCalls} 次`);
check('全程零 futu/ibkr 与旧 /live|/news/analyze 死路请求', deadCalls.length === 0, deadCalls.slice(0, 6).join(' · '));
check('arena 请求无 5xx', arena5xx.length === 0, arena5xx.slice(0, 6).join(' · '));
check('全程零 pageerror', errs.length === 0, errs.slice(0, 4).join(' | '));

await browser.close();
const failed = results.filter((r) => !r.pass);
console.log(`\n${results.length - failed.length}/${results.length} 通过`);
process.exitCode = failed.length ? 1 : 0;
