/** 市场分析页几何探测：逐卡片测量位置与尺寸，定位「长短不一/不对齐」 */
import { chromium } from 'playwright';

const BASE = process.env.QM_BASE || 'http://localhost:3080';
const TAB = process.argv[2] || '大盘脉搏';

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
await p.locator('button', { hasText: TAB }).first().click({ timeout: 6000 }).catch(() => undefined);
await p.waitForTimeout(8000);

const info = await p.evaluate(() => {
  const root = document.querySelector('.overflow-y-auto') || document.body;
  const out = [];
  // 卡片判定：圆角 + 白底 + 有标题 h3 的容器
  root.querySelectorAll('div').forEach((d) => {
    const cs = getComputedStyle(d);
    if (!cs.borderRadius.includes('16') && parseFloat(cs.borderRadius) < 12) return;
    const r = d.getBoundingClientRect();
    if (r.width < 240 || r.height < 60) return;
    const h3 = d.querySelector('h3');
    if (!h3) return;
    out.push({
      title: (h3.textContent || '').trim().slice(0, 18),
      x: Math.round(r.x), y: Math.round(r.y),
      w: Math.round(r.width), h: Math.round(r.height),
      bottom: Math.round(r.bottom),
    });
  });
  // 去重（嵌套卡片）并按 y 排序
  const seen = new Set();
  return out.filter((c) => {
    const k = `${c.title}|${c.x}|${c.y}`;
    if (seen.has(k)) return false;
    seen.add(k);
    return true;
  }).sort((a, b2) => a.y - b2.y || a.x - b2.x);
});

console.log(`== ${TAB} ==`);
info.forEach((c) => {
  console.log(`    ${c.title.padEnd(20)} x=${String(c.x).padStart(4)} w=${String(c.w).padStart(4)} h=${String(c.h).padStart(4)} bottom=${c.bottom}`);
});

// 按「列」判定对齐：同一行区域里，各列最后一张卡的底边应一致。
// 不能按「同 y 的卡片」比较 —— 一列可能叠了多张卡，那样必然误报。
const rows = {};
info.forEach((c) => { const k = Math.round(c.y / 60) * 60; (rows[k] = rows[k] || []).push(c); });
Object.entries(rows).forEach(([y, cs]) => {
  const byCol = {};
  cs.forEach((c) => { const col = c.x < 400 ? 'L' : (c.x > 1600 ? 'F' : 'R'); (byCol[col] = byCol[col] || []).push(c); });
  const colBottoms = Object.entries(byCol).map(([k, v]) => [k, Math.max(...v.map((c) => c.bottom))]);
  if (colBottoms.length > 1) {
    const bs = colBottoms.map(([, b]) => b);
    const spread = Math.max(...bs) - Math.min(...bs);
    const detail = colBottoms.map(([k, b]) => `${k}=${b}`).join(' ');
    console.log(`  >>> 行区 y≈${y}: ${detail} → 底边差 ${spread}px ${spread > 6 ? '✗ 未对齐' : '✓ 对齐'}`);
  } else if (colBottoms.length === 1) {
    console.log(`  >>> 行区 y≈${y}: 单列，底边 ${colBottoms[0][1]}`);
  }
});
await b.close();
