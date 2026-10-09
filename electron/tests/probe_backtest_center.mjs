/**
 * 回测中心（T-FB-20 UI 验收探针）。
 *
 * 链路：登录 → #/alpha-research → 点 nav「回测中心」→ 派发台选因子
 * （搜索精确因子名 → 勾选）→ 矩阵渲染（显著性列 NW t）→ 点完成格开报告抽屉
 * （页签/BENCH_LABELS 中文基准/机构报告块/导出按钮）→ Esc 关 → 台账批次展开
 * （批内单元行「看报告」）→ console error/pageerror = 0。
 *
 * 前置：批量 fbb-679c0c5be5fc44f19222deb878294923（4 因子 × 5 市场）已在排水，
 * a_share 单元应为 completed（本探针断言其 data-status=completed）。
 * 运行：QM_BASE=http://localhost:3080 node electron/tests/probe_backtest_center.mjs
 */
import { chromium } from 'playwright';

const BASE = process.env.QM_BASE || 'http://localhost:3080';
const BATCH_ID = 'fbb-679c0c5be5fc44f19222deb878294923';
const FACTOR_ID = '067a79bc213d1d1ef52147ca6fd37d0d'; // idio_maxret20_rev
const FACTOR_NAME = 'idio_maxret20_rev';

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
const ctx = await b.newContext({ viewport: { width: 1720, height: 1150 } });
const p = await ctx.newPage();

const consoleErrors = [];
p.on('console', (m) => {
  if (m.type() === 'error') consoleErrors.push(m.text());
});
p.on('pageerror', (e) => consoleErrors.push(`pageerror: ${e.message}`));

