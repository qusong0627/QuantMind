/**
 * 「实盘控制台开关已打开」验收探针（2026-09-24）。
 *
 * 背景：构建时未注入 `VITE_ENABLE_REAL_TRADING=true` 的产物里，`/live` 会渲染
 * `LiveDisabledPage`（文案「实盘控制台未启用」）——整页不渲染。用户点名此状态，
 * 本次用 `VITE_ENABLE_REAL_TRADING=true bash scripts/deploy_frontend.sh --allow-local-live`
 * 重建后，本探针断的就是「开关真的在产物里生效」：
 *
 *   1) 兜底页**不在**：`#/live` 文本里不得出现「实盘控制台未启用」/「本次构建的前端开关」；
 *   2) 控制台**在**：至少 4 个 consoleTabs 页签按钮可见（系统健康/候选信号/评估中心/
 *      策略管理/手动任务/持仓监控/交易记录/个人中心/设置）——这是开关打开态的**专属证据**
 *      （兜底页只有一张 Card，一个页签都没有）；
 *   3) 账户面**真的连着**：捕获 `GET /real-trading/account` 的响应，至少一条 200；
 *   4) 全程零 pageerror。
 *
 * 反假通过：登录不成功 → 页签数=0 → 断言 2 失败，探针整体 exit 1（零参与即失败）。
 *
 * 跑法：
 *   PROBE_BASE=http://localhost:3080 node electron/tests/probe_live_flag_on.mjs
 * ⚠️ HashRouter：深链一律带 `#/`。
 */
import { chromium } from 'playwright';

const BASE = process.env.PROBE_BASE || 'http://localhost:3080';
const USER = process.env.PROBE_USER || 'admin';
const PASS = process.env.PROBE_PASS || 'admin123';

const CONSOLE_TABS = [
  '系统健康', '候选信号', '评估中心', '策略管理', '手动任务',
  '持仓监控', '交易记录', '个人中心', '设置',
];
const DISABLED_MARKERS = ['实盘控制台未启用', '本次构建的前端开关'];

const browser = await chromium.launch({
  executablePath: '/usr/bin/google-chrome-stable',
  args: ['--no-sandbox'],
});
const page = await browser.newPage({ viewport: { width: 1680, height: 1000 } });

const errs = [];
const acctCalls = [];
page.on('pageerror', (e) => errs.push('[PAGEERROR] ' + (e.message || '').slice(0, 220)));
page.on('response', (r) => {
  if (/\/real-trading\/account\b/.test(r.url())) {
    acctCalls.push({ status: r.status(), url: r.url().replace(BASE, '') });
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
  results.push({ name, pass, detail });
  console.log(`${pass ? '✅' : '❌'} ${name}${detail ? `  — ${detail}` : ''}`);
};

// ── 登录 ──────────────────────────────────────────────────────────────────
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

// ── 深链实盘控制台 ────────────────────────────────────────────────────────
await page.goto(`${BASE}/#/live`, { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(9000);
await dismissModals();

const bodyText = await text(page.locator('body'));

// 1) 兜底页不在
const hitDisabled = DISABLED_MARKERS.filter((m) => bodyText.includes(m));
check('兜底页不在（无「实盘控制台未启用」文案）', hitDisabled.length === 0,
  hitDisabled.length ? `命中: ${hitDisabled.join(' / ')}` : '零命中');

// 2) 控制台在：页签按钮 ≥4 个
const tabFound = [];
for (const t of CONSOLE_TABS) {
  const n = await page.locator('button', { hasText: new RegExp(`^${t}$`) }).count();
  if (n > 0) tabFound.push(t);
}
check('控制台页签可见 ≥4', tabFound.length >= 4,
  `命中 ${tabFound.length}/${CONSOLE_TABS.length}: ${tabFound.join('、') || '（无——登录失败或仍是兜底页）'}`);

// 3) 账户面连通
const okAcct = acctCalls.filter((c) => c.status === 200);
check('GET /real-trading/account 有 200 响应', okAcct.length >= 1,
  acctCalls.length ? acctCalls.map((c) => `${c.status}`).join(',') : '未观察到该请求');

// 4) 零 pageerror
check('全程零 pageerror', errs.length === 0, errs.slice(0, 3).join(' | ') || '干净');

await browser.close();
const failed = results.filter((r) => !r.pass);
console.log(`\n${failed.length === 0 ? 'PASS' : 'FAIL'}  ${results.length - failed.length}/${results.length}`);
process.exit(failed.length === 0 ? 0 : 1);
