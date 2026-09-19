import { chromium } from 'playwright';
const BASE = 'http://localhost:3080';
const browser = await chromium.launch({ executablePath: '/usr/bin/google-chrome-stable', args: ['--no-sandbox'] });
const page = await browser.newPage({ viewport: { width: 1760, height: 1000 } });
const logs = [];
page.on('pageerror', e => logs.push('[PAGEERROR] ' + (e.message || '').slice(0, 300)));
page.on('console', m => { if (m.type() === 'error') logs.push('[console.error] ' + m.text().slice(0, 300)); });

await page.goto(`${BASE}/#/auth/login`, { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(3500);
const inputs = page.locator('input');
if (await inputs.count() >= 2) {
  await inputs.nth(0).fill('admin');
  await inputs.nth(1).fill('admin123');
  const btns = page.locator('button');
  for (let i = 0, n = await btns.count(); i < n; i++) {
    const t = ((await btns.nth(i).innerText().catch(() => '')) || '').replace(/\s/g, '');
    if (/登录/.test(t)) { await btns.nth(i).click(); break; }
  }
  await page.waitForTimeout(5000);
}

await page.goto(`${BASE}/#/inference-center`, { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(6000);

// 首访合规弹窗会盖住全屏，先关掉（确认按钮在弹窗底部，可能需滚动）
for (let round = 0; round < 4; round++) {
  const modal = page.locator('.ant-modal:visible');
  if (await modal.count() === 0) break;
  const mbtns = modal.first().locator('button');
  let clicked = false;
  for (let i = 0, n = await mbtns.count(); i < n; i++) {
    const t = ((await mbtns.nth(i).innerText().catch(() => '')) || '').replace(/\s/g, '');
    if (/同意|确认|我知道了|已阅读|知道了/.test(t)) {
      await mbtns.nth(i).scrollIntoViewIfNeeded().catch(() => {});
      await mbtns.nth(i).click({ timeout: 5000 }).catch(async () => { await mbtns.nth(i).click({ force: true }).catch(() => {}); });
      clicked = true;
      break;
    }
  }
  if (!clicked) { await page.keyboard.press('Escape').catch(() => {}); }
  await page.waitForTimeout(1200);
}
await page.waitForTimeout(9000);

const tab = async (label) => {
  const btns = page.locator('nav[aria-label="推理中心工作区"] button');
  for (let i = 0, n = await btns.count(); i < n; i++) {
    const t = ((await btns.nth(i).innerText().catch(() => '')) || '').replace(/\s/g, '');
    if (t.includes(label)) { await btns.nth(i).click(); return true; }
  }
  return false;
};

const snap = async (tag) => {
  await page.waitForTimeout(2500);
  const body = ((await page.locator('body').innerText().catch(() => '')) || '').replace(/\n+/g, ' | ');
  console.log(`--- [${tag}] ---`);
  console.log(body.slice(0, 900));
  await page.screenshot({ path: `/tmp/ic_${tag}.png` });
};

console.log('[tabs present]', await page.locator('nav[aria-label="推理中心工作区"] button').count());
console.log('[tab single]', await tab('单票研判'));
await snap('single');
console.log('[tab cross]', await tab('截面选股'));
await snap('cross');
console.log('[tab governance]', await tab('模型治理'));
await snap('governance');

console.log('[ERRORS]', logs.filter(l => l.startsWith('[PAGEERROR]') || l.startsWith('[console')).join(' || ') || 'none');
await browser.close();
