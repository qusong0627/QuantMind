/**
 * 副驾驶面板「陈旧即显形」探针（审计 H15）。
 *
 * 旧行为：as_of 是**响应时刻**——哨兵/总线停摆数小时，面板照样写「截至 <现在>」，
 * 前台看着新鲜。修复后：as_of = 数据时刻（最新事件 ts）；超过 300s → 挂横幅；
 * 事件行渲染自带 ts。本探针把 /copilot/panel 桩成「最新事件在 42 分钟前」，
 * 断言：① 头部「截至」显示的是**真实旧时间**（不是现在）；② 陈旧横幅带真实年龄；
 * ③ 事件行 ts 可见；④ 切回新鲜数据（点刷新）后横幅消失。
 *
 * 默认打 3000（本仓库 vite dev，源码改动立刻可见）。注意别用 5173（另一项目的 vite）。
 */
import { chromium } from 'playwright';

const BASE = process.env.PROBE_BASE || 'http://localhost:3000';

const pad = (v) => String(v).padStart(2, '0');
const hms = (ms) => {
  const d = new Date(ms);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
};

// 保证「42 分钟前」不跨本地零点（跨零点后头部标签变 MM-DD HH:MM，格式断言会分叉）
const now = Date.now();
const nowD = new Date(now);
const ageMin = nowD.getHours() === 0 && nowD.getMinutes() < 45 ? 10 : 42;
const oldMs = now - ageMin * 60 * 1000;
const oldIso = new Date(oldMs).toISOString();
const freshIso = new Date(now).toISOString();

const eventAt = (ts) => ({
  alert_id: 'probe-h15-1',
  ts,
  alert_type: 'news:risk_event',
  severity: 'warn',
  market: 'CN',
  symbol: '600036.SH',
  title: '探针事件：某银行被立案调查',
  targets: [],
  pushed: true,
  outcome_status: 'filled',
  hit: true,
  annotation: null,
});

/** 面板桩：mode=stale → as_of/事件均为 42 分钟前的旧数据；fresh → 现在 */
let mode = 'stale';
const panelPayload = () => ({
  success: true,
  data:
    mode === 'stale'
      ? { as_of: oldIso, events: { available: true, items: [eventAt(oldIso)], source: 'probe:stub' } }
      : { as_of: freshIso, events: { available: true, items: [eventAt(freshIso)], source: 'probe:stub' } },
});

const browser = await chromium.launch({
  executablePath: '/usr/bin/google-chrome-stable',
  args: ['--no-sandbox'],
});
const page = await browser.newPage({ viewport: { width: 1920, height: 1080 } });
const errs = [];
page.on('pageerror', (e) => errs.push('[PAGEERROR] ' + (e.message || '').slice(0, 200)));

// 桩必须挂在登录/导航之前：SPA 首次进面板就会拉 /copilot/panel
await page.route('**/api/v1/copilot/panel*', (route) => route.fulfill({ json: panelPayload() }));

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

// 首启「投资适当性评估」弹窗会静默吞点击（probe_desk_layout 同款坑）
const dismissModals = async () => {
  for (let round = 0; round < 8; round++) {
    const blocking = await page.evaluate(
      () =>
        Array.from(document.querySelectorAll('.ant-modal-wrap')).filter(
          (m) => getComputedStyle(m).display !== 'none'
        ).length
    );
    if (!blocking) return true;
    const btns = page.locator('.ant-modal:visible button');
    let clicked = false;
    for (let i = 0, n = await btns.count(); i < n; i++) {
      const t = ((await btns.nth(i).innerText().catch(() => '')) || '').replace(/\s/g, '');
      if (/稍后再答|同意|确认|我知道了|已阅读|知道了|跳过/.test(t)) {
        await btns.nth(i).click({ timeout: 5000 }).catch(() => {});
        clicked = true;
        break;
      }
    }
    if (!clicked) await page.keyboard.press('Escape').catch(() => {});
    await page.waitForTimeout(800);
  }
  return false;
};

// 副驾驶面板在 #/trading → 持仓监控 → position-rail（d4d78ce2 迁入）
await page.goto(`${BASE}/#/trading`, { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(5000);
await dismissModals();
const posTab = page.locator('button', { hasText: /^持仓监控$/ }).first();
if (await posTab.count()) await posTab.click({ timeout: 8000 }).catch(() => {});
await page.waitForSelector('[data-testid="copilot-panel"]', { timeout: 20000 }).catch(() => {});
await page.waitForTimeout(3000);

const results = [];
const check = (name, ok, detail) => {
  results.push({ name, ok, detail });
  console.log(`${ok ? '  ✅' : '  ❌'} ${name}${detail ? ` — ${detail}` : ''}`);
};

console.log(`\n=== 副驾驶面板陈旧显形 @ ${BASE}（桩：最新事件 ${ageMin} 分钟前）===\n`);

const panel = page.locator('[data-testid="copilot-panel"]').first();
check('副驾驶面板存在', (await panel.count()) > 0);

const headerText = await panel.getByTestId('copilot-asof').innerText().catch(() => '');
const expectedOld = `截至 ${hms(oldMs)}`;
check(
  '头部「截至」= 真实旧数据时刻（不是响应时刻）',
  headerText.includes(expectedOld) && !headerText.includes(`截至 ${hms(Date.now())}`),
  `header="${headerText.trim()}" 期望含 "${expectedOld}"`,
);

const banner = panel.getByTestId('copilot-stale-banner');
const bannerVisible = (await banner.count()) > 0 && (await banner.first().isVisible().catch(() => false));
check('陈旧横幅可见', bannerVisible);
if (bannerVisible) {
  const text = (await banner.first().innerText()).replace(/\s+/g, ' ');
  check(
    `横幅带真实年龄（${ageMin} 分钟前）且不冒充实时`,
    text.includes('情报数据陈旧') && text.includes(`${ageMin} 分钟前`) && text.includes('按旧数据对待'),
    `banner="${text}"`,
  );
}

const tsCell = panel.locator(`[title="${oldIso}"]`).first();
check(
  '事件行渲染事件自带 ts（title=原始 ISO）',
  (await tsCell.count()) > 0 && (await tsCell.innerText()).includes(hms(oldMs)),
);

// ④ 数据恢复新鲜（点刷新）→ 横幅消失、头部跟上
mode = 'fresh';
const refreshBtn = panel.locator('button', { hasText: /刷新/ }).first();
await refreshBtn.click({ timeout: 8000 }).catch(() => {});
await page.waitForTimeout(2500);
check(
  '数据恢复后横幅消失（陈旧判定跟着数据走）',
  (await banner.count()) === 0,
  `banner count=${await banner.count()}`,
);
const headerText2 = await panel.getByTestId('copilot-asof').innerText().catch(() => '');
check('恢复后头部「截至」不再显示旧时刻', !headerText2.includes(expectedOld), `header="${headerText2.trim()}"`);

const pageErrs = errs.filter((e) => !/ResizeObserver|favicon/.test(e));
check('无页面级 JS 错误', pageErrs.length === 0, pageErrs.slice(0, 3).join(' | '));

await browser.close();

const failed = results.filter((r) => !r.ok);
console.log(`\n${failed.length === 0 ? 'PASS' : `FAIL（${failed.length} 项）`}：${results.length} 项检查`);
process.exit(failed.length === 0 ? 0 : 1);
