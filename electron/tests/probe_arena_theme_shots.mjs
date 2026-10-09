/**
 * 视觉改造的**截图留档**（2026-09-23：模型卡美化 + 行情回测/关于加宽）。
 *
 * 为什么不并进 `probe_arena_width_audit.mjs`：那个探针断的是**数字**（宽度差、字号、
 * 边框），能防回归但看不出「好不好看」；这里是给人眼看的四张图，两类断言分开跑，
 * 免得截图 I/O 拖慢体检。图落在 /tmp，看完即弃，不进仓。
 *
 * 运行：PROBE_BASE=http://localhost:3080 node electron/tests/probe_arena_theme_shots.mjs
 * ⚠️ 必须从**仓库根**跑（playwright 在 electron/node_modules 下，从 /tmp 跑解析不到）。
 */
import { chromium } from 'playwright';

const BASE = process.env.PROBE_BASE || 'http://localhost:3080';
const browser = await chromium.launch({ executablePath: '/usr/bin/google-chrome-stable', args: ['--no-sandbox'] });
const page = await browser.newPage({ viewport: { width: 1920, height: 1080 } });

const click = async (re, scope = 'button') => {
  const items = page.locator(scope);
  for (let i = 0, n = await items.count(); i < n; i++) {
    const t = ((await items.nth(i).innerText().catch(() => '')) || '').replace(/\s/g, '');
    if (re.test(t)) { await items.nth(i).click({ timeout: 5000 }).catch(() => {}); return true; }
  }
  return false;
};
// 首启「投资适当性评估」的 .ant-modal-wrap 覆盖全屏，不先关掉后面点什么都没反应
const dismiss = async () => {
  for (let r = 0; r < 8; r++) {
    const n = await page.evaluate(() => Array.from(document.querySelectorAll('.ant-modal-wrap')).filter((m) => getComputedStyle(m).display !== 'none').length);
    if (!n) return;
    if (!(await click(/稍后再答|同意|确认|我知道了|已阅读|知道了|跳过/, '.ant-modal:visible button'))) break;
    await page.waitForTimeout(700);
  }
};

await page.goto(`${BASE}/#/auth/login`, { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(4000);
const inputs = page.locator('input:visible');
if ((await inputs.count()) >= 2) {
  await inputs.nth(0).fill(process.env.PROBE_USER || 'admin');
  await inputs.nth(1).fill(process.env.PROBE_PASS || 'admin123');
  await click(/登录/);
  await page.waitForTimeout(6000);
}
await page.goto(`${BASE}/#/live`, { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(8000);
await dismiss();

await click(/^盘中实况$/);
await page.waitForTimeout(15000);
await dismiss();
await page.screenshot({ path: '/tmp/arena_theme_live.png' });
const sec = page.locator('.qm-arena-root .model-cards-section');
if (await sec.count()) {
  await sec.first().screenshot({ path: '/tmp/arena_theme_cards.png' });
  console.log('模型卡特写 /tmp/arena_theme_cards.png');
} else {
  console.log('⚠️ 没找到 .model-cards-section，特写跳过');
}

await click(/^行情回测$/);
await page.waitForTimeout(13000);
await dismiss();
await page.screenshot({ path: '/tmp/arena_theme_marketlab.png' });

await click(/^设置$/);
await page.waitForTimeout(5000);
await dismiss();
await click(/^关于$/);
await page.waitForTimeout(7000);
await dismiss();
await page.screenshot({ path: '/tmp/arena_theme_about.png' });

await browser.close();
console.log('截图完成：/tmp/arena_theme_{live,cards,marketlab,about}.png');
