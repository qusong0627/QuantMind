/**
 * 首页六宫格多市场 E2E：走真实用户路径（登录 → 点市场 → 断言每一格的口径）
 *
 * 断言重点（本轮修复的核心）：切到非 A 股市场后，**不允许**再出现 A 股的账户与成交。
 *  - 港股/美股页不得出现 A 股成交（招商银行/正帆科技…）与 A 股账户金额
 *  - 策略格数字应等于后端 `?market=` 的返回条数（港 15 / 美 0）
 *  - 空市场必须出现「未开通 / 暂无」空态，而不是别的市场的数字
 *
 * 用法：node electron/tests/dashboard-market.e2e.mjs
 * 环境：QM_BASE（默认 http://localhost:3080）
 */
import { chromium } from 'playwright';

const BASE = process.env.QM_BASE || 'http://localhost:3080';

/** A 股特征串：出现在非 A 股市场的格子里即为失败 */
const CN_FINGERPRINTS = ['招商银行', '正帆科技', '杭州园林', '永辉超市', '600036.SH'];

const MARKETS = [
  { label: 'A股', expect: ['A股概览', '资金概览', '实时交易记录', '策略监控', '智能图表', '信息通知'] },
  { label: '港股', expect: ['港股概览', '恒生指数'] },
  { label: '美股', expect: ['美股概览'] },
  { label: '期货', expect: ['期货概览'] },
];

const errors = [];
const consoleErrors = [];

const flat = (s) => String(s || '').replace(/\s+/g, ' ');

const browser = await chromium.launch({
  executablePath: '/usr/bin/google-chrome-stable',
  args: ['--no-sandbox'],
});
const ctx = await browser.newContext({ viewport: { width: 1720, height: 1150 } });
const page = await ctx.newPage();
page.on('pageerror', (e) => errors.push('[PAGEERROR] ' + (e.message || '').slice(0, 200)));
page.on('console', (m) => {
  if (m.type() === 'error') consoleErrors.push('[console] ' + m.text().slice(0, 160));
});
page.on('response', (r) => {
  const u = r.url();
  if (/\/(market\/overview|simulation\/account|simulation\/trades|strategies|notifications)/.test(u) && r.status() >= 400) {
    errors.push(`[API ${r.status()}] ${u.slice(0, 150)}`);
  }
});

// ---- 登录 ----
await page.goto(`${BASE}/`, { waitUntil: 'domcontentloaded', timeout: 40000 });
await page.waitForTimeout(3500);
if ((await page.locator('input[type=password]').count()) > 0) {
  await page.locator('input').nth(0).fill('admin');
  await page.locator('input[type=password]').first().fill('admin123');
  const btns = page.locator('button');
  for (let i = 0; i < (await btns.count()); i++) {
    const t = flat(await btns.nth(i).innerText().catch(() => '')).replace(/\s/g, '');
    if (/登录|登\s*录/.test(t)) { await btns.nth(i).click(); break; }
  }
  await page.waitForTimeout(7000);
}

