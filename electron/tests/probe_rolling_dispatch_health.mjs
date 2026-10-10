/**
 * 滚动训练「派发器心跳」标记探针（2026-10-10，P2-4 半 A）
 *
 * 背景（审计 M5）：调度存了 enabled 但派发 ticker 死掉时整月不可见——本月
 * 滚动重训是人工补位派发的。修后 GET /models/rolling/schedule 带 dispatch
 * 心跳块（与体检 C07 同口径），面板「月度重训调度」区渲染状态标记。
 *
 * 验证：模型管理 → 选中模型 → 「滚动训练」页签 → 出现 .rp-dispatch-health
 * 标记，且文案与后端 heartbeat state 对应；/schedule 响应含 dispatch 块。
 */
import { chromium } from 'playwright';

const BASE = process.env.QM_BASE || 'http://localhost:3080';
const b = await chromium.launch({ executablePath: '/usr/bin/google-chrome-stable', args: ['--no-sandbox'] });
const ctx = await b.newContext({ viewport: { width: 1720, height: 1150 } });
const p = await ctx.newPage();
const errors = [];
let scheduleResp = null;
p.on('pageerror', (e) => errors.push((e.message || '').slice(0, 160)));
p.on('response', async (r) => {
  if (r.url().includes('/models/rolling/schedule') && r.request().method() === 'GET') {
    scheduleResp = await r.json().catch(() => null);
  }
});

// ── 登录 ──
await p.goto(`${BASE}/#/auth/login`, { waitUntil: 'domcontentloaded', timeout: 40000 });
await p.waitForTimeout(3500);
if ((await p.locator('input[type=password]').count()) > 0) {
  await p.locator('input').nth(0).fill('admin');
  await p.locator('input[type=password]').first().fill('admin123');
  const btns = p.locator('button');
  for (let i = 0; i < (await btns.count()); i++) {
    const t = (await btns.nth(i).innerText().catch(() => '')).replace(/\s/g, '');
    if (/登录/.test(t)) { await btns.nth(i).click(); break; }
  }
  await p.waitForTimeout(6000);
}
const later = p.locator('.ant-modal-wrap button:has-text("稍后再答")');
if (await later.count()) { await later.first().click(); await p.waitForTimeout(600); }

// ── 模型管理 → 选一个模型 → 滚动训练页签 ──
await p.goto(`${BASE}/#/model-registry`, { waitUntil: 'domcontentloaded' });
await p.waitForTimeout(5000);
const later2 = p.locator('.ant-modal-wrap button:has-text("稍后再答")');
if (await later2.count()) { await later2.first().click(); await p.waitForTimeout(600); }

// 左栏模型卡片（ModelCard 根 div 带 cursor-pointer）——选第一个模型
await p.locator('div.cursor-pointer').first().click();
await p.waitForTimeout(3000);
await p.locator('button, [role=tab], a, div', { hasText: '滚动训练' }).last().click();
await p.waitForTimeout(2500);

// 硬等待：等标记元素出现（不靠 timeout 断言）
await p.waitForFunction(
  () => !!document.querySelector('.rp-dispatch-health'),
  null,
  { timeout: 20000 },
).catch(() => {});

const chip = await p.locator('.rp-dispatch-health .rp-pill').first().innerText().catch(() => '(缺失)');
const cls = await p.locator('.rp-dispatch-health .rp-pill').first().getAttribute('class').catch(() => '');
const flat = (await p.locator('body').innerText().catch(() => '')).replace(/\s+/g, ' ');

console.log('标记文案:', JSON.stringify(chip));
console.log('标记 class:', cls);
console.log('schedule 响应 dispatch 块:', JSON.stringify(scheduleResp?.dispatch ?? null));
console.log('契约一致（文案与 state 对应）:', (() => {
  const s = scheduleResp?.dispatch?.state;
  if (!s) return '(无响应可比)';
  if (s === 'ok') return /派发器正常/.test(chip);
  if (s === 'stale') return /心跳过期/.test(chip);
  if (s === 'off') return /总闸未开/.test(chip);
  return /无心跳记录/.test(chip);
})());
console.log('旧文案残留（应为 false）:', flat.includes('调度总闸 RETRAIN_SCHEDULER_ENABLED 见部署配置'));
console.log('ERRORS:', errors.length ? errors.slice(0, 4) : 'none');
await p.screenshot({ path: '/tmp/rolling_dispatch_health.png' });
await b.close();
