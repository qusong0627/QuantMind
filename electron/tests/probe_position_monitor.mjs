// 持仓监控 1:1 布局回归探针（2026-10-09 用户「一半一半」改版护栏）。
// 跑法（从仓库根，node 要能解析到 electron 的 playwright）：
//   node electron/tests/probe_position_monitor.mjs
// 必须走实盘栏 #/live：只有它的持仓监控 tab 会经 railPanels 注入「风控止损」块，
// 模拟台（/#/trading）右栏没有 RISK 卡，在这里验会出现假阴性。
// 断言：1) ≥2xl 左板/右栏 1:1（±12px）；2) 右栏三卡齐（哨兵/风控止损/副驾驶）；
//  3) 风控指标 4 列一行；4) 左板明细价格/盈亏列无截断；5) 无 pageerror；并截图。
import { chromium } from 'playwright';

const BASE = process.env.PROBE_BASE || 'http://localhost:3080';
const browser = await chromium.launch({
  executablePath: '/usr/bin/google-chrome-stable',
  args: ['--no-sandbox'],
});
const page = await browser.newPage({ viewport: { width: 1920, height: 1080 } });
await page.addInitScript(() => window.localStorage.setItem('qm:ui_mode_pref', 'professional'));
const pageErrors = [];
page.on('pageerror', (e) => pageErrors.push((e.message || '').slice(0, 200)));

const results = [];
const check = (name, pass, detail = '') => {
  results.push(pass);
  console.log(`${pass ? 'PASS' : 'FAIL'}  ${name}${detail ? '  — ' + detail : ''}`);
};
const text = async (loc) => ((await loc.innerText().catch(() => '')) || '').replace(/\s/g, '');

const dismissModals = async () => {
  for (let round = 0; round < 8; round++) {
    const blocking = await page.evaluate(
      () => Array.from(document.querySelectorAll('.ant-modal-wrap'))
        .filter((m) => getComputedStyle(m).display !== 'none').length,
    );
    if (!blocking) return true;
    const btns = page.locator('.ant-modal:visible button');
    let clicked = false;
    for (let i = 0, n = await btns.count(); i < n; i++) {
      if (/稍后再答|同意|确认|我知道了|已阅读|知道了|跳过/.test(await text(btns.nth(i)))) {
        await btns.nth(i).click({ timeout: 3000 }).catch(() => {});
        clicked = true;
        break;
      }
    }
    if (!clicked) await page.keyboard.press('Escape').catch(() => {});
    await page.waitForTimeout(700);
  }
  return false;
};

await page.goto(`${BASE}/#/auth/login`, { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(3500);
const inputs = page.locator('input');
if ((await inputs.count()) >= 2) {
  await inputs.nth(0).fill('admin');
  await inputs.nth(1).fill('admin123');
  const btns = page.locator('button');
  for (let i = 0, n = await btns.count(); i < n; i++) {
    if (/登录/.test(await text(btns.nth(i)))) { await btns.nth(i).click(); break; }
  }
  await page.waitForTimeout(5000);
}

await page.goto(`${BASE}/#/live`, { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(9000);
await dismissModals();

// 点开「持仓监控」栏
const sideBtns = page.locator('button');
let opened = false;
for (let i = 0, n = await sideBtns.count(); i < n; i++) {
  if ((await text(sideBtns.nth(i))) === '持仓监控') {
    await sideBtns.nth(i).click({ timeout: 4000 }).catch(() => {});
    const cls = await sideBtns.nth(i).getAttribute('class').catch(() => '');
    opened = /bg-blue-50/.test(cls || '');
    break;
  }
}
await page.waitForTimeout(8000);
await dismissModals();
check('「持仓监控」栏点开后激活', opened);

const measure = () =>
  page.evaluate(() => {
    const rail = document.querySelector('[data-testid="position-rail"]');
    if (!rail) return { error: 'no rail' };
    const main = rail.parentElement;
    const left = main.children[0];
    const board = left.querySelector(':scope > div'); // 主卡（PositionVisualBoard 根）
    const railKids = Array.from(rail.children)
      .filter((el) => el.getBoundingClientRect().width > 0)
      .map((el) => ({
        w: Math.round(el.getBoundingClientRect().width),
        t: (el.textContent || '').replace(/\s+/g, '').slice(0, 16),
      }));
    // 左板明细：价格/盈亏数值单元格被截断才是回归（名称类 truncate 是设计）
    const cut = [];
    left.querySelectorAll('.truncate').forEach((el) => {
      const t = (el.textContent || '').trim();
      if (!/^[¥+\-−]?[\d,]+(\.\d+)?%?$/.test(t)) return; // 只看纯数值
      if (el.scrollWidth > el.clientWidth + 1) {
        cut.push(`${t.slice(0, 14)}(${el.scrollWidth}>${el.clientWidth})`);
      }
    });
    // 风控指标网格列数
    const riskGrid = Array.from(rail.querySelectorAll('div.grid')).find((g) =>
      /止损线|大跌拦截/.test(g.textContent || ''));
    return {
      mainW: Math.round(main.getBoundingClientRect().width),
      leftW: Math.round(left.getBoundingClientRect().width),
      railW: Math.round(rail.getBoundingClientRect().width),
      boardW: board ? Math.round(board.getBoundingClientRect().width) : null,
      railKids,
      riskCols: riskGrid ? getComputedStyle(riskGrid).gridTemplateColumns.split(' ').length : null,
      hasRiskTitle: /风控与风险锁/.test(rail.textContent || ''),
      cut: cut.slice(0, 8),
      cutTotal: cut.length,
    };
  });

for (const w of [1920, 1680, 1536]) {
  await page.setViewportSize({ width: w, height: 1080 });
  await page.waitForTimeout(2200);
  const m = await measure();
  console.log(`[${w}]`, JSON.stringify(m));
  if (!m.error) {
    check(`@${w} 左板/右栏 1:1`, Math.abs(m.leftW - m.railW) <= 12, `left=${m.leftW} rail=${m.railW}`);
    check(`@${w} 右栏三卡齐（哨兵/风控/副驾驶）`, m.railKids.length >= 3, m.railKids.map((k) => k.t).join(' | '));
    check(`@${w} 「风控与风险锁」在栏内`, m.hasRiskTitle);
  }
  await page.screenshot({ path: `/tmp/pm_after_${w}.png` });
  await page.evaluate(() => document.querySelector('[data-testid="position-rail"]')?.scrollTo(0, 0));
}

// 1920 下风控 4 列 + 截断总检
await page.setViewportSize({ width: 1920, height: 1080 });
await page.waitForTimeout(2000);
const m1920 = await measure();
check('1920 风控指标 4 列', m1920.riskCols === 4, `cols=${m1920.riskCols}`);
check('左板明细无截断', m1920.cutTotal === 0, m1920.cutTotal ? m1920.cut.join(' · ') : '');
check('无未捕获 pageerror', pageErrors.length === 0, pageErrors.slice(0, 2).join(' | '));

await browser.close();
const failed = results.filter((r) => !r).length;
console.log(`\n${results.length - failed}/${results.length} 通过`);
process.exit(failed ? 1 : 0);
