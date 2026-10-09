/**
 * 展示名重命名 E2E 探针（2026-10-09）
 *
 * 用户诉求：广场导入模型 model_id 是机器名，「模型不知道是干啥的」→
 * 模型管理详情页铅笔按钮改展示名。本探针跑真实 UI 往返：
 *   搜索定位 → 点卡片进详情 → 铅笔 → 改名为「·UI验证」→ 校验详情 h2 与列表卡片
 *   → 再改回原名 → 校验还原。观测 PATCH 网络码与最终截图。
 */
import { chromium } from 'playwright';

const BASE = process.env.QM_BASE || 'http://localhost:3080';
const MID = 'mdl_cn_hub_T_10_LightGBM_d899b0_0792';
const ORIG = 'T+10 LightGBM 选股';
const TEST = 'T+10 LightGBM 选股·UI验证';

const b = await chromium.launch({ executablePath: '/usr/bin/google-chrome-stable', args: ['--no-sandbox'] });
const ctx = await b.newContext({ viewport: { width: 1720, height: 1150 } });
const p = await ctx.newPage();
const patches = [];
const errors = [];
p.on('pageerror', (e) => errors.push((e.message || '').slice(0, 160)));
p.on('response', (r) => {
  if (r.url().includes('/display-name')) {
    patches.push(`${r.status()} ${r.request().method()} ${r.url().split('/api/v1')[1]}`);
  }
});

const dismissLater = async () => {
  const btn = p.locator('.ant-modal-wrap button:has-text("稍后再答")');
  if (await btn.count()) { await btn.first().click(); await p.waitForTimeout(600); }
};

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
await dismissLater();

// ── 深链进模型管理 ──
await p.goto(`${BASE}/#/model-registry`, { waitUntil: 'domcontentloaded' });
await p.waitForTimeout(4000);
await dismissLater();

// ── 搜索定位该模型并进详情 ──
await p.locator('input[placeholder="搜索模型..."]').first().fill('d899b0');
await p.waitForTimeout(1500);
await p.locator('div.cursor-pointer.rounded-2xl').first().click();
await p.waitForTimeout(2500);

const h2Text = async () => (await p.locator('h2').first().innerText().catch(() => '')).trim();
const cardText = async () =>
  (await p.locator('div.cursor-pointer.rounded-2xl').first().innerText().catch(() => '')).replace(/\s+/g, ' ');
console.log('初始 h2:', JSON.stringify(await h2Text()));

const renameTo = async (name) => {
  await p.locator('button[aria-label="修改展示名"]').first().click();
  const modal = p.locator('.ant-modal').filter({ hasText: '修改展示名' }).last();
  await modal.waitFor({ state: 'visible', timeout: 8000 });
  const input = modal.locator('input').first();
  await input.fill(name);
  // antd 两字中文按钮 innerText 会插空格（「保 存」），用正则匹配
  await modal.locator('button').filter({ hasText: /保\s*存/ }).first().click();
  // 等消息条与面板刷新
  await p.waitForTimeout(3500);
};

// ── 改为验证名 ──
await renameTo(TEST);
const h2After = await h2Text();
const cardAfter = await cardText();
console.log('改名后 h2:', JSON.stringify(h2After));
console.log('改名后 卡片:', JSON.stringify(cardAfter.slice(0, 140)));
console.log('改名后 详情包含新名:', h2After.includes(TEST));
console.log('改名后 卡片包含新名:', cardAfter.includes(TEST));
await p.screenshot({ path: '/tmp/rename_after.png' });

// ── 改回原名（往返还原）──
await renameTo(ORIG);
const h2Back = await h2Text();
const cardBack = await cardText();
console.log('还原后 h2:', JSON.stringify(h2Back));
console.log('还原后 h2 复原:', h2Back.includes(ORIG) && !h2Back.includes('UI验证'));
console.log('还原后 卡片复原:', cardBack.includes(ORIG) && !cardBack.includes('UI验证'));

console.log('PATCH 网络:', JSON.stringify(patches, null, 1));
console.log('ERRORS:', errors.length ? errors.slice(0, 4) : 'none');
await p.screenshot({ path: '/tmp/rename_restored.png' });
await b.close();
