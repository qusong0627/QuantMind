/**
 * 个股终端三市场回归 E2E：A 股 / 港股 / 美股。
 *
 * 用法：QM_BASE=http://localhost:3080 node tests/stock-terminal.e2e.mjs
 *
 * 走真实用户路径：登录 → 切市场 → 点「个股终端」→ 搜索标的 → 断言 K 线与详情面板。
 * 三市场共用 stock-terminal-shared，本脚本是共享层重构的回归网。
 */
import { chromium } from 'playwright';

const BASE = process.env.QM_BASE || 'http://localhost:3080';

/** 每个市场的断言：标题、搜索关键词、命中的建议、右侧 Tab 标签 */
const MARKETS = [
  {
    key: 'CN',
    radio: 'A股',
    title: '个股终端',
    query: '600519',
    tabs: ['概况', '财务', '估值', '筹码', '融资', '形态', '股东', '资讯', 'L2'],
    klineHint: '茅台',
  },
  {
    key: 'HK',
    radio: '港股',
    title: '港股个股终端',
    query: '00700',
    tabs: ['CCASS', '南向', '估值', '分红', '财务', '分析师', '资讯'],
    klineHint: '腾讯',
  },
  {
    key: 'US',
    radio: '美股',
    title: '美股个股终端',
    query: 'AAPL',
    tabs: ['概览', '估值', '财务', '分析师', '财报', '内部人', '机构', '分红拆股', '资讯'],
    klineHint: '苹果',
  },
];

const apiCalls = [];
const errors = [];
const flat = (s) => s.replace(/\s+/g, ' ');

// QM_MARKETS=CN,HK 可只跑部分市场（美股后端未就绪时先验收共享层回归）
const only = (process.env.QM_MARKETS || '').split(',').map((s) => s.trim().toUpperCase()).filter(Boolean);
const MARKETS_TO_RUN = only.length ? MARKETS.filter((m) => only.includes(m.key)) : MARKETS;

const b = await chromium.launch({ executablePath: '/usr/bin/google-chrome-stable', args: ['--no-sandbox'] });
const ctx = await b.newContext({ viewport: { width: 1720, height: 1150 } });
const p = await ctx.newPage();
p.on('console', (m) => { if (m.type() === 'error') errors.push('[console] ' + m.text().slice(0, 200)); });
p.on('pageerror', (e) => errors.push('[PAGEERROR] ' + (e.message || '').slice(0, 200)));
p.on('response', (r) => {
  const u = r.url();
  if (/stock-terminal|\/market\/(kline|quotes)/.test(u)) {
    apiCalls.push(r.status() + ' ' + u.replace(BASE, '').slice(0, 110));
    if (r.status() >= 400) errors.push('[API ' + r.status() + '] ' + u.slice(0, 160));
  }
});

// ---- 登录 ----
await p.goto(`${BASE}/`, { waitUntil: 'domcontentloaded', timeout: 40000 });
await p.waitForTimeout(3500);
if ((await p.locator('input[type=password]').count()) > 0) {
  await p.locator('input').nth(0).fill('admin');
  await p.locator('input[type=password]').first().fill('admin123');
  const btns = p.locator('button');
  for (let i = 0; i < (await btns.count()); i++) {
    const t = (await btns.nth(i).innerText().catch(() => '')).replace(/\s/g, '');
    if (/登录/.test(t)) { await btns.nth(i).click(); break; }
  }
  await p.waitForTimeout(7000);
}
console.log('登录后 URL:', p.url());

let pass = 0;
let total = 0;
const check = (ok, label) => { total++; if (ok) pass++; console.log(`${ok ? '✓' : '✗'} ${label}`); };

