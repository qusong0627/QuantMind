/**
 * 回测中心「策略编辑」布局验收探针（2026-10-29 重设计）。
 *
 * 断言的是这次重设计的核心修复：
 *   - Monaco 画布 ≥ 700px（旧三栏布局实测只有 222px）；
 *   - 右侧参数栏 318px、底部结果坞可展开/最大化；
 *   - 文件选择器仍可打开并选到 .py（能力没被删）；
 *   - 参数「应用」有无改动/待应用反馈；
 *   - console/pageerror 零错误。
 *
 * 运行：QM_BASE=http://localhost:3080 node electron/tests/probe_strategy_editor_layout.mjs
 */
import { chromium } from 'playwright';

const BASE = process.env.QM_BASE || 'http://localhost:3080';
const fails = [];
const ok = (name, cond, extra = '') => {
  if (cond) console.log(`PASS ${name}`);
  else {
    console.log(`FAIL ${name} ${extra}`);
    fails.push(name);
  }
};

const b = await chromium.launch({
  executablePath: '/usr/bin/google-chrome-stable',
  args: ['--no-sandbox'],
});
const ctx = await b.newContext({ viewport: { width: 1440, height: 1000 } });
const p = await ctx.newPage();
const consoleErrors = [];
p.on('console', (m) => m.type() === 'error' && consoleErrors.push(m.text()));
p.on('pageerror', (e) => consoleErrors.push(`pageerror: ${e.message}`));

const dismissLater = async () => {
  const btn = p.locator('.ant-modal-wrap button:has-text("稍后再答")');
  if (await btn.count()) {
    await btn.first().click();
    await p.waitForTimeout(500);
  }
};

