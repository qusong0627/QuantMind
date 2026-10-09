/** 截图：手动任务里的「推送下单」、持仓监控右栏里的「风控止损」。输出到 /tmp。 */
import { chromium } from 'playwright';
const BASE = process.env.PROBE_BASE || 'http://localhost:3080';
const browser = await chromium.launch({ executablePath: '/usr/bin/google-chrome-stable', args: ['--no-sandbox'] });
const page = await browser.newPage({ viewport: { width: 1680, height: 1000 } });
const click = async (re, scope = 'button') => {
  const items = page.locator(scope);
  for (let i = 0, n = await items.count(); i < n; i++) {
    const t = ((await items.nth(i).innerText().catch(() => '')) || '').replace(/\s/g, '');
    if (re.test(t)) { await items.nth(i).click({ timeout: 5000 }).catch(() => {}); return true; }
  }
  return false;
};
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
  await inputs.nth(0).fill('admin'); await inputs.nth(1).fill('admin123');
  await click(/登录/); await page.waitForTimeout(6000);
}
await dismiss();
await page.goto(`${BASE}/#/live`, { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(8000);
await dismiss();

await click(/^手动任务$/);
await page.waitForTimeout(7000);
await dismiss();
await page.evaluate(() => {
  const h = Array.from(document.querySelectorAll('h3')).find((x) => (x.innerText || '').includes('推送下单'));
  h?.closest('section')?.scrollIntoView({ block: 'center' });
});
await page.waitForTimeout(1200);
await page.screenshot({ path: '/tmp/merged-manual-task.png' });

await dismiss();
await click(/^持仓监控$/);
await page.waitForTimeout(9000);
await dismiss();
await page.screenshot({ path: '/tmp/merged-position.png' });
await page.evaluate(() => document.querySelector('[data-testid="position-rail"]')?.scrollIntoView({ block: 'start' }));
await page.waitForTimeout(800);
await page.screenshot({ path: '/tmp/merged-position-rail.png' });
console.log('shots: /tmp/merged-manual-task.png /tmp/merged-position.png /tmp/merged-position-rail.png');
await browser.close();
