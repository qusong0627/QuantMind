/** 滚动训练前端探测：模型管理 → 选中模型 → 「滚动训练」tab 面板渲染 + 台账真实行（QM_BASE 可覆盖） */
import { chromium } from 'playwright';

const BASE = process.env.QM_BASE || 'http://localhost:3080';
const browser = await chromium.launch({ executablePath: '/usr/bin/google-chrome-stable', args: ['--no-sandbox'] });
const page = await browser.newPage({ viewport: { width: 1680, height: 980 } });
const errs = [];
page.on('pageerror', e => errs.push('[PAGEERROR] ' + (e.message || '').slice(0, 150)));
page.on('console', m => { if (m.type() === 'error') errs.push('[console.error] ' + m.text().slice(0, 150)); });

// 登录
await page.goto(`${BASE}/#/auth/login`, { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(3500);
const inputs = page.locator('input');
if (await inputs.count() >= 2) {
  await inputs.nth(0).fill('admin'); await inputs.nth(1).fill('admin123');
  const btns = page.locator('button');
  for (let i = 0, n = await btns.count(); i < n; i++) {
    const t = ((await btns.nth(i).innerText().catch(() => '')) || '').replace(/\s/g, '');
    if (/登录/.test(t)) { await btns.nth(i).click(); break; }
  }
  await page.waitForTimeout(5000);
}

// 模型管理 → 选中一个模型（点含 mdl_ 文本的行）
await page.goto(`${BASE}/#/model-registry`, { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(7000);
let body = ((await page.locator('body').innerText().catch(() => '')) || '').replace(/\n+/g, ' | ');
console.log('[列表片段]', body.slice(0, 300));
const modelRow = page.locator('text=/mdl_/').first();
console.log('[模型行命中]', await modelRow.count());
await modelRow.click().catch(() => {});
await page.waitForTimeout(5000);
body = ((await page.locator('body').innerText().catch(() => '')) || '').replace(/\n+/g, ' | ');
console.log('[滚动训练 tab 存在]', body.includes('滚动训练'));

// 关掉可能挡路的问卷弹窗（antd Modal）
const modalClose = page.locator('.ant-modal-close:visible').first();
if (await modalClose.count()) { await modalClose.click().catch(() => {}); await page.waitForTimeout(800); }
await page.keyboard.press('Escape').catch(() => {});
await page.waitForTimeout(500);

// 切到滚动训练 tab（dispatchEvent 绕过遮挡层，antd Tabs 监听 tab 节点 click）
const tab = page.locator('[role="tab"]', { hasText: '滚动训练' }).first();
console.log('[tab locator 命中]', await tab.count());
if (await tab.count()) {
  await tab.dispatchEvent('click').catch(async () => { await tab.click({ force: true }).catch(() => {}); });
  await page.waitForTimeout(6000);
}
body = ((await page.locator('body').innerText().catch(() => '')) || '').replace(/\n+/g, ' | ');
const sec = body.split('滚动训练').pop() || '';
console.log('[面板片段]', sec.slice(0, 700));
for (const label of ['派生配方', '立即执行', '月度重训调度', '滚动台账', '预览推导结果', '保存为配方']) {
  console.log(`[${label}]`, body.includes(label));
}
// 台账行（真实 campaign；界面是中文列：触发=手动 / 状态=已注册）
console.log('[台账有行]', /手动|已注册|rc_cn_|train_20/.test(body));
console.log('[ERRORS]', errs.length ? errs.slice(0, 3).join(' || ') : 'none');
await page.screenshot({ path: '/tmp/rolling_tab.png', fullPage: false });
await browser.close();