try {
  // ── 登录 ──────────────────────────────────────────────────────────
  await p.goto(`${BASE}/#/auth/login`, { waitUntil: 'domcontentloaded', timeout: 40000 });
  for (let i = 0; i < 20 && (await p.locator('input[type=password]').count()) === 0; i += 1) {
    await p.waitForTimeout(400);
  }
  if ((await p.locator('input[type=password]').count()) > 0) {
    await p.locator('input').nth(0).fill('admin');
    await p.locator('input[type=password]').first().fill('admin123');
    const btns = p.locator('button');
    for (let i = 0; i < (await btns.count()); i += 1) {
      const t = (await btns.nth(i).innerText().catch(() => '')).replace(/\s/g, '');
      if (/登录/.test(t)) {
        await btns.nth(i).click();
        break;
      }
    }
    for (let i = 0; i < 25; i += 1) {
      await p.waitForTimeout(400);
      if (!p.url().includes('/auth/login')) break;
    }
    await p.waitForTimeout(1500);
  }
  await dismissLater();

  // ── 进入回测中心 → 策略编辑 ──────────────────────────────────────
  await p.goto(`${BASE}/#/dashboard`, { waitUntil: 'domcontentloaded' });
  await p.waitForTimeout(2500);
  await dismissLater();
  await p.locator('[data-nav-id="backtest"]').click();
  await p.waitForTimeout(2000);
  await p.locator('button:has-text("策略编辑")').first().click();
  await p.waitForSelector('[data-testid=strategy-editor-workspace]', { timeout: 30000 });
  ok('工作台渲染（strategy-editor-workspace）', true);
  await dismissLater();
  await p.screenshot({ path: '/tmp/se_new_1_empty.png' });

  // ── 文件选择器：打开、选一个 .py ─────────────────────────────────
  await p.locator('[data-testid=strategy-file-switcher]').click();
  await p.waitForSelector('.ant-popover .se-file-row', { timeout: 20000 });
  const fileRows = p.locator('.ant-popover .se-file-row');
  const rowCount = await fileRows.count();
  ok('文件选择器列出条目（≥1）', rowCount >= 1, `count=${rowCount}`);
  const pyRow = p.locator('.ant-popover .se-file-row button').filter({ hasText: /\.py/i }).first();
  ok('工作空间里能找到 .py 文件', (await pyRow.count()) > 0);
  await pyRow.click();
  await p.waitForSelector('.monaco-editor', { timeout: 30000 });
  await p.waitForTimeout(1200);
  await p.screenshot({ path: '/tmp/se_new_2_editor.png' });

  // ── 几何审计：Monaco / 参数栏 ────────────────────────────────────
  const geometry = await p.evaluate(() => {
    const measure = (selector) => {
      const el = document.querySelector(selector);
      if (!el) return null;
      const r = el.getBoundingClientRect();
      return { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) };
    };
    return {
      monaco: measure('.monaco-editor'),
      params: measure('aside.se-params'),
      workspace: measure('[data-testid=strategy-editor-workspace]'),
      dock: measure('[data-testid=strategy-editor-dock]'),
    };
  });
  console.log('geometry:', JSON.stringify(geometry));
  ok('Monaco 宽度 ≥ 700px（旧版 222px）', (geometry.monaco?.w ?? 0) >= 700, `w=${geometry.monaco?.w}`);
  ok('Monaco 高度 ≥ 300px', (geometry.monaco?.h ?? 0) >= 300, `h=${geometry.monaco?.h}`);
  ok('参数栏宽 318px', (geometry.params?.w ?? 0) === 318, `w=${geometry.params?.w}`);
  ok('结果坞默认折叠（44px）', (geometry.dock?.h ?? 0) <= 46, `h=${geometry.dock?.h}`);

  // ── 参数：待应用 → 应用 → 无改动 ─────────────────────────────────
  const symbol = p.locator('#se-param-symbol');
  ok('参数栏含标的代码输入框', (await symbol.count()) === 1);
  const before = await symbol.inputValue();
  await symbol.fill(before === '600036' ? '000001' : '600036');
  await p.waitForTimeout(300);
  ok('改参数后显示「待应用」', (await p.locator('text=待应用').count()) > 0);
  await p.locator('button:has-text("应用参数")').click();
  await p.waitForTimeout(500);
  ok('应用后显示「无改动」', (await p.locator('text=无改动').count()) > 0);

  // ── 结果坞：展开 / 最大化 / 收起 ─────────────────────────────────
  await p.locator('button[aria-label="展开结果面板"]').click();
  await p.waitForTimeout(400);
  ok('结果坞展开', (await p.locator('[data-testid=strategy-editor-dock]').getAttribute('data-mode')) === 'open');
  await p.locator('.se-tab', { hasText: '运行日志' }).click();
  await p.waitForTimeout(300);
  ok('运行日志页签可见', (await p.locator('button:has-text("输出")').count()) > 0);
  await p.screenshot({ path: '/tmp/se_new_3_dock.png' });

  await p.locator('button[aria-label="最大化结果面板"]').click();
  await p.waitForTimeout(500);
  const maxDock = await p.evaluate(() => {
    const el = document.querySelector('[data-testid=strategy-editor-dock]');
    return el ? Math.round(el.getBoundingClientRect().height) : 0;
  });
  ok('最大化后结果坞 ≥ 650px', maxDock >= 650, `h=${maxDock}`);
  await p.screenshot({ path: '/tmp/se_new_4_max.png' });
  await p.locator('button[aria-label="还原分栏"]').click();
  await p.waitForTimeout(300);

  // ── 参数栏收起/展开 ─────────────────────────────────────────────
  await p.locator('button[aria-label="收起参数栏"]').click();
  await p.waitForTimeout(300);
  ok('参数栏可收起（Monaco 变宽）', (await p.locator('aside.se-params').count()) === 0);
  await p.locator('button[aria-label="展开回测设置"]').first().click();
  await p.waitForTimeout(300);
  ok('参数栏可展开', (await p.locator('aside.se-params').count()) === 1);

  // ── console 错误 ────────────────────────────────────────────────
  const benign = [/favicon/i, /ResizeObserver/i, /net::ERR_ABORTED.*\.map/i];
  const real = consoleErrors.filter((e) => !benign.some((re) => re.test(e)));
  ok('console/pageerror 零错误', real.length === 0, real.slice(0, 3).join(' || '));

  await b.close();
  console.log(fails.length === 0 ? 'PROBE ALL GREEN' : `PROBE FAILED: ${fails.join(', ')}`);
  process.exit(fails.length === 0 ? 0 : 1);
} catch (err) {
  console.log(`PROBE EXCEPTION: ${err.message}`);
  console.log(`console errors so far: ${consoleErrors.slice(0, 5).join(' || ') || '(none)'}`);
  await p.screenshot({ path: '/tmp/se_new_exception.png' }).catch(() => {});
  await b.close();
  process.exit(2);
}
