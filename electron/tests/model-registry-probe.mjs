/** 模型库页探测：切到美股，数一数前端实际渲染出几个模型 */
import { chromium } from 'playwright';

const BASE = process.env.QM_BASE || 'http://localhost:3080';
const b = await chromium.launch({ executablePath: '/usr/bin/google-chrome-stable', args: ['--no-sandbox'] });
const ctx = await b.newContext({ viewport: { width: 1720, height: 1150 } });
const p = await ctx.newPage();
const apis = [];
p.on('response', (r) => {
  const u = r.url();
  if (/\/api\/v1\/models(\?|$)/.test(u)) apis.push(`${r.status()} ${u.split('/api/v1')[1]}`);
});

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
// 切到美股
const usBtn = p.locator('button[role=radio]', { hasText: '美股' }).first();
if (await usBtn.count()) { await usBtn.click(); await p.waitForTimeout(1500); }

// 进模型库
const nav = p.locator('a,button', { hasText: '模型库' }).first();
const ok = await nav.click({ timeout: 8000 }).then(() => true).catch(() => false);
if (!ok) {
  await p.goto(`${BASE}/#/model-registry`, { waitUntil: 'domcontentloaded' });
}
await p.waitForTimeout(9000);

const txt = await p.locator('body').innerText().catch(() => '');
const flat = txt.replace(/\s+/g, ' ');
console.log('URL:', p.url());
console.log('页面标题含「模型」:', flat.includes('模型'));
// 数模型条目：model_id 前缀出现次数
const ids = txt.match(/mdl_us_[A-Za-z0-9_]+/g) || [];
console.log('页面出现的 US model_id 条数:', new Set(ids).size);
console.log('唯一 model_id 样例:', [...new Set(ids)].slice(0, 3));
console.log('页面含「美股」标识:', flat.includes('美股'));
console.log('\n模型列表接口调用:');
console.log([...new Set(apis)].join('\n'));
await b.close();
