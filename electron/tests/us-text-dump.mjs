/** 文本转储：把指定 Tab 的实际渲染文本打出来，用于人工判读「信息够不够」 */
import { chromium } from 'playwright';

const BASE = process.env.QM_BASE || 'http://localhost:3080';
const TAB = process.argv[2] || '市场宽度';

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

const txt = await p.evaluate(() => {
  const root = document.querySelector('.overflow-y-auto') || document.body;
  return root.innerText;
});
console.log(txt.split('\n').filter((l) => l.trim()).join(' | '));
await b.close();
