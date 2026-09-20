/**
 * 个人中心「持仓监控与提醒 ｜ 提醒通道」两列对称布局探针。
 *
 * 需求原话：「这2个个人中心的设置，分2列、左右对称」。单元测试锁的是 class 契约，
 * 这里量的是**真实渲染出来的几何**——同排、等宽、等高、左右并排，窄屏才允许堆叠。
 * 只断言 class 会在样式被覆盖/栅格失效时假绿，所以两者都要。
 *
 * ⚠️ 深链必须带 `#/`（HashRouter）。`personal` 不在 DEEP_LINKABLE 里，只能点页签。
 *
 * 用法：PROBE_BASE=http://localhost:3000 node tests/probe_alert_settings_layout.mjs
 * 退出码：0 全过；1 有 FAIL。
 */
import { chromium } from 'playwright';

const BASE = process.env.PROBE_BASE || 'http://localhost:3000';
const HEADLESS = process.env.PROBE_HEADFUL !== '1';
/** 允许的亚像素误差（边框/舍入）*/
const EPS = 2;

const results = [];
const record = (name, ok, detail) => {
  results.push({ name, ok, detail });
  console.log(`${ok ? '✓ PASS' : '✗ FAIL'} ${name}${detail ? ` — ${detail}` : ''}`);
};

const browser = await chromium.launch({
  executablePath: '/usr/bin/google-chrome-stable',
  args: ['--no-sandbox'],
  headless: HEADLESS,
});
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
const pageErrors = [];
page.on('pageerror', (e) => pageErrors.push((e.message || '').slice(0, 200)));

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
  for (let round = 0; round < 4; round++) {
    const modal = page.locator('.ant-modal:visible');
    if ((await modal.count()) === 0) break;
    const btns = modal.first().locator('button');
    let clicked = false;
    for (let i = 0, n = await btns.count(); i < n; i++) {
      const t = ((await btns.nth(i).innerText().catch(() => '')) || '').replace(/\s/g, '');
      if (/同意|确认|我知道了|已阅读|知道了|跳过|稍后/.test(t)) {
        await btns.nth(i).click({ timeout: 5000 }).catch(() => {});
        clicked = true; break;
      }
    }
    if (!clicked) await page.keyboard.press('Escape').catch(() => {});
    await page.waitForTimeout(1000);
  }
}

/** 打开交易台 → 个人中心页签 */
async function openPersonalCenter() {
  await page.goto(`${BASE}/#/trading`, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(5000);
  await dismissModals();
  const tab = page.locator('button', { hasText: /^个人中心$/ }).first();
  await tab.waitFor({ timeout: 15000 });
  await tab.click({ timeout: 8000 });
  await page.waitForSelector('[data-testid="holding-monitor-card"]', { timeout: 20000 });
  await page.waitForSelector('[data-testid="holding-alert-channels"]', { timeout: 20000 });
  await page.waitForTimeout(2000); // 等哨兵状态/配置落地，避免量到骨架
}

/** 量两卡的几何（先滚进视口，保证 layout 稳定） */
const measure = () => page.evaluate(() => {
  const box = (sel) => {
    const el = document.querySelector(sel);
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return {
      top: Math.round(r.top), left: Math.round(r.left),
      width: Math.round(r.width), height: Math.round(r.height),
      bottom: Math.round(r.bottom), right: Math.round(r.right),
      parentChildren: el.parentElement ? el.parentElement.children.length : -1,
      parentIsShared: null,
      title: (el.querySelector('h4')?.textContent || '').trim(),
    };
  };
  const left = box('[data-testid="holding-monitor-card"]');
  const right = box('[data-testid="holding-alert-channels"]');
  const l = document.querySelector('[data-testid="holding-monitor-card"]');
  const r = document.querySelector('[data-testid="holding-alert-channels"]');
  const els = [l, r];
  return {
    left, right,
    sameParent: !!l && !!r && l.parentElement === r.parentElement,
    parentClass: l?.parentElement?.className || '',
    inViewport: els.every((e) => e && e.getBoundingClientRect().top < window.innerHeight),
  };
});

await login();
await openPersonalCenter();

// ── 桌面：左右并排、等宽、等高、同排 ──────────────────────────────
{
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.waitForTimeout(600);
  const m = await measure();

  record('两卡落在同一个父容器（同一排的对称前提）', m.sameParent && m.left.parentChildren === 2,
    `sameParent=${m.sameParent} 父容器子元素数=${m.left?.parentChildren}`);
  record('父容器是两列栅格（lg:grid-cols-2）', /lg:grid-cols-2/.test(m.parentClass), m.parentClass);
  record('标题各就各位', m.left?.title === '持仓监控与提醒' && m.right?.title === '提醒通道',
    `左=${m.left?.title} 右=${m.right?.title}`);
  record('左右并排（左侧卡右边缘不超过右侧卡左边缘）',
    !!m.left && !!m.right && m.left.right <= m.right.left + EPS,
    `left.right=${m.left?.right} right.left=${m.right?.left}`);
  record('两卡等宽', !!m.left && Math.abs(m.left.width - m.right.width) <= EPS,
    `左=${m.left?.width}px 右=${m.right?.width}px`);
  record('两卡顶端齐平（同排不参差）', !!m.left && Math.abs(m.left.top - m.right.top) <= EPS,
    `左 top=${m.left?.top} 右 top=${m.right?.top}`);
  record('两卡等高（底边对齐 = 视觉对称）', !!m.left && Math.abs(m.left.height - m.right.height) <= EPS,
    `左=${m.left?.height}px 右=${m.right?.height}px`);
  await page.screenshot({ path: '/tmp/probe_alert_settings_1440.png', fullPage: false });
}

// ── 窄屏：允许堆叠，但仍须等宽（不能一宽一窄） ────────────────────
{
  await page.setViewportSize({ width: 767, height: 1000 });
  await page.waitForTimeout(800);
  const m = await measure();
  record('窄屏堆叠（右卡在左卡下方）', !!m.left && !!m.right && m.right.top >= m.left.bottom - EPS,
    `左 bottom=${m.left?.bottom} 右 top=${m.right?.top}`);
  record('窄屏仍等宽', !!m.left && Math.abs(m.left.width - m.right.width) <= EPS,
    `左=${m.left?.width}px 右=${m.right?.width}px`);
  await page.screenshot({ path: '/tmp/probe_alert_settings_767.png', fullPage: false });
}

// ── 无 JS 报错 ────────────────────────────────────────────────────
record('页面无未捕获异常', pageErrors.length === 0, pageErrors.slice(0, 2).join(' | '));

await browser.close();

const failed = results.filter((r) => !r.ok);
console.log(`\n结果：PASS ${results.length - failed.length} / FAIL ${failed.length}`);
console.log('截图：/tmp/probe_alert_settings_1440.png, /tmp/probe_alert_settings_767.png');
process.exit(failed.length === 0 ? 0 : 1);
