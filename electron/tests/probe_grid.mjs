import { chromium } from 'playwright';
const BASE = 'http://localhost:3080';
const TAB = process.argv[2] || '大盘脉搏';
const b = await chromium.launch({ executablePath: '/usr/bin/google-chrome-stable', args: ['--no-sandbox'] });
const ctx = await b.newContext({ viewport: { width: 1720, height: 1150 } });
const p = await ctx.newPage();
console.log('加载的 main bundle:', await (async()=>{ await p.goto(BASE+'/', {waitUntil:'domcontentloaded'}); return p.evaluate(()=>[...document.querySelectorAll('script[src]')].map(s=>s.src.split('/').pop()).find(x=>x.startsWith('main-')));})());
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
await p.locator('button', { hasText: TAB }).first().click({ timeout: 6000 }).catch(() => undefined);
await p.waitForTimeout(8000);
console.log('== ' + TAB + ' ==');
const r = await p.evaluate(() => {
  const root = document.querySelector('.overflow-y-auto') || document.body;
  const out = [];
  // 只看「行容器」：直接子元素 ≥2 且宽度接近整页的 grid
  root.querySelectorAll('div.grid').forEach((d) => {
    const rect = d.getBoundingClientRect();
    if (rect.width < 1000 || !d.className.includes('shrink-0')) return;
    const kids = [...d.children].map((k) => {
      const kr = k.getBoundingClientRect();
      return { h: Math.round(kr.height), cls: (k.className || '').slice(0, 34) };
    });
    if (kids.length < 2) return;
    out.push({ h: Math.round(rect.height), kids, cls: d.className.slice(0, 60) });
  });
  return out.slice(0, 6);
});
r.forEach((x) => {
  const hs = x.kids.map((k) => k.h);
  const spread = Math.max(...hs) - Math.min(...hs);
  console.log(`  行容器 h=${x.h}  子项高度=[${hs.join(', ')}]  ${spread <= 2 ? '✓ 等高' : '✗ 差 ' + spread + 'px'}`);
  console.log(`     ${x.cls}`);
});
await b.close();
