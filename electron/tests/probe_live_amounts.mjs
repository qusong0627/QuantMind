/**
 * 实况页「数字对不上」诊断探针（2026-10-08，一次性排查用）。
 *
 * 用户报障：实况图表各模型线合计 +2 万多，但「顶部总表」只显示 1287。
 * 本探针打开实况页，逐个点击右栏 tab（持仓/实盘/详情/已完成/成交），
 * dump 每块渲染出的数字文本 + 截图，用于把用户说的数字对到具体 UI 元素。
 *
 * 用法：PROBE_BASE=http://localhost:3080 node tests/probe_live_amounts.mjs
 */
import { chromium } from 'playwright';

const BASE = process.env.PROBE_BASE || 'http://localhost:3080';

const browser = await chromium.launch({
  executablePath: '/usr/bin/google-chrome-stable',
  args: ['--no-sandbox'],
});
const page = await browser.newPage({ viewport: { width: 1920, height: 1080 } });

async function login() {
  await page.goto(`${BASE}/#/auth/login`, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(4000);
  const inputs = page.locator('input:visible');
  if ((await inputs.count()) >= 2) {
    await inputs.nth(0).fill('admin');
    await inputs.nth(1).fill('admin123');
    const btns = page.locator('button');
    for (let i = 0, n = await btns.count(); i < n; i++) {
      const t = ((await btns.nth(i).innerText().catch(() => '')) || '').replace(/\s/g, '');
      if (/登录/.test(t)) { await btns.nth(i).click(); break; }
    }
    await page.waitForTimeout(6000);
  }
}

async function dismissModals() {
  for (let round = 0; round < 8; round++) {
    const blocking = await page.evaluate(
      () => Array.from(document.querySelectorAll('.ant-modal-wrap'))
        .filter((m) => getComputedStyle(m).display !== 'none').length,
    );
    if (!blocking) return true;
    let clicked = false;
    const btns = page.locator('.ant-modal:visible button');
    for (let i = 0, n = await btns.count(); i < n && !clicked; i++) {
      const t = ((await btns.nth(i).innerText().catch(() => '')) || '').replace(/\s/g, '');
      if (/稍后再答|同意|确认|我已知晓|已阅读|知道了|跳过/.test(t)) {
        await btns.nth(i).click().catch(() => {});
        clicked = true;
      }
    }
    if (!clicked) await page.keyboard.press('Escape').catch(() => {});
    await page.waitForTimeout(700);
  }
  return false;
}

await login();
await dismissModals();
await page.goto(`${BASE}/#/live`, { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(8000);
await dismissModals();
if (!(await page.locator('.eq-fill-mark').first().isVisible().catch(() => false))) {
  const btn = page.locator('button').filter({ hasText: /^实况$/ }).first();
  await btn.click({ timeout: 8000 }).catch(async () => {
    await dismissModals();
    await btn.click({ force: true });
  });
  await page.waitForTimeout(9000);
  await dismissModals();
}
await page.waitForTimeout(3000);

// 逐个 tab 点击 → dump 右栏文本 + 全页数字扫描 + 截图
const TABS = ['持仓', '实盘', '详情', '已完成'];
for (const tab of TABS) {
  const btn = page.locator('.trade-tabs button').filter({ hasText: new RegExp(`^${tab}`) }).first();
  if (!(await btn.count())) { console.log(`\n===== [${tab}] 按钮不存在`); continue; }
  await btn.click().catch(() => {});
  await page.waitForTimeout(4500);
  const dump = await page.evaluate(() => {
    const right = document.querySelector('.right-section');
    const txt = right ? (right.innerText || '').replace(/\n{2,}/g, '\n').slice(0, 2600) : '(无 .right-section)';
    // 全页含 1287 的元素
    const hits = [];
    for (const el of document.querySelectorAll('body *')) {
      const t = el.textContent || '';
      if (!/1287|1,287/.test(t)) continue;
      const childHas = Array.from(el.children).some((c) => /1287|1,287/.test(c.textContent || ''));
      if (childHas) continue;
      hits.push({ cls: String(el.className || '').slice(0, 60), text: t.trim().slice(0, 150) });
    }
    // 顶部资产概览的关键卡
    const top = document.body.innerText.match(/总资产[\s\S]{0,400}/);
    return { right: txt, hits, top: top ? top[0].slice(0, 400) : null };
  });
  console.log(`\n===== [${tab}] 右栏文本 =====`);
  console.log(dump.right);
  if (dump.hits.length) console.log('!!! 1287 命中:', JSON.stringify(dump.hits));
  await page.screenshot({ path: `/tmp/live_tab_${tab}.png`, fullPage: false });
}

await browser.close();
console.log('\n截图: /tmp/live_tab_*.png');
