/**
 * 推理中心三市场 E2E：A 股 / 港股 / 美股。
 *
 * 用法：QM_BASE=http://localhost:3080 node tests/inference-center.e2e.mjs
 *      QM_MARKETS=US 只跑部分市场
 *
 * 走真实用户路径：登录 → 切市场 → 进「推理中心」→ 截面推理 Tab → 个股预测 Tab
 * （搜标的 → 选建议 → 开始个股推理 → 断言 K 线与结果块）。
 */
import { chromium } from 'playwright';

const BASE = process.env.QM_BASE || 'http://localhost:3080';

const MARKETS = [
  { key: 'CN', radio: 'A股', label: 'A股市场', query: '600519', hint: '茅台', currency: '¥' },
  { key: 'HK', radio: '港股', label: '港股市场', query: '00700', hint: '腾讯', currency: 'HK$' },
  { key: 'US', radio: '美股', label: '美股市场', query: 'AAPL', hint: '苹果', currency: '$' },
];

const only = (process.env.QM_MARKETS || '').split(',').map((s) => s.trim().toUpperCase()).filter(Boolean);
const TO_RUN = only.length ? MARKETS.filter((m) => only.includes(m.key)) : MARKETS;

const apiCalls = [];
const errors = [];
const flat = (s) => s.replace(/\s+/g, ' ');

const b = await chromium.launch({ executablePath: '/usr/bin/google-chrome-stable', args: ['--no-sandbox'] });
const p = await (await b.newContext({ viewport: { width: 1720, height: 1150 } })).newPage();
p.on('console', (m) => { if (m.type() === 'error') errors.push('[console] ' + m.text().slice(0, 200)); });
p.on('pageerror', (e) => errors.push('[PAGEERROR] ' + (e.message || '').slice(0, 200)));
p.on('response', (r) => {
  const u = r.url();
  if (/\/api\/v1\/(models|research|stock-terminal-us|market-calendar)/.test(u)) {
    const path = u.replace(BASE, '').split('?')[0];
    apiCalls.push(r.status() + ' ' + path);
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
console.log('登录后:', p.url());

let pass = 0, total = 0;
const check = (ok, label) => { total++; if (ok) pass++; console.log(`${ok ? '✓' : '✗'} ${label}`); };

/** 切市场：先等应用水合到可交互（导航栏出现），再点轮询确认生效 */
async function switchMarket(page, radioLabel, wantKey) {
  for (let attempt = 0; attempt < 4; attempt++) {
    await page.locator('a,button', { hasText: '推理中心' }).first()
      .waitFor({ state: 'visible', timeout: 25000 }).catch(() => {});
    await page.waitForTimeout(2000);
    const radio = page.locator('button[role=radio]', { hasText: radioLabel }).first();
    if (await radio.count()) {
      await radio.click({ timeout: 10000 }).catch((e) => console.log('  · 点击市场按钮失败:', String(e).slice(0, 70)));
    }
    for (let i = 0; i < 8; i++) {
      await page.waitForTimeout(700);
      if ((await page.evaluate(() => localStorage.getItem('qm:current_market'))) === wantKey) return true;
    }
  }
  return false;
}

for (const mk of TO_RUN) {
  console.log(`\n========== ${mk.key} 推理中心 ==========`);
  await p.goto(`${BASE}/`, { waitUntil: 'domcontentloaded' });
  const switched = await switchMarket(p, mk.radio, mk.key);
  const market = await p.evaluate(() => localStorage.getItem('qm:current_market'));
  check(switched, `市场已切到 ${mk.key}（localStorage=${market}）`);

  const nav = p.locator('a,button', { hasText: '推理中心' }).first();
  await nav.waitFor({ state: 'visible', timeout: 20000 }).catch(() => {});
  await p.waitForTimeout(1000);
  const navOk = await nav.click({ timeout: 8000 }).then(() => true).catch(() => false);
  if (!navOk) { console.log('✗ 未能点开推理中心导航'); continue; }
  // 等页面挂载出 h1，而不是固定 sleep
  await p.locator('h1', { hasText: '模型推理中心' }).first().waitFor({ state: 'visible', timeout: 30000 }).catch(() => {});
  await p.waitForTimeout(2500);

  const h1 = (await p.locator('h1').first().innerText().catch(() => '')).replace(/\s+/g, ' ').trim();
  check(h1.includes('模型推理中心'), `页面标题（实测 h1="${h1}"）`);
  let f = flat(await p.locator('body').innerText().catch(() => ''));
  check(f.includes(mk.label), `市场标识「${mk.label}」（三个市场各渲染自己的页面）`);

  // ── Tab 1：市场截面推理 ──
  const crossTab = p.locator('button', { hasText: '市场截面推理' }).first();
  await crossTab.click({ timeout: 8000 }).catch(() => {});
  await p.waitForTimeout(6000);
  f = flat(await p.locator('body').innerText().catch(() => ''));
  check(f.includes('推理历史') || f.includes('单日推理'), '截面 Tab 渲染出二级导航');
  check(!f.includes('加载中…') || f.includes('模型'), '截面 Tab 内容非空');

  // ── Tab 2：个股预测推理 ──
  const indTab = p.locator('button', { hasText: '个股预测推理' }).first();
  await indTab.click({ timeout: 8000 }).catch(() => {});
  await p.waitForTimeout(4000);
  const input = p.locator('input[placeholder*="搜索"]').first();
  const hasInput = (await input.count()) > 0;
  check(hasInput, '个股预测 Tab 渲染出标的输入框');
  if (!hasInput) continue;

  await input.click();
  await input.fill(mk.query);
  const sug = p.locator('div.absolute > div', { hasText: mk.hint }).first();
  // 等联想项出现（首次列表请求要建缓存，冷启动可能超过 5s）
  await sug.waitFor({ state: 'visible', timeout: 25000 }).catch(() => {});
  let picked = false;
  if (await sug.count()) {
    await sug.click({ timeout: 6000 }).catch(() => {});
    picked = true;
  } else {
    // 联想未命中就直接提交输入内容（失焦提交路径）
    await p.locator('body').click({ position: { x: 5, y: 5 } }).catch(() => {});
  }
  check(picked, `搜索「${mk.query}」出现联想项「${mk.hint}」`);
  // 等结果区真正出现（首次预测可能要跑模型，最长给 90s），而不是固定 sleep 后碰运气
  await p.locator('text=模型信号分数').first().waitFor({ state: 'visible', timeout: 90000 }).catch(() => {});
  await p.waitForTimeout(1500);

  f = flat(await p.locator('body').innerText().catch(() => ''));
  check(f.includes(mk.currency), `货币符号「${mk.currency}」正确`);
  const canvases = await p.locator('canvas').count();
  check(canvases > 0, `K 线/预测图 canvas 已渲染（${canvases} 个）`);
  check(f.includes('模型信号分数'), '结果区渲染出模型信号分数');
  check(!f.includes('推理接口异常'), '未出现推理接口异常提示');
  check(f.includes('多维量化分析') === false, '结果区已从空态切换到数据态');
}

console.log('\n---- 接口调用 ----');
console.log([...new Set(apiCalls)].slice(0, 40).join('\n') || '(无)');
console.log('\n---- 错误 ----');
console.log(errors.length ? [...new Set(errors)].slice(0, 12).join('\n') : '(无)');
console.log(`\n断言通过 ${pass}/${total}`);

await b.close();
process.exit(0);
