/**
 * 「智能体台账」卡片行自适应探针（2026-10-09）。
 *
 * 背景（用户报障）：「3个智能体界面自适应、不然显示窄了，就是3行，很难看」。
 * 根因 = 台账栏复用实况页的卡片类（.model-cards-section / .model-card-mini…），
 * 而它们的基础排版规则住在页面级 Live.css（随实况页懒加载 chunk 才载入）——
 * 新会话**直接进台账**（没访问过盘中实况）时样式未到，三张卡各占一整行。
 * 修复 = 台账栏显式 import Live.css + 卡片行改 grid auto-fit（选择器带 div.
 * 抬特异性压过晚加载的页面样式，见 qm-arena-theme.css）。
 *
 * 关键场景：新开会话第一个打开的 arena 栏就是「智能体台账」。
 * 判据（几何不变量，与实现解耦）：
 *   1. 侧栏改名落地（智能体台账/盘中实况 在、旧名「智能体交易」不在、「设置」仍在末位）；
 *   2. 台账卡片 display:grid，1680/1440/1280/1024 四档视口三张卡同一行且等宽；
 *   3. 盘中实况 @1280 折行时卡片等宽（不再出现 flex 时代的通栏独苗）；
 *   4. 无未捕获 pageerror。
 *
 * 用法：PROBE_BASE=http://localhost:3080 node electron/tests/probe_agent_ledger_cards.mjs
 * 退出码：0 全过；1 有 FAIL。
 */
import { chromium } from 'playwright';

const BASE = process.env.PROBE_BASE || 'http://localhost:3080';
const browser = await chromium.launch({
  executablePath: '/usr/bin/google-chrome-stable',
  args: ['--no-sandbox'],
});
const page = await browser.newPage({ viewport: { width: 1680, height: 980 } });
await page.addInitScript(() => window.localStorage.setItem('qm:ui_mode_pref', 'professional'));
const pageErrors = [];
page.on('pageerror', (e) => pageErrors.push((e.message || '').slice(0, 200)));

const text = async (loc) => ((await loc.innerText().catch(() => '')) || '').replace(/\s/g, '');
const results = [];
const check = (name, pass, detail = '') => {
  results.push(pass);
  console.log(`${pass ? 'PASS' : 'FAIL'}  ${name}${detail ? '  — ' + detail : ''}`);
};

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

const openTab = async (label) => {
  const btns = page.locator('button');
  for (let i = 0, n = await btns.count(); i < n; i++) {
    if ((await text(btns.nth(i))) === label) {
      await btns.nth(i).click({ timeout: 4000 }).catch(() => {});
      await page.waitForTimeout(2500);
      const cls = await btns.nth(i).getAttribute('class').catch(() => '');
      return /bg-blue-50/.test(cls || '');
    }
  }
  return false;
};

const measureCards = () =>
  page.evaluate(() => {
    const root = document.querySelector('.qm-arena-root');
    if (!root) return { error: 'no .qm-arena-root' };
    const section = root.querySelector('.model-cards-section');
    if (!section) return { error: 'no .model-cards-section' };
    const cs = getComputedStyle(section);
    const cards = Array.from(section.querySelectorAll('.model-card-mini')).map((c) => {
      const r = c.getBoundingClientRect();
      return { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) };
    });
    // 行数按 y 聚类（容差 10px）：选中卡有 translateY(-2px)（.selected），
    // 逐像素去重会把同一行数成两行（假阳性）
    const ys = [...new Set(cards.map((c) => c.y))].sort((a, b) => a - b);
    let rows = 0;
    let last = -Infinity;
    for (const y of ys) {
      if (y - last > 10) rows += 1;
      last = y;
    }
    return {
      display: cs.display,
      sectionW: Math.round(section.getBoundingClientRect().width),
      rows,
      widths: cards.map((c) => c.w),
      cards,
    };
  });

// ── 登录 ──
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
await page.waitForTimeout(1000);

// ── 侧栏改名核验（口径同 probe_live_arena_tabs.mjs：从「设置」按钮往上找侧栏条）──
const side = await page.evaluate(() => {
  const b = Array.from(document.querySelectorAll('button'))
    .find((x) => (x.textContent || '').trim() === '设置');
  const strip = b?.parentElement?.parentElement;
  return strip ? Array.from(strip.querySelectorAll('button')).map((x) => (x.textContent || '').trim()) : [];
});
check('侧栏有「智能体台账」', side.includes('智能体台账'));
check('侧栏有「盘中实况」', side.includes('盘中实况'));
check('旧名「智能体交易」已消失（侧栏）', !side.includes('智能体交易'));
check('「设置」仍是最后一栏', side[side.length - 1] === '设置', JSON.stringify(side.slice(-3)));

// ── 关键场景：先开台账（没访问过盘中实况）──
const ok1 = await openTab('智能体台账');
await page.waitForTimeout(3500);
check('「智能体台账」点开后激活', ok1);
const title = await page.evaluate(() => {
  const h = document.querySelector('.qm-arena-root h1');
  return h ? h.textContent.trim() : null;
});
check('页内标题 = 智能体台账', title === '智能体台账', String(title));

for (const w of [1680, 1440, 1280, 1024]) {
  await page.setViewportSize({ width: w, height: 980 });
  await page.waitForTimeout(1500);
  const m = await measureCards();
  console.log(`  [台账 ${w}]`, JSON.stringify(m));
  check(`台账@${w}: 卡片 display:grid`, m.display === 'grid');
  check(`台账@${w}: 三张卡同一行（rows=1）`, m.rows === 1);
  check(`台账@${w}: 三张等宽`, new Set(m.widths).size === 1, JSON.stringify(m.widths));
  if (w === 1680 || w === 1280) await page.screenshot({ path: `/tmp/probe_ledger_cards_${w}.png` });
}

// ── 盘中实况：改名后卡片仍一行（1680）/ 折行形态（1280）──
await page.setViewportSize({ width: 1680, height: 980 });
await page.waitForTimeout(1200);
const ok2 = await openTab('盘中实况');
await page.waitForTimeout(4500);
check('「盘中实况」点开后激活', ok2);
await page.evaluate(() => document.querySelector('.qm-arena-root')?.scrollTo(0, 0));
let m = await measureCards();
console.log('  [实况 1680]', JSON.stringify(m));
check('实况@1680: 卡片 grid 一行', m.display === 'grid' && m.rows === 1, `rows=${m.rows}`);
await page.screenshot({ path: '/tmp/probe_ledger_live_1680.png' });

await page.setViewportSize({ width: 1280, height: 980 });
await page.waitForTimeout(2000);
m = await measureCards();
console.log('  [实况 1280]', JSON.stringify(m));
if (m.rows > 1) {
  const uniq = new Set(m.widths);
  check('实况@1280 折行时卡片等宽（不再出现通栏独苗）', uniq.size === 1, JSON.stringify(m.widths));
} else {
  check('实况@1280 仍一行', true, JSON.stringify(m.widths));
}
await page.screenshot({ path: '/tmp/probe_ledger_live_1280.png' });

// ── pageerror 巡检 ──
check('无未捕获 pageerror', pageErrors.length === 0, pageErrors.slice(0, 3).join(' | '));

await browser.close();
const failed = results.filter((r) => !r).length;
console.log(`\n${results.length - failed}/${results.length} 通过`);
process.exit(failed ? 1 : 0);
