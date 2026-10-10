/** 父本种子选择器（T-MV-01）探针：拆解首页 → 父本 chip → 面板拉池内因子 → 选中计数 */
import { chromium } from 'playwright';

const BASE = process.env.QM_BASE || 'http://localhost:3080';
const b = await chromium.launch({ executablePath: '/usr/bin/google-chrome-stable', args: ['--no-sandbox'] });
const ctx = await b.newContext({ viewport: { width: 1720, height: 1150 } });
const p = await ctx.newPage();

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
const dismissLater = async () => {
  for (const label of ['稍后再答', '我知道了', '暂不升级']) {
    const btn = p.locator(`.ant-modal-wrap button:has-text("${label}")`);
    if (await btn.count()) { await btn.first().click(); await p.waitForTimeout(600); }
  }
};
await dismissLater();

await p.goto(`${BASE}/#/alpha-research`, { waitUntil: 'domcontentloaded' });
await p.waitForTimeout(7000);
await dismissLater();

// 输入方向（拆解按钮与父本选取都不依赖输入，但贴近真实使用）
const ta = p.locator('textarea').first();
await ta.fill('动量与波动率组合探针');

// 父本 chip（title 含「父本」——未选时「父本定向演化…」，已选时「已选 N 个父本…」）
const chip = p.locator('button[title*="父本"]').first();
console.log('chip count:', await chip.count());
await chip.click();

// 面板出现 → 等待加载结束（要么出选项，要么出空池/错误文案）
await p.waitForFunction(
  () => {
    const text = document.body.innerText;
    if (text.includes('正在读取本池因子')) return false;
    return text.includes('父本因子（可选') &&
      (text.includes('本池暂无已完成回测的因子') || text.includes('ICIR') || text.includes('IC '));
  },
  { timeout: 30000 },
);

const panelText = await p.evaluate(() => {
  const el = [...document.querySelectorAll('span')].find((s) => s.textContent?.includes('父本因子（可选'));
  return el ? el.closest('div')?.parentElement?.innerText?.slice(0, 600) : '(panel not found)';
});
console.log('PANEL:', panelText?.replace(/\n+/g, ' | ').slice(0, 500));
await p.screenshot({ path: '/tmp/seed_picker_open.png' });

// 选第一个选项 → 计数徽章 1/3
const firstOption = p.locator('div.max-h-44 > button').first();
if (await firstOption.count()) {
  await firstOption.click();
  await p.waitForTimeout(400);
  const chipText = (await chip.innerText()).replace(/\s/g, '');
  console.log('chip text after select:', chipText);
  const chipTitle = await chip.getAttribute('title');
  console.log('chip title after select:', chipTitle);
  await p.screenshot({ path: '/tmp/seed_picker_selected.png' });
} else {
  console.log('NO OPTIONS: pool empty for this user/market');
}
await b.close();
console.log('probe done');
