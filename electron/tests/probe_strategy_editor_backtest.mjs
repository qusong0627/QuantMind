/**
 * 回测中心「策略编辑」端到端验收：选 minibt 策略 → 运行 → 结果坞。
 *
 * 断言：
 *   - 运行后结果坞自动展开并切到「运行日志」；
 *   - 完成后自动切到「净值」，KPI 条出现非空数值；
 *   - 净值图 canvas 至少 1 张、月度热力格 > 0、成交表头渲染；
 *   - console/pageerror 零错误。
 *
 * 运行：QM_BASE=http://localhost:3080 node electron/tests/probe_strategy_editor_backtest.mjs
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

const waitForChartAtLeast = async (n, timeout = 20000) => {
  const started = Date.now();
  while (Date.now() - started < timeout) {
    if ((await p.locator('.recharts-surface').count()) >= n) return true;
    await p.waitForTimeout(300);
  }
  return false;
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

  // ── 进策略编辑 ────────────────────────────────────────────────────
  await p.goto(`${BASE}/#/dashboard`, { waitUntil: 'domcontentloaded' });
  await p.waitForTimeout(2500);
  await dismissLater();
  await p.locator('[data-nav-id="backtest"]').click();
  await p.waitForTimeout(2000);
  await p.locator('button:has-text("策略编辑")').first().click();
  await p.waitForSelector('[data-testid=strategy-editor-workspace]', { timeout: 30000 });
  await dismissLater();

  // ── 选一个 minibt 策略 ────────────────────────────────────────────
  await p.locator('[data-testid=strategy-file-switcher]').click();
  await p.waitForSelector('.ant-popover .se-file-row', { timeout: 20000 });
  const minibtRow = p
    .locator('.ant-popover .se-file-row button')
    .filter({ hasText: /双均线|（MA5|\(MA5/i })
    .first();
  ok('工作空间里能找到双均线 minibt 策略', (await minibtRow.count()) > 0);
  await minibtRow.click();
  await p.waitForSelector('.monaco-editor', { timeout: 30000 });
  await p.waitForTimeout(1200);
  const codeText = await p.locator('.monaco-editor').innerText();
  ok('编辑器识别为 minibt', codeText.includes('minibt') || (await p.locator('text=minibt').count()) > 0);

  // ── 运行 ──────────────────────────────────────────────────────────
  await p.locator('[data-testid=strategy-run-button]').click();
  await p.waitForSelector('[data-testid=strategy-editor-dock][data-mode="open"]', { timeout: 20000 });
  await p.waitForTimeout(800);
  const activeTab = (await p.locator('.se-tab[data-active="true"]').first().innerText().catch(() => '')).trim();
  ok('运行后结果坞自动展开且切到日志', activeTab.includes('运行日志'), `tab=${activeTab}`);

  // 等待结构化结果：结果页签（净值）出现，再等它被自动选中。
  // 不能等「累计收益」文案——运行日志里也可能出现这四个字，会在结果到达前误判。
  await p.waitForSelector('.se-tab:has-text("净值")', { timeout: 240000 });
  await p.waitForFunction(
    () => {
      const active = document.querySelector('.se-tab[data-active="true"]');
      return Boolean(active && (active.textContent || '').includes('净值'));
    },
    { timeout: 20000 },
  );
  await p.waitForTimeout(1000);
  const netTab = (await p.locator('.se-tab[data-active="true"]').first().innerText().catch(() => '')).trim();
  ok('完成后自动切到净值页', netTab.includes('净值'), `tab=${netTab}`);

  const kpiValues = await p.locator('.se-kpi-value').allInnerTexts();
  ok('KPI 条渲染 7 项', kpiValues.length >= 7, `n=${kpiValues.length}`);
  ok('KPI 有非空数值', kpiValues.some((v) => v.trim() && v.trim() !== '—'), kpiValues.join(' | '));
  ok('净值图渲染', await waitForChartAtLeast(1), 'recharts=0');
  await p.screenshot({ path: '/tmp/se_run_1_net.png' });

  // ── 月度热力 ─────────────────────────────────────────────────────
  await p.locator('.se-tab', { hasText: '月度' }).click();
  await p.waitForSelector('.se-month-cell', { timeout: 15000 });
  const monthCells = await p.locator('.se-month-cell').count();
  ok('月度热力格 > 0', monthCells > 0, `n=${monthCells}`);
  await p.screenshot({ path: '/tmp/se_run_2_monthly.png' });

  // ── 成交/持仓 ────────────────────────────────────────────────────
  await p.locator('.se-tab', { hasText: '成交' }).click();
  await p.waitForTimeout(800);
  ok('成交/持仓表头渲染', (await p.locator('th:has-text("日期")').count()) > 0);
  await p.screenshot({ path: '/tmp/se_run_3_trades.png' });

  // ── 年度 ─────────────────────────────────────────────────────────
  await p.locator('.se-tab', { hasText: '年度' }).click();
  await p.waitForTimeout(600);
  ok('年度表渲染', (await p.locator('th:has-text("收益")').count()) > 0);

  // ── 运行日志 ─────────────────────────────────────────────────────
  await p.locator('.se-tab', { hasText: '运行日志' }).click();
  await p.waitForTimeout(600);
  const logLines = await p.locator('.se-log-line').count();
  ok('运行日志有输出', logLines > 0, `n=${logLines}`);

  // ── console 错误 ─────────────────────────────────────────────────
  const benign = [/favicon/i, /ResizeObserver/i, /net::ERR_ABORTED.*\.map/i];
  const real = consoleErrors.filter((e) => !benign.some((re) => re.test(e)));
  ok('console/pageerror 零错误', real.length === 0, real.slice(0, 3).join(' || '));

  await b.close();
  console.log(fails.length === 0 ? 'PROBE ALL GREEN' : `PROBE FAILED: ${fails.join(', ')}`);
  process.exit(fails.length === 0 ? 0 : 1);
} catch (err) {
  console.log(`PROBE EXCEPTION: ${err.message}`);
  console.log(`console errors so far: ${consoleErrors.slice(0, 5).join(' || ') || '(none)'}`);
  await p.screenshot({ path: '/tmp/se_run_exception.png' }).catch(() => {});
  await b.close();
  process.exit(2);
}
