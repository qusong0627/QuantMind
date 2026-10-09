/**
 * 「QuantBot 落在实盘栏」验收探针（2026-09-22 用户指定落点）。
 *
 * 2026-09-23 用户改口「设置放最低 / 最底部」：`consoleTabs.ts` 用 PINNED_LAST_TAB_ID
 * 把「设置」钉死在最后一位，追加栏（QuantBot + arena 三栏）一律插在它**之前**——
 * 「紧挨设置下面」这条旧契约在任何正确布局上都恒为假，故改判如下。
 *
 * 断言三件，全按 DOM 量，不靠截图肉眼：
 *   1) 「设置」恒为最后一栏，且 QuantBot 是**追加栏第一条**（紧跟基础栏「个人中心」
 *      之后、在「设置」之前）；
 *   2) 点它，右边内容区出现 dsh 的 iframe（不是又落回左栏、也不是空白）；
 *   3) 切去别的栏再切回来，**iframe 没被卸载重建**（keepMounted 生效）。
 *      判据是给 iframe 打的 JS 标记还在——React 重建会换掉整个元素。
 *
 * 打 3080（quantmind-web 容器，部署产物）还是 3000（vite dev）都行：
 *   PROBE_BASE=http://localhost:3080 node electron/tests/probe_quantbot_tab.mjs
 * ⚠️ 深链一律带 `#/`（HashRouter），漏了会落到首页而不是目标页。
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
page.on('pageerror', (e) => errs.push('[PAGEERROR] ' + (e.message || '').slice(0, 200)));

const text = async (loc) => ((await loc.innerText().catch(() => '')) || '').replace(/\s/g, '');
const clickByText = async (re, scope = 'button') => {
  const items = page.locator(scope);
  for (let i = 0, n = await items.count(); i < n; i++) {
    const t = await text(items.nth(i));
    if (re.test(t)) { await items.nth(i).click(); return true; }
  }
  return false;
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
// 首访合规弹窗（同意/已知晓/跳过）
for (let round = 0; round < 4; round++) {
  const modal = page.locator('.ant-modal:visible');
  if ((await modal.count()) === 0) break;
  const done = await clickByText(/同意|确认|我知道了|已阅读|知道了|跳过/, '.ant-modal:visible button');
  if (!done) await page.keyboard.press('Escape').catch(() => {});
  await page.waitForTimeout(1000);
}

// ── 进实盘交易栏 ──────────────────────────────────────────────────────────
await page.goto(`${BASE}/#/live`, { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(6000);
for (let round = 0; round < 3; round++) {
  if (!(await clickByText(/同意|确认|我知道了|已阅读|知道了/, '.ant-modal:visible button'))) break;
  await page.waitForTimeout(1000);
}

const labels = await page
  .locator('div.w-\\[200px\\] button')
  .evaluateAll((els) => els.map((e) => (e.textContent || '').trim()));
const idxSettings = labels.indexOf('设置');
const idxQuantBot = labels.indexOf('QuantBot');
const idxPersonal = labels.indexOf('个人中心');
let failed = 0;
const check = (name, ok, detail = '') => {
  if (!ok) failed += 1;
  console.log(`${ok ? '  ✓' : '  ✗'} ${name}${detail ? ` —— ${detail}` : ''}`);
};
console.log('侧栏 =', JSON.stringify(labels));
check(
  '「设置」恒为最后一栏（PINNED_LAST_TAB_ID）',
  idxSettings >= 0 && idxSettings === labels.length - 1,
  `设置=#${idxSettings}，共 ${labels.length} 栏`,
);
check(
  'QuantBot 是追加栏第一条（个人中心之后、设置之前）',
  idxPersonal >= 0 && idxQuantBot === idxPersonal + 1 && idxQuantBot < idxSettings,
  `个人中心=#${idxPersonal} QuantBot=#${idxQuantBot} 设置=#${idxSettings}`,
);

// ── 点开：右边内容区应出现 dsh 的 iframe ──────────────────────────────────
await clickByText(/^QuantBot$/);
await page.waitForTimeout(5000);
const info = await page.evaluate(() => {
  const f = document.querySelector('iframe');
  if (!f) return null;
  const r = f.getBoundingClientRect();
  const sidebar = document.querySelector('div.w-\\[200px\\]');
  const sb = sidebar?.getBoundingClientRect();
  f.__probeMark = 'kept';
  return {
    src: f.getAttribute('src'),
    x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height),
    rightOfSidebar: sb ? r.x >= sb.right - 1 : null,
  };
});
console.log('iframe =', JSON.stringify(info));
check(
  '内容区出现 QuantBot（在侧栏右侧，尺寸正常）',
  Boolean(info && info.w > 600 && info.h > 300 && info.rightOfSidebar),
  info ? `w=${info.w} h=${info.h} rightOfSidebar=${info.rightOfSidebar}` : '没有 iframe',
);

// ── 切走再切回：iframe 不许被重建 ─────────────────────────────────────────
await clickByText(/^交易记录$/).catch(() => {});
await page.waitForTimeout(1500);
const aliveWhenAway = await page.evaluate(() => {
  const f = document.querySelector('iframe');
  if (!f) return 'gone';
  const hidden = !!f.closest('.hidden');
  return f.__probeMark === 'kept' ? (hidden ? 'kept-hidden' : 'kept-visible') : 'rebuilt';
});
await clickByText(/^QuantBot$/);
await page.waitForTimeout(1500);
const backMark = await page.evaluate(() => {
  const f = document.querySelector('iframe');
  return f ? (f.__probeMark === 'kept' ? 'same-element' : 'rebuilt') : 'gone';
});
check(
  '切走只隐藏、切回来还是同一个 iframe（dsh 连接没断）',
  aliveWhenAway === 'kept-hidden' && backMark === 'same-element',
  `切走时=${aliveWhenAway} 切回后=${backMark}`,
);

if (process.env.PROBE_SHOT) {
  await page.screenshot({ path: process.env.PROBE_SHOT, fullPage: false });
  console.log('截图 =', process.env.PROBE_SHOT);
}
check('无 pageerror', errs.length === 0, errs.slice(0, 2).join(' | '));
console.log(failed === 0 ? '\n全部通过' : `\n${failed} 项未通过`);
if (failed > 0) process.exitCode = 1;
await browser.close();