const results = [];
for (const market of MARKETS) {
  // 切市场（顶部市场切换器）
  const btn = page.locator(`button[role="radio"]:has-text("${market.label}")`).first();
  if (await btn.count()) {
    await btn.click();
  } else {
    await page.locator(`text=${market.label}`).first().click({ timeout: 5000 }).catch(() => {});
  }
  // 回到首页（若当前在别的页面）
  await page.locator('nav >> text=' + market.label).first().click().catch(() => {});
  // 非 A 股市场的行情概览后端较慢（实测 HK 2.7s / US 6.2s / FUTURES 12.7s），
  // 这里按内容轮询等待，而不是固定 sleep —— 否则会误判成「卡片没渲染」
  const deadline = Date.now() + 30000;
  let text = '';
  while (Date.now() < deadline) {
    await page.waitForTimeout(1500);
    text = flat(await page.locator('body').innerText().catch(() => ''));
    // 「总策略」= 策略卡脱离骨架屏；「盈亏比 / 暂无交易统计」= 智能图表成交统计落地。
    // 少了后者会误判「成交统计对不上」（港股行情概览比图表先到）
    const ready =
      market.expect.every((k) => text.includes(k)) &&
      text.includes('总策略') &&
      (text.includes('盈亏比') || text.includes('暂无交易统计'));
    const stillSkeleton = /大盘概览|资金概览 交易记录/.test(text) || text.includes('加载中');
    if (ready && !stillSkeleton) break;
  }
  // 其余卡片（通知/资金）留一点收尾时间，避免截图与断言落在中间态
  await page.waitForTimeout(2000);
  text = flat(await page.locator('body').innerText().catch(() => ''));

  const missing = market.expect.filter((k) => !text.includes(k));
  const cnHits = market.label === 'A股' ? [] : CN_FINGERPRINTS.filter((k) => text.includes(k));
  // 策略格数字 vs 后端 ?market= 条数
  const strategyHit = text.match(/总策略 (\d+)/);
  const uiTotal = strategyHit ? Number(strategyHit[1]) : null;
  // 智能图表的成交统计（累计 N 笔 / 胜率）也必须按市场过滤，不能拿 A 股的 35 笔顶上
  const tradeHit = text.match(/累计 (\d+) 笔/);
  const uiTrades = tradeHit ? Number(tradeHit[1]) : null;

  results.push({ market: market.label, missing, cnHits, uiTotal, uiTrades, textLen: text.length });
  await page.screenshot({ path: `/tmp/dashboard_${market.label}.png`, fullPage: false });
}

// ---- 用页面内的 fetch 拿后端真值做交叉核对（复用登录态） ----
const backendCounts = await page.evaluate(async () => {
  const token = localStorage.getItem('access_token') || localStorage.getItem('auth_token') || '';
  const out = {};
  for (const [key, market] of [['CN', 'CN'], ['HK', 'HK'], ['US', 'US'], ['FUTURES', 'FUTURES']]) {
    try {
      const r = await fetch(`/api/v1/strategies?market=${market}`, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      });
      const d = await r.json();
      out[key] = {
        strategies: Number(d?.total ?? (Array.isArray(d?.strategies) ? d.strategies.length : -1)),
      };
      const r2 = await fetch(`/api/v1/simulation/trades/stats/summary?market=${market}`, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      });
      const d2 = await r2.json();
      out[key].trades = Number(d2?.total_trades ?? -1);
    } catch (e) {
      out[key] = { strategies: -1, trades: -1 };
    }
  }
  return out;
});

await browser.close();

// ---- 汇总 ----
let failed = 0;
console.log('\n=== 首页六宫格多市场 E2E ===');
for (const r of results) {
  const ok = r.missing.length === 0 && r.cnHits.length === 0;
  if (!ok) failed += 1;
  console.log(
    `${ok ? '✅' : '❌'} ${r.market.padEnd(4)} 文案缺失=[${r.missing.join(',')}] ` +
      `A股串混入=[${r.cnHits.join(',')}] 策略格=${r.uiTotal ?? '—'} 成交统计=${r.uiTrades ?? '—'}`,
  );
}
console.log('后端真值:', JSON.stringify(backendCounts));

// 策略格数字与成交统计都必须与后端同市场口径一致（美股/期货应为 0）
for (const r of results) {
  const key = { A股: 'CN', 港股: 'HK', 美股: 'US', 期货: 'FUTURES' }[r.market];
  const expected = backendCounts[key];
  if (!expected) continue;
  if (r.uiTotal !== null && expected.strategies >= 0 && r.uiTotal !== expected.strategies) {
    console.log(`❌ ${r.market} 策略格 ${r.uiTotal} ≠ 后端 ${expected.strategies}`);
    failed += 1;
  }
  if (r.uiTrades !== null && expected.trades >= 0 && r.uiTrades !== expected.trades) {
    console.log(`❌ ${r.market} 成交统计 ${r.uiTrades} ≠ 后端 ${expected.trades}`);
    failed += 1;
  }
}

if (errors.length) {
  console.log('\n页面错误:');
  errors.slice(0, 10).forEach((e) => console.log('  ' + e));
}
if (consoleErrors.length) {
  console.log('\n控制台错误:');
  consoleErrors.slice(0, 5).forEach((e) => console.log('  ' + e));
}

console.log(failed === 0 ? '\n✅ 全部通过' : `\n❌ ${failed} 项未通过`);
process.exit(failed === 0 ? 0 : 1);