for (const mk of MARKETS_TO_RUN) {
  console.log(`\n========== ${mk.key} 个股终端 ==========`);
  // 市场切换器在首页/看板头部，个股终端页内部没有再渲染它 —— 必须先切市场再进页面
  await p.goto(`${BASE}/`, { waitUntil: 'domcontentloaded' });
  const radio = p.locator('button[role=radio]', { hasText: mk.radio }).first();
  // 重载后要等鉴权恢复 + 市场按钮渲染出来（固定 sleep 会偶发扑空）
  await radio.waitFor({ state: 'visible', timeout: 25000 }).catch(() => {});
  if (await radio.count()) {
    await radio.click();
    await p.waitForTimeout(2500);
  } else {
    console.log(`⚠ 未找到「${mk.radio}」市场切换按钮`);
  }
  const marketNow = await p.evaluate(() => localStorage.getItem('qm:current_market'));
  check(marketNow === mk.key, `市场已切到 ${mk.key}（localStorage=${marketNow}）`);

  // 进入个股终端（导航项与三市场共用同一条）
  const nav = p.locator('a,button', { hasText: '个股终端' }).first();
  await nav.waitFor({ state: 'visible', timeout: 20000 }).catch(() => {});
  await p.waitForTimeout(1200);
  const navOk = await nav.click({ timeout: 8000 }).then(() => true).catch(() => false);
  if (!navOk) await p.goto(`${BASE}/#/stock-terminal`, { waitUntil: 'domcontentloaded' });
  // 等页面真正挂载：顶部搜索框出现才算进到终端页
  const input = p.locator('input[placeholder*="搜索"]').first();
  await input.waitFor({ state: 'visible', timeout: 20000 }).catch(() => {});

  // 标题断言读 h1 —— 用 body 文本会被导航栏的「个股终端」误判为通过
  const h1 = (await p.locator('h1').first().innerText().catch(() => '')).replace(/\s+/g, ' ').trim();
  check(h1.includes(mk.title), `页面标题「${mk.title}」（实测 h1="${h1}"）`);
  let f = flat(await p.locator('body').innerText().catch(() => ''));

  if (!(await input.count())) { console.log('✗ 未进入终端页（无搜索框），跳过本市场'); continue; }

  // 搜索并选中：点建议项里带该标的中文名的那一条（不要盲点第一个按钮）
  await input.click();
  await input.fill(mk.query);
  await p.waitForTimeout(3000);
  const typed = await input.inputValue().catch(() => '');
  const sug = p.locator('button', { hasText: mk.klineHint }).first();
  const sugCount = await sug.count();
  console.log(`  · 输入值="${typed}" 建议项数=${sugCount}`);
  const picked = await sug.click({ timeout: 6000 }).then(() => true).catch(() => false);
  check(picked, `搜索「${mk.query}」后选中建议项「${mk.klineHint}」`);
  await p.waitForTimeout(9000);

  f = flat(await p.locator('body').innerText().catch(() => ''));
  check(f.includes(mk.klineHint), `选中标的名出现（${mk.klineHint}）`);
  const canvases = await p.locator('canvas').count();
  check(canvases > 0, `K线 canvas 已渲染（${canvases} 个）`);
  check(!f.includes('暂无K线') && !f.includes('K线加载中'), 'K线非空态');

  // 详情 Tab
  let tabHit = 0;
  for (const t of mk.tabs) {
    if (f.includes(t)) tabHit++;
  }
  check(tabHit === mk.tabs.length, `详情 Tab 齐备 ${tabHit}/${mk.tabs.length} [${mk.tabs.join(' / ')}]`);

  // 逐个点开前 3 个 Tab，确认不白屏
  for (const t of mk.tabs.slice(0, 3)) {
    const btn = p.locator('button', { hasText: new RegExp(`^${t}$`) }).first();
    if (!(await btn.count())) { console.log(`  ⚠ 找不到 Tab 按钮 ${t}`); continue; }
    await btn.click().catch(() => {});
    await p.waitForTimeout(2500);
    const body = flat(await p.locator('body').innerText().catch(() => ''));
    check(body.length > 200, `Tab「${t}」渲染非空`);
  }
}

console.log('\n---- 终端接口调用 ----');
console.log([...new Set(apiCalls)].slice(0, 40).join('\n') || '(无)');
console.log('\n---- 错误 ----');
console.log(errors.length ? [...new Set(errors)].slice(0, 12).join('\n') : '(无)');
console.log(`\n断言通过 ${pass}/${total}`);

await b.close();
process.exit(errors.length ? 1 : 0);
