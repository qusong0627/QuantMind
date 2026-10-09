/**
 * 日终账本曲线（实盘 tab）悬停走查探针（2026-10-08）。
 *
 * 背景：用户报「日终账本 · 通达信桥 曲线都不对」。定案为口径问题——
 * 曲线原以「首个记录日 = 100%」归一（读到 -6.1% 的伪业绩），已改 ¥ 金额。
 * 本探针在改动后的页面上：
 *   1) 切到实盘 tab，截图 日终账本 区块（金额轴 + 右端值）；
 *   2) 悬停曲线中段与末段，截 tooltip（金额 / ±% / 尾注文案）。
 * 用法：node electron/tests/probe_ledger_hover.mjs（从仓库根跑）
 */
import { chromium } from 'playwright';

const BASE = process.env.PROBE_BASE || 'http://localhost:3080';

const browser = await chromium.launch({
  executablePath: '/usr/bin/google-chrome-stable',
  args: ['--no-sandbox'],
});
const page = await browser.newPage({ viewport: { width: 1920, height: 1080 } });

async function login() {
  await page.goto(`${BASE}/#/auth/login`, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(4000);
  const inputs = page.locator('input:visible');
  if ((await inputs.count()) >= 2) {
    await inputs.nth(0).fill('admin');
    await inputs.nth(1).fill('admin123');
    const btns = page.locator('button');
    for (let i = 0, n = await btns.count(); i < n; i++) {
      const t = ((await btns.nth(i).innerText().catch(() => '')) || '').replace(/\s/g, '');
      if (/登录/.test(t)) { await btns.nth(i).click(); break; }
    }
    await page.waitForTimeout(6000);
  }
}

async function dismissModals() {
  for (let round = 0; round < 8; round++) {
    const blocking = await page.evaluate(
      () => Array.from(document.querySelectorAll('.ant-modal-wrap'))
        .filter((m) => getComputedStyle(m).display !== 'none').length,
    );
    if (!blocking) return true;
    let clicked = false;
    const btns = page.locator('.ant-modal:visible button');
    for (let i = 0, n = await btns.count(); i < n && !clicked; i++) {
      const t = ((await btns.nth(i).innerText().catch(() => '')) || '').replace(/\s/g, '');
      if (/稍后再答|同意|确认|我已知晓|已阅读|知道了|跳过/.test(t)) {
        await btns.nth(i).click().catch(() => {});
        clicked = true;
      }
    }
    if (!clicked) await page.keyboard.press('Escape').catch(() => {});
    await page.waitForTimeout(700);
  }
  return false;
}

await login();
await dismissModals();
await page.goto(`${BASE}/#/live`, { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(8000);
await dismissModals();
// 确保在实况页（有 eq-fill-mark 即图表已出）
if (!(await page.locator('.eq-fill-mark').first().isVisible().catch(() => false))) {
  const btn = page.locator('button').filter({ hasText: /^实况$/ }).first();
  await btn.click({ timeout: 8000 }).catch(async () => {
    await dismissModals();
    await btn.click({ force: true });
  });
  await page.waitForTimeout(9000);
  await dismissModals();
}
await page.waitForTimeout(2000);

// 切到实盘 tab
const realTab = page.locator('.trade-tabs button').filter({ hasText: /^实盘$/ }).first();
await realTab.click().catch(async () => { await dismissModals(); await realTab.click({ force: true }); });
await page.waitForTimeout(5000);
await dismissModals();

// 定位 日终账本 区块的图表 svg
const sec = page.locator('.real-section', { hasText: '日终账本' }).last();
await sec.scrollIntoViewIfNeeded().catch(() => {});
const svg = sec.locator('svg').first();
const box = await svg.boundingBox();
if (!box) { console.log('NO_SVG — 日终账本区块没有渲染出图表'); await browser.close(); process.exit(1); }
console.log(`svg box: x=${box.x.toFixed(0)} y=${box.y.toFixed(0)} w=${box.width.toFixed(0)} h=${box.height.toFixed(0)}`);

// 区块截图（金额轴 + 末端标签）
await sec.screenshot({ path: '/tmp/ledger_section.png' });

// 悬停：曲线中段偏上（曲线 y 大致在上半区走向平台），先扫两处
async function hoverAt(fx, fy, tag) {
  await page.mouse.move(box.x + box.width * fx, box.y + box.height * fy, { steps: 4 });
  await page.waitForTimeout(900);
  // tooltip 文本在 svg 内（HoverTip），把 svg 区块截出来
  await sec.screenshot({ path: `/tmp/ledger_hover_${tag}.png` });
  // SVG 元素没有 innerText（HTML 专有），必须用 textContent，否则恒为空数组
  const texts = await svg.locator('text').allTextContents().catch(() => []);
  const tip = texts.filter((t) => /¥|%|实盘账户日终总资产|虚拟净值|盈亏/.test(t));
  console.log(`hover(${tag}) @(${(box.x + box.width * fx).toFixed(0)},${(box.y + box.height * fy).toFixed(0)}) → ${JSON.stringify(tip)}`);
}

// 曲线在 180px 高图里大致走 y=40%→65%；多点扫射提高吸附命中
for (const [fx, fy] of [[0.5, 0.55], [0.5, 0.6], [0.75, 0.6], [0.9, 0.6], [0.95, 0.62]]) {
  await hoverAt(fx, fy, `${fx}_${fy}`);
}

await browser.close();
console.log('screenshots: /tmp/ledger_section.png, /tmp/ledger_hover_*.png');
