/**
 * 交易台（系统健康页）两列几何探针 —— 量「对齐」到底有没有做到。
 *
 * 用户两次点单：
 *   ① 「实时推理放 系统监控右侧对齐」——实时推理必须在**系统健康同一行、右侧**，且顶/底边对齐；
 *   ② 「持仓预警 · 哨兵 和 副驾驶 · 实时情报 显示 2 列。对齐。」——同上，
 *      且必须是**左右两列**（不是上下堆叠成 2 行）。
 *
 * 默认打 3000（本仓库自己的 vite dev，`VITE_PORT=3000 npx vite`），源码改动立刻可见，
 * 不必先跑 deploy_frontend.sh。⚠️ **别用 5173** —— 那是另一个项目（inkwell）的 vite。
 *
 * 口径：只量 getBoundingClientRect（top/bottom/left/width/行号），
 * 不做像素级视觉判断 ——「对齐」要落到数字上。
 */
import { chromium } from 'playwright';

const BASE = process.env.PROBE_BASE || 'http://localhost:3000';
/** 顶/底边容差：同行的盒子受 flex stretch 影响可能有 1px 级差异 */
const TOL = 2;

const browser = await chromium.launch({
  executablePath: '/usr/bin/google-chrome-stable',
  args: ['--no-sandbox'],
});
const page = await browser.newPage({ viewport: { width: 1920, height: 1080 } });
const errs = [];
page.on('pageerror', (e) => errs.push('[PAGEERROR] ' + (e.message || '').slice(0, 200)));

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

// 深链必须带 `#/`（HashRouter）；不带会落到默认页而不是交易台
await page.goto(`${BASE}/#/desk`, { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(6000);

// 首访合规弹窗
for (let round = 0; round < 4; round++) {
  const modal = page.locator('.ant-modal:visible');
  if ((await modal.count()) === 0) break;
  const mbtns = modal.first().locator('button');
  let clicked = false;
  for (let i = 0, n = await mbtns.count(); i < n; i++) {
    const t = ((await mbtns.nth(i).innerText().catch(() => '')) || '').replace(/\s/g, '');
    if (/同意|确认|我知道了|已阅读|知道了/.test(t)) {
      await mbtns.nth(i).click({ timeout: 5000 }).catch(() => {});
      clicked = true; break;
    }
  }
  if (!clicked) break;
  await page.waitForTimeout(1200);
}

const box = async (testid) => {
  const el = page.locator(`[data-testid="${testid}"]`).first();
  if ((await el.count()) === 0) return null;
  if (!(await el.isVisible().catch(() => false))) return null;
  return await el.evaluate((node) => {
    const r = node.getBoundingClientRect();
    return {
      top: Math.round(r.top),
      bottom: Math.round(r.bottom),
      left: Math.round(r.left),
      width: Math.round(r.width),
      height: Math.round(r.height),
      text: (node.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 60),
    };
  });
};

const results = [];
const check = (name, ok, detail) => {
  results.push({ name, ok, detail });
  console.log(`${ok ? '  ✅' : '  ❌'} ${name}${detail ? ` — ${detail}` : ''}`);
};

console.log(`\n=== 交易台两列几何 @ ${BASE} (视口 1920) ===\n`);

const health = await box('health-card');
const infer = await box('realtime-infer-card');
const inferNa = await box('realtime-infer-na');
const copilot = await box('copilot-panel');
const alerts = await box('holding-alert-panel');

check('系统健康卡存在', !!health, health && `top=${health.top} left=${health.left} w=${health.width}`);
check(
  '实时推理卡存在（CN 页签；非 CN 应出现 realtime-infer-na）',
  !!infer || !!inferNa,
  infer ? 'cn' : inferNa ? 'na-占位' : '两张都没有',
);

const pair = (a, b, label, { requireRight = true } = {}) => {
  if (!a || !b) {
    check(`${label}：同一行且左右对齐`, false, '缺卡片，无法量');
    return;
  }
  const sameRow = Math.abs(a.top - b.top) <= TOL;
  const sameBottom = Math.abs(a.bottom - b.bottom) <= TOL;
  const sameWidth = Math.abs(a.width - b.width) <= TOL;
  const leftFirst = a.left < b.left;
  if (requireRight) {
    check(
      `${label}：同一行（top/bottom 对齐）`,
      sameRow && sameBottom,
      `top ${a.top}/${b.top}（差 ${Math.abs(a.top - b.top)}），bottom ${a.bottom}/${b.bottom}（差 ${Math.abs(a.bottom - b.bottom)}）`,
    );
    check(`${label}：等宽两列`, sameWidth, `宽度 ${a.width}/${b.width}（差 ${Math.abs(a.width - b.width)}）`);
    check(`${label}：左卡在左、右卡在右`, leftFirst, `left ${a.left}/${b.left}`);
  } else {
    check(
      `${label}：同一个两列网格内`,
      sameRow && sameWidth,
      `top ${a.top}/${b.top}，宽 ${a.width}/${b.width}`,
    );
  }
};

if (infer) pair(health, infer, '系统健康 ｜ 实时推理');
else if (inferNa) pair(health, inferNa, '系统健康 ｜ 实时推理占位', { requireRight: false });
pair(copilot, alerts, '副驾驶 ｜ 持仓预警');

// 副驾驶/持仓预警必须在实时推理那行的**下面**（不是又挤回同一行）
if (copilot && (infer || inferNa)) {
  const anchor = infer || inferNa;
  check(
    '行序：副驾驶行在实时推理行之下',
    copilot.top > anchor.top,
    `copilot.top=${copilot.top} > 上一行.top=${anchor.top}`,
  );
}

const pageErrs = errs.filter((e) => !/ResizeObserver|favicon/.test(e));
check('无页面级 JS 错误', pageErrs.length === 0, pageErrs.slice(0, 3).join(' | '));

console.log('\n卡片文本取样（确认不是空壳）:');
for (const [k, v] of Object.entries({ health, infer, inferNa, copilot, alerts })) {
  if (v) console.log(`  ${k}: ${v.text}`);
}

await browser.close();

const failed = results.filter((r) => !r.ok);
console.log(`\n${failed.length === 0 ? 'PASS' : `FAIL（${failed.length} 项）`}：${results.length} 项检查`);
process.exit(failed.length === 0 ? 0 : 1);
