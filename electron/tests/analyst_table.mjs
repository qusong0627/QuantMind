import { chromium } from 'playwright';
const BASE = 'http://localhost:3080';
const b = await chromium.launch({ executablePath: '/usr/bin/google-chrome-stable', args: ['--no-sandbox'] });
const ctx = await b.newContext({ viewport: { width: 1720, height: 1150 } });
const p = await ctx.newPage();
await p.goto(`${BASE}/`, { waitUntil: 'domcontentloaded', timeout: 40000 });
await p.waitForTimeout(3000);
if ((await p.locator('input[type=password]').count()) > 0) {
  await p.locator('input').nth(0).fill('admin');
  await p.locator('input[type=password]').first().fill('admin123');
  const btns = p.locator('button');
  for (let i=0;i<(await btns.count());i++){const t=(await btns.nth(i).innerText().catch(()=>'')).replace(/\s/g,'');if(/登录|登\s*录/.test(t)){await btns.nth(i).click();break;}}
  await p.waitForTimeout(7000);
}
const us = p.locator('button[role=radio]', { hasText: '美股' }).first();
if (await us.count()) { await us.click(); await p.waitForTimeout(1500); }
await p.locator('a,button', { hasText: '市场分析' }).first().click({timeout:8000}).catch(()=>{});
await p.waitForTimeout(9000);
await p.locator('button', { hasText: '分析师动向' }).first().click({timeout:6000}).catch(()=>{});
await p.waitForTimeout(9000);

const r = await p.evaluate(() => {
  // 找「评级升降级流水」所在卡片里的前 3 行，逐格量宽高
  const cards = [...document.querySelectorAll('div')].filter((d) => (d.querySelector('h3')?.textContent || '').includes('评级升降级流水'));
  if (!cards.length) return { err: '未找到卡片' };
  const card = cards[0];
  const rows = [...card.querySelectorAll('div.grid')];
  const head = rows[0];
  const out = { card: Math.round(card.getBoundingClientRect().width), head: {}, rows: [] };
  const dump = (row) => [...row.children].map((c) => {
    const rect = c.getBoundingClientRect();
    return { w: Math.round(rect.width), h: Math.round(rect.height), x: Math.round(rect.x), t: (c.innerText||'').replace(/\s+/g,' ').slice(0,26), sw: c.scrollWidth, cw: c.clientWidth };
  });
  out.head = dump(head);
  rows.slice(1, 4).forEach((row) => out.rows.push(dump(row)));
  // 行本身的高度与容器
  const scrollBox = card.querySelector('.overflow-y-auto');
  out.scrollBoxH = scrollBox ? Math.round(scrollBox.getBoundingClientRect().height) : null;
  out.rowCount = rows.length - 1;
  out.rowHeights = rows.slice(1, 6).map((x) => Math.round(x.getBoundingClientRect().height));
  return out;
});
console.log(JSON.stringify(r, null, 1).slice(0, 2200));
await b.close();
