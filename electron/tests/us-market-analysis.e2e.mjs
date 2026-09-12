/** 美股市场分析页 E2E：走真实用户路径（点「美股」→ 点「市场分析」→ 逐 Tab 断言 DOM） */
import { chromium } from 'playwright';

const BASE = process.env.QM_BASE || 'http://localhost:3080';
const EXPECT = {
  // 大盘脉搏已重排为「看哪儿热」优先：热门榜 + 活力条 + 资金流
  大盘脉搏: ['市场温度计', 'GICS 板块热力图', '今日热门', '量比', '板块资金流'],
  市场宽度: ['A-D', 'MA50', 'MA200', '涨跌幅分布'],
  板块轮动: ['轮动', '相对标普', '板块估值'],
  财报季: ['财报', '超预期', '盈利预期'],
  分析师动向: ['评级', '目标价'],
  资金与筹码: ['内部人', '机构', '除息'],
  估值主题: ['估值', '市值分层', '股息'],
};

const apiCalls = [];
const errors = [];
const b = await chromium.launch({ executablePath: '/usr/bin/google-chrome-stable', args: ['--no-sandbox'] });
const ctx = await b.newContext({ viewport: { width: 1720, height: 1150 } });
const p = await ctx.newPage();
p.on('console', (m) => { if (m.type() === 'error') errors.push('[console] ' + m.text().slice(0, 200)); });
p.on('pageerror', (e) => errors.push('[PAGEERROR] ' + (e.message || '').slice(0, 200)));
p.on('response', (r) => {
  const u = r.url();
  if (/market-analysis-us/.test(u)) {
    apiCalls.push(r.status() + ' ' + u.split('market-analysis-us')[1].slice(0, 55));
    if (r.status() >= 400) errors.push('[API ' + r.status() + '] ' + u.slice(0, 140));
  }
});

const flat = (s) => s.replace(/\s+/g, ' ');

// ---- 登录 ----
await p.goto(`${BASE}/`, { waitUntil: 'domcontentloaded', timeout: 40000 });
await p.waitForTimeout(3500);
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
console.log('登录后 URL:', p.url());

// ---- 点顶部市场切换器的「美股」 ----
const usBtn = p.locator('button[role=radio]', { hasText: '美股' }).first();
console.log('找到「美股」切换按钮:', (await usBtn.count()) > 0);
if (await usBtn.count()) { await usBtn.click(); await p.waitForTimeout(2000); }
console.log('切换后 localStorage 市场:', await p.evaluate(() => localStorage.getItem('qm:current_market')));

// ---- 点侧边栏「市场分析」 ----
const navItem = p.locator('a,button', { hasText: '市场分析' }).first();
const navOk = await navItem.click({ timeout: 8000 }).then(() => true).catch(() => false);
console.log('点击「市场分析」导航:', navOk);
if (!navOk) {
  await p.goto(`${BASE}/#/market-analysis`, { waitUntil: 'domcontentloaded' });
}
await p.waitForTimeout(10000);

let text = await p.locator('body').innerText().catch(() => '');
let f = flat(text);
console.log('\nURL:', p.url());
console.log('✓ 渲染美股页标题:', f.includes('美股市场多维分析'));
console.log('✓ QuantUS 引擎标识:', f.includes('QuantUS'));
console.log('✓ 标普500 指数卡:', f.includes('标普500'));
console.log('✓ 费城半导体 指数卡:', f.includes('费城半导体'));
console.log('✓ 未误渲染 A 股指数:', !f.includes('上证指数'));
console.log('✓ 标的池口径提示:', f.includes('标的池') && f.includes('非全市场'));
console.log('✓ 指数量比已渲染:', f.includes('量比'));

// ---- 逐 Tab ----
console.log('\n---- 各 Tab 内容断言 ----');
let pass = 0;
for (const [tab, keys] of Object.entries(EXPECT)) {
  const el = p.locator('button', { hasText: tab }).first();
  const clicked = await el.click({ timeout: 6000 }).then(() => true).catch(() => false);
  if (!clicked) { console.log(`✗ ${tab}: 按钮点击失败`); continue; }
  await p.waitForTimeout(7000);
  text = await p.locator('body').innerText().catch(() => '');
  f = flat(text);
  const hit = keys.filter((k) => f.includes(k));
  const loading = f.includes('加载中');
  const ok = hit.length === keys.length && !loading;
  if (ok) pass++;
  console.log(`${ok ? '✓' : '✗'} ${tab}: ${hit.length}/${keys.length} [${hit.join(' / ')}]${loading ? ' 仍在加载' : ''}`);
}
console.log(`\nTab 通过 ${pass}/${Object.keys(EXPECT).length}`);

console.log('\n---- market-analysis-us 接口 ----');
const uniq = [...new Set(apiCalls)];
console.log(uniq.length ? uniq.join('\n') : '(无调用)');
console.log('\n---- 错误 ----');
console.log(errors.length ? [...new Set(errors)].slice(0, 10).join('\n') : '(无)');
await b.close();
