/** 布局诊断：实测美股市场分析页各板块的像素尺寸、行数、留白，用于量化「拥挤度」 */
import { chromium } from 'playwright';

const BASE = process.env.QM_BASE || 'http://localhost:3080';
const TABS = ['大盘脉搏', '市场宽度', '板块轮动', '财报季', '分析师动向', '资金与筹码', '估值主题'];

const b = await chromium.launch({ executablePath: '/usr/bin/google-chrome-stable', args: ['--no-sandbox'] });
const ctx = await b.newContext({ viewport: { width: 1720, height: 1150 } });
const p = await ctx.newPage();

await p.goto(`${BASE}/`, { waitUntil: 'domcontentloaded', timeout: 40000 });
await p.waitForTimeout(3000);
if ((await p.locator('input[type=password]').count()) > 0) {
  await p.locator('input').nth(0).fill('admin');
  await p.locator('input[type=password]').first().fill('admin123');
  const btns = p.locator('button');
  for (let i = 0; i < (await btns.count()); i++) {
    const t = (await btns.nth(i).innerText().catch(() => '')).replace(/\s/g, '');
    if (/登录|登\s*录/.test(t)) { await btns.nth(i).click(); break; }
  }
  await p.waitForTimeout(7000);
}
const usBtn = p.locator('button[role=radio]', { hasText: '美股' }).first();
if (await usBtn.count()) { await usBtn.click(); await p.waitForTimeout(1500); }
await p.locator('a,button', { hasText: '市场分析' }).first().click({ timeout: 8000 }).catch(() => undefined);
await p.waitForTimeout(9000);

const probed = await p.evaluate(() => {
  const out = {};
  const main = document.querySelector('.overflow-y-auto') || document.body;
  out.viewport = { w: window.innerWidth, h: window.innerHeight };
  out.scrollHeight = main.scrollHeight;
  // 卡片：找 bg-white/90 的容器
  const cards = Array.from(main.querySelectorAll('div')).filter((d) => {
    const c = getComputedStyle(d).backgroundColor;
    const r = d.getBoundingClientRect();
    return r.width > 240 && r.height > 60 && /rgba?\(2[45][0-9]|rgba?\(25[0-5]/.test(c) === false && d.className.includes('rounded-2xl');
  });
  out.cards = cards.slice(0, 14).map((d) => {
    const r = d.getBoundingClientRect();
    return {
      w: Math.round(r.width),
      h: Math.round(r.height),
      title: (d.querySelector('h3')?.textContent || '').trim().slice(0, 22),
    };
  });
  out.cardCount = cards.length;
  // 页面顶部到首屏可见内容的高度
  const h1 = main.querySelector('h1');
  out.headerBottom = h1 ? Math.round(h1.getBoundingClientRect().bottom) : null;
  return out;
});

console.log('视口:', JSON.stringify(probed.viewport), ' 页面总高:', probed.scrollHeight, ' (首屏 1150)');
console.log('卡片数:', probed.cardCount);
probed.cards.forEach((c) => console.log(`   卡片 ${String(c.w).padStart(4)}x${String(c.h).padStart(4)}  ${c.title}`));

// 每个 Tab 的行数统计
for (const tab of TABS) {
  const el = p.locator('button', { hasText: tab }).first();
  if (!(await el.click({ timeout: 5000 }).then(() => true).catch(() => false))) continue;
  await p.waitForTimeout(6000);
  const info = await p.evaluate(() => {
    const root = document.querySelector('.overflow-y-auto') || document.body;
    // RankRow 模式：flex 行且含 rounded-md 序号方块
    const rankRows = root.querySelectorAll('div.flex.items-center.gap-2\\.5').length;
    const tables = root.querySelectorAll('table').length;
    const tbodyRows = root.querySelectorAll('tbody tr').length;
    const charts = root.querySelectorAll('canvas').length;
    const txt = (root.innerText || '');
    // 估算有效信息密度：非空文本行数
    const lines = txt.split('\n').filter((l) => l.trim()).length;
    return {
      h: root.scrollHeight, rankRows, tables, tbodyRows, charts, lines,
      chars: txt.replace(/\s/g, '').length,
    };
  });
  console.log(
    `${tab.padEnd(6)} 页高=${String(info.h).padStart(5)}  排行行=${String(info.rankRows).padStart(3)}  表格=${info.tables}(行${info.tbodyRows})  图=${info.charts}  文本行=${String(info.lines).padStart(4)}  字符=${info.chars}`,
  );
}
await b.close();
