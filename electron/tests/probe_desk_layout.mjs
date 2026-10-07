/**
 * 交易台几何探针（系统健康页 ① + 持仓监控右栏 ②）—— 量「对齐」到底有没有做到。
 *
 * 用户点单（① 原样，② 已随面板迁移改版）：
 *   ① 「实时推理放 系统监控右侧对齐」——实时推理必须在**系统健康同一行、右侧**，且顶/底边对齐；
 *   ② 「持仓预警 · 哨兵 和 副驾驶 · 实时情报 显示 2 列。对齐。」
 *      ⚠️ 2026-09-29（commit d4d78ce2）两块按用户 2026-09-20 口径「并进持仓监控」
 *      （它们讲的全是持仓的风险与情报，跟持仓明细分在两页看，风险永远和平仓对不上号）
 *      迁离交易台页脚。几何契约随之挪到 #/trading → 持仓监控 → `position-rail` 右栏：
 *        1440 视口（xl 断点内）= 右栏落主卡下方、两卡并排 **2 列**（原「左右两列」形态）；
 *        1920 视口（≥2xl）      = 右栏收成 380px 单列、两卡**上下堆叠同左同宽**。
 *      原「在交易台页脚同行左右对齐」的断言已随页脚撤掉作废——改动时不要搬回来。
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

/**
 * 首启「投资适当性评估」弹窗必须**真的关掉**：它的 `.ant-modal-wrap` 覆盖全屏，
 * 会把后续一切点击静默吃掉（playwright click 只报超时，看起来像元素不存在——
 * probe_live_arena_tabs 同款坑）。「稍后再答」是该评测的正当出口（7 天内不再问），
 * 等价于用户跳过；文案都不匹配就 Escape；最后以可见 wrap 数归零为收敛判据。
 */
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

// 深链必须带 `#/`（HashRouter）；不带会落到默认页而不是交易台
await page.goto(`${BASE}/#/desk`, { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(6000);
await dismissModals();

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

// ── ② 副驾驶 ｜ 持仓预警：已在「持仓监控」右栏（d4d78ce2 迁入），不再挂交易台 ──
// 断言对象从交易台页脚移到 #/trading → 持仓监控 → position-rail，两档视口各量一次。
await page.goto(`${BASE}/#/trading`, { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(5000);
await dismissModals();
const posTab = page.locator('button', { hasText: /^持仓监控$/ }).first();
if (await posTab.count()) await posTab.click({ timeout: 8000 }).catch(() => {});
await page.waitForSelector('[data-testid="position-rail"]', { timeout: 20000 }).catch(() => {});
await page.waitForTimeout(3000);

const rail = await box('position-rail');
const alerts = await box('holding-alert-panel');
const copilot = await box('copilot-panel');

check('持仓监控右栏存在（position-rail）', !!rail, rail && `top=${rail.top} left=${rail.left} w=${rail.width}`);
check('右栏含持仓预警（哨兵）', !!alerts, alerts ? `top=${alerts.top} h=${alerts.height}` : '未渲染');
check('右栏含副驾驶（实时情报）', !!copilot, copilot ? `top=${copilot.top} h=${copilot.height}` : '未渲染');

if (rail && alerts && copilot) {
  const inRail = (b) => b.left >= rail.left - TOL && b.left + b.width <= rail.left + rail.width + TOL;
  check(
    '1920 ≥2xl：两卡在右栏内、单列堆叠（同左同宽、预警在上）',
    inRail(alerts) && inRail(copilot) &&
      Math.abs(alerts.left - copilot.left) <= TOL &&
      Math.abs(alerts.width - copilot.width) <= TOL &&
      alerts.bottom <= copilot.top + TOL,
    `rail [${rail.left}, ${rail.left + rail.width}]；预警 [${alerts.left}, ${alerts.left + alerts.width}] bottom=${alerts.bottom}；` +
      `副驾驶 [${copilot.left}, ${copilot.left + copilot.width}] top=${copilot.top}`,
  );
} else {
  check('1920 ≥2xl：两卡在右栏内、单列堆叠（同左同宽、预警在上）', false, '缺卡片或右栏，无法量');
}

// 1440（xl 断点内）：右栏落到主卡下方、两卡并排 2 列——用户点名的「显示 2 列」
await page.setViewportSize({ width: 1440, height: 1080 });
await page.waitForTimeout(1500);
{
  const alertsSm = await box('holding-alert-panel');
  const copilotSm = await box('copilot-panel');
  if (alertsSm && copilotSm) {
    check(
      '1440（xl）：两卡并排 2 列（同一行、等宽、左预警右副驾驶）',
      Math.abs(alertsSm.top - copilotSm.top) <= TOL &&
        Math.abs(alertsSm.width - copilotSm.width) <= TOL &&
        alertsSm.left < copilotSm.left,
      `top ${alertsSm.top}/${copilotSm.top}，width ${alertsSm.width}/${copilotSm.width}，left ${alertsSm.left}/${copilotSm.left}`,
    );
  } else {
    check('1440（xl）：两卡并排 2 列（同一行、等宽、左预警右副驾驶）', false, '窄档下缺卡片，无法量');
  }
}
await page.setViewportSize({ width: 1920, height: 1080 });
await page.waitForTimeout(800);

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