try {
  // ── 登录 ──────────────────────────────────────────────────────────
  await p.goto(`${BASE}/#/auth/login`, { waitUntil: 'domcontentloaded', timeout: 40000 });
  await p.waitForTimeout(3500);
  if ((await p.locator('input[type=password]').count()) > 0) {
    await p.locator('input').nth(0).fill('admin');
    await p.locator('input[type=password]').first().fill('admin123');
    const btns = p.locator('button');
    for (let i = 0; i < (await btns.count()); i++) {
      const t = (await btns.nth(i).innerText().catch(() => '')).replace(/\s/g, '');
      if (/登录/.test(t)) {
        await btns.nth(i).click();
        break;
      }
    }
    await p.waitForTimeout(6000);
  }
  const dismissLater = async () => {
    const btn = p.locator('.ant-modal-wrap button:has-text("稍后再答")');
    if (await btn.count()) {
      await btn.first().click();
      await p.waitForTimeout(600);
    }
  };
  await dismissLater();

  // ── 进回测中心 ────────────────────────────────────────────────────
  await p.goto(`${BASE}/#/alpha-research`, { waitUntil: 'domcontentloaded' });
  await p.waitForTimeout(4000);
  await p.locator('button:has-text("回测中心")').first().click();
  await p.waitForSelector('[data-testid=backtest-center]', { timeout: 20000 });
  await p.waitForSelector('[data-testid=batch-dispatch]', { timeout: 20000 });
  ok('回测中心页渲染（backtest-center + batch-dispatch）', true);
  await p.screenshot({ path: '/tmp/backtest_probe_1_center.png' });

  // ── 派发台：搜索精确因子名 → 勾选 ─────────────────────────────────
  const search = p.locator('input[aria-label="搜索因子"]');
  await search.fill(FACTOR_NAME);
  await p.waitForTimeout(800);
  const row = p.locator(`label:has-text("${FACTOR_NAME}")`).first();
  ok('搜索命中因子行', (await row.count()) > 0);
  await row.locator('input[type=checkbox]').click();
  await p.waitForTimeout(600);
  const selText = await p.locator('[data-testid=batch-dispatch]').innerText();
  ok('派发台记「1 已选」', /因子（\s*1\s*已选/.test(selText), selText.slice(0, 120));

  // ── 矩阵：等待行与格渲染 ──────────────────────────────────────────
  await p.waitForSelector('[data-testid=matrix-heatmap]', { timeout: 20000 });
  const cellSel = `[data-testid="matrix-cell-${FACTOR_ID}-a_share"]`;
  await p.waitForSelector(cellSel, { timeout: 30000 });
  const cnStatus = await p.locator(cellSel).getAttribute('data-status');
  ok('矩阵 a_share 格 completed（批次实跑）', cnStatus === 'completed', `status=${cnStatus}`);
  const cellCount = await p.locator('[data-testid^=matrix-cell-]').count();
  ok('矩阵格数 ≥5（1 因子 × 5 市场）', cellCount >= 5, `count=${cellCount}`);

  // ── 显著性列（T-FB-18）：切 NW t ──────────────────────────────────
  await p.locator('select[aria-label="矩阵指标"]').selectOption('nw_t');
  await p.waitForTimeout(600);
  const nwText = (await p.locator(cellSel).innerText()).trim();
  ok('NW t 列渲染数值', /^-?\d+\.\d{2}$/.test(nwText), `cell="${nwText}"`);
  await p.screenshot({ path: '/tmp/backtest_probe_2_matrix_nwt.png' });

  // ── 点完成格开报告抽屉 ────────────────────────────────────────────
  await p.locator(cellSel).click();
  await p.waitForSelector('[data-testid=factor-report]', { timeout: 20000 });
  await p.waitForSelector('[data-testid=report-significance]', { timeout: 20000 });
  ok('报告抽屉打开（factor-report + report-significance）', true);
  ok('机构报告块：成本网格', (await p.locator('[data-testid=report-cost-grid]').count()) > 0);
  ok('导出 PDF 按钮在场', (await p.locator('[data-testid=export-report-pdf]').count()) > 0);

  // 超额页签：真实指数基准中文名（BENCH_LABELS）
  await p.locator('[data-testid=factor-report] button:has-text("超额")').first().click();
  await p.waitForTimeout(800);
  const drawerText = await p.locator('[data-testid=factor-report]').innerText();
  ok('超额页签：基准=沪深300 指数', drawerText.includes('沪深300 指数'), drawerText.slice(0, 200));
  ok('超额口径注明 QuantDB index_daily', drawerText.includes('QuantDB index_daily'));
  await p.screenshot({ path: '/tmp/backtest_probe_3_report_excess.png' });

  // Esc 关闭
  await p.keyboard.press('Escape');
  await p.waitForTimeout(600);
  ok('Esc 关闭报告抽屉', (await p.locator('[data-testid=factor-report]').count()) === 0);

  // ── 台账：批次行展开 → 批内单元「看报告」 ─────────────────────────
  await p.waitForSelector('[data-testid=run-ledger-batches]', { timeout: 20000 });
  const batchRow = p.locator(`[data-testid="batch-row-${BATCH_ID}"]`);
  ok('台账批次行在场（本批 fbb-679c…）', (await batchRow.count()) > 0);
  if ((await batchRow.count()) > 0) {
    await batchRow.first().click();
    await p.waitForTimeout(1000);
    const reportBtns = p.locator('button:has-text("看报告")');
    ok('批内单元行「看报告」≥1', (await reportBtns.count()) >= 1, `n=${await reportBtns.count()}`);
    await p.screenshot({ path: '/tmp/backtest_probe_4_ledger.png' });
  }
  ok('台账运行行区块在场', (await p.locator('[data-testid=run-ledger-runs]').count()) > 0);

  // ── console 错误（过滤已知良性） ──────────────────────────────────
  const benign = [/favicon/i, /ResizeObserver/i, /net::ERR_ABORTED.*\.map/i];
  const real = consoleErrors.filter((e) => !benign.some((r) => r.test(e)));
  ok('console/pageerror 零错误', real.length === 0, real.slice(0, 3).join(' || '));

  await b.close();
  console.log(fails.length === 0 ? 'PROBE ALL GREEN' : `PROBE FAILED: ${fails.join(', ')}`);
  process.exit(fails.length === 0 ? 0 : 1);
} catch (err) {
  console.log(`PROBE EXCEPTION: ${err.message}`);
  console.log(`console errors so far: ${consoleErrors.slice(0, 5).join(' || ') || '(none)'}`);
  await p.screenshot({ path: '/tmp/backtest_probe_exception.png' }).catch(() => {});
  await b.close();
  process.exit(2);
}
