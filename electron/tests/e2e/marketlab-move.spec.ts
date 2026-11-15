/**
 * E2E：行情回测已迁到「回测中心」，且排在「快速回测」之前。
 *
 * 锁定三件事（2026-11-15 用户口径）：
 *  1. 回测中心左栏有「行情回测」；
 *  2. 它的位置在「快速回测」之前；
 *  3. 点开不是白屏：页头 / 三个子页签 / 工作台三栏都真实渲染。
 *
 * 依赖：本机 dev 前端（http://localhost:5173）+ 后端网关（8000）+ 默认管理员账号。
 * 运行：cd electron && npx playwright test tests/e2e/marketlab-move.spec.ts
 */
import { test, expect, type Page } from '@playwright/test';

// 本机没有 Playwright 自带 chromium 时用系统 Chrome（仓库既有探针同款）
test.use({
  launchOptions: {
    executablePath: '/usr/bin/google-chrome-stable',
    args: ['--no-sandbox'],
  },
  viewport: { width: 1680, height: 980 },
});

/** 首启的「投资适当性评估」等 antd 弹窗会盖住侧栏，点任何东西之前先让路。
 *  一律短超时 + force：弹窗在关闭动画里不该拖住整条用例。 */
async function dismissModals(page: Page) {
  for (const name of ['稍后再答', '知道了', '我知道了']) {
    const btn = page.getByRole('button', { name }).first();
    if (await btn.isVisible().catch(() => false)) {
      await btn.click({ timeout: 3000, force: true }).catch(() => {});
      await page.waitForTimeout(300);
    }
  }
  const close = page.locator('.ant-modal-close').first();
  if (await close.isVisible().catch(() => false)) {
    await close.click({ timeout: 3000, force: true }).catch(() => {});
    await page.waitForTimeout(300);
  }
}

async function login(page: Page) {
  await page.goto('/#/auth/login', { waitUntil: 'domcontentloaded' });
  const username = page.locator('input[placeholder="用户名或手机号"]');
  const password = page.locator('input[placeholder="密码"]');
  await expect(username).toBeVisible({ timeout: 20_000 });
  await username.fill('admin');
  await password.fill('admin123');
  await page.getByRole('button', { name: '立即登录' }).click();
  // 登录成功会离开登录页（底部导航出现）
  await expect(page.locator('button[data-nav-id="backtest"]')).toBeVisible({ timeout: 25_000 });
  await dismissModals(page);
}

test('行情回测在回测中心左栏、排在快速回测之前、点开可渲染', async ({ page }) => {
  test.setTimeout(90_000);
  const errors: string[] = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  page.on('console', (m) => {
    if (m.type() === 'error') errors.push(m.text());
  });
  // 临时诊断：行情实验室的网络面（验证完删除）


  await login(page);

  // 进回测中心
  await page.locator('button[data-nav-id="backtest"]').click();
  const moduleButtons = page.locator('aside button[data-module-id]');
  await expect(moduleButtons.first()).toBeVisible({ timeout: 20_000 });

  const ids = await moduleButtons.evaluateAll((els) =>
    els.map((el) => el.getAttribute('data-module-id')),
  );
  const idxLab = ids.indexOf('market-lab');
  const idxQuick = ids.indexOf('quick-backtest');
  expect(idxLab, `左栏模块顺序：${ids.join(' / ')}`).toBeGreaterThanOrEqual(0);
  expect(idxQuick).toBeGreaterThanOrEqual(0);
  expect(idxLab, '行情回测必须排在快速回测之前').toBeLessThan(idxQuick);

  // 点开模块
  await dismissModals(page);
  await page.locator('aside button[data-module-id="market-lab"]').click();
  await expect(page.locator('.market-lab-root .lab-page')).toBeVisible({ timeout: 20_000 });

  // 页头 + 三个子页签
  await expect(page.locator('.market-lab-root .lab-title h1')).toHaveText('行情回测');
  await expect(page.locator('.market-lab-root .lab-tabs button')).toHaveCount(3);

  // 工作台三栏（左策略 / 中K线结果 / 右运行与备注）
  await expect(page.locator('.market-lab-root .lab-rail')).toBeVisible();
  await expect(page.locator('.market-lab-root .lab-center')).toBeVisible();
  await expect(page.locator('.market-lab-root .lab-panel')).toBeVisible();

  // 数据面：内置模板列表经 /market-lab/strategies 返回并渲染（不是空壳）
  await expect(page.locator('.market-lab-root .lab-tpl-list li').first()).toBeVisible({ timeout: 20_000 });
  // K 线加载完成（quantdb 未挂载时这条会失败——属真实环境缺口，不掩盖）
  await expect(page.locator('.market-lab-root .lab-hint')).toContainText(
    /quantdb 日线 · [1-9]\d* 根/,
    { timeout: 20_000 },
  );

  // 周期：A股默认日线，可切 5分钟（真实分钟库）
  await expect(page.locator('.market-lab-root .lab-freq .seg button')).toHaveCount(3);
  await page.locator('.market-lab-root .lab-freq .seg button', { hasText: '5分钟' }).click();
  await expect(page.locator('.market-lab-root .lab-hint')).toContainText(
    /quantdb 5分钟 · [1-9]\d* 根/,
    { timeout: 20_000 },
  );
  // 真正的分钟数据判据：加载结束 + 区间带时分（只看周期标签会被旧日线骗过）
  await expect(page.locator('.market-lab-root .lab-mask')).toHaveCount(0, { timeout: 20_000 });
  await expect(page.locator('.market-lab-root .lab-hint')).toContainText(
    /\d{4}-\d{2}-\d{2} \d{2}:\d{2}(?::\d{2})? ~ \d{4}-\d{2}-\d{2} \d{2}:\d{2}(?::\d{2})?/,
    { timeout: 20_000 },
  );
  await page.screenshot({ path: 'test-results/marketlab-minute.png' });
  await page.locator('.market-lab-root .lab-freq .seg button', { hasText: '日线' }).click();
  await expect(page.locator('.market-lab-root .lab-hint')).toContainText(
    /quantdb 日线 · [1-9]\d* 根/,
    { timeout: 20_000 },
  );
  await expect(page.locator('.market-lab-root .lab-hint')).toContainText(
    /\d{4}-\d{2}-\d{2} ~ \d{4}-\d{2}-\d{2}/,
    { timeout: 20_000 },
  );

  // 合规：展示面不出现「买入/卖出」字眼（策略标题里的语料词已在显示层改写为入场/离场）
  const bodyText = await page.locator('.market-lab-root').innerText();
  expect(bodyText, `展示面仍有买卖字眼：${bodyText.match(/.{0,12}(买入|卖出).{0,12}/)?.[0] ?? ''}`)
    .not.toMatch(/买入|卖出/);

  // 新增 Pine 策略：入口存在且弹窗能开（不实际提交，避免污染真实策略库）
  await page.locator('.market-lab-root .lab-new').click();
  await expect(page.locator('.market-lab-root .lab-modal-title')).toContainText('新增 Pine 策略');
  await page.screenshot({ path: 'test-results/marketlab-new-pine.png' });
  await page.locator('.market-lab-root .lab-modal-x').click();
  await expect(page.locator('.market-lab-root .lab-modal')).toHaveCount(0);

  // 市场联动：右上角切港股 → 标的/数据源/复权档一起切
  await page.getByRole('radio', { name: '港股' }).click();
  await expect(page.locator('.market-lab-root .lab-sub')).toContainText('港股');
  await expect(page.locator('.market-lab-root .lab-symbol .code')).toHaveText('0700.HK', {
    timeout: 20_000,
  });
  await expect(page.locator('.market-lab-root .lab-hint')).toContainText(
    /QuantHK 日线 · [1-9]\d* 根/,
    { timeout: 20_000 },
  );
  // 真正的数据切换判据：名称与加载态（只看市场标签会被旧 bars 骗过）
  await expect(page.locator('.market-lab-root .lab-symbol strong')).toHaveText('腾讯控股', {
    timeout: 20_000,
  });
  await expect(page.locator('.market-lab-root .lab-mask')).toHaveCount(0);
  // 港股有 不复权/后复权 两档；美股/期货/加密只有一档
  await expect(page.locator('.market-lab-root .lab-adj .seg button')).toHaveCount(2);
  // 周期：三档都列出来；港股无分钟库 → 5分钟标 unavailable，点击自动回日线
  await expect(page.locator('.market-lab-root .lab-freq .seg button')).toHaveCount(3);
  const hk5 = page.locator('.market-lab-root .lab-freq .seg button', { hasText: '5分钟' });
  await expect(hk5).toHaveClass(/unavailable/);
  await hk5.click();
  await expect(page.locator('.market-lab-root .lab-freq .seg button.on')).toHaveText('日线');
  await expect(page.locator('.market-lab-root .lab-warn')).toContainText('暂无');
  await page.screenshot({ path: 'test-results/marketlab-hk-freq-fallback.png' });
  await page.screenshot({ path: 'test-results/marketlab-hk.png' });

  // 切回 A 股继续跑模板回测
  await page.getByRole('radio', { name: 'A股' }).click();
  await expect(page.locator('.market-lab-root .lab-symbol .code')).toHaveText('600309.SH', {
    timeout: 20_000,
  });
  await expect(page.locator('.market-lab-root .lab-adj .seg button')).toHaveCount(3);

  // 批量回测入口：池选择（含自定义）+ 策略多选 + 发起按钮
  await page.locator('.market-lab-root .lab-tabs button', { hasText: '策略排行' }).click();
  await expect(page.locator('.market-lab-root .lab-batch-start')).toBeVisible();
  await expect(page.locator('.market-lab-root .lab-batch-form select')).toBeVisible();
  await expect(page.locator('.market-lab-root .lab-batch-check').first()).toBeVisible();
  await expect(page.locator('.market-lab-root .lab-batch-submit')).toBeVisible();
  const poolSelect = page.locator('.market-lab-root .lab-batch-form select');
  // 有批次在跑时表单会禁用（单批次闸门）——此时只验入口，不强改选项
  if (!(await poolSelect.isDisabled())) {
    await poolSelect.selectOption('custom');
    await expect(page.locator('.market-lab-root .lab-batch-codes')).toBeVisible();
  }
  // 批次产物：排行表从 /batch/{run_id} 真实渲染（最新批次默认选中）
  await expect(page.locator('.market-lab-root .lab-batch-bar')).toBeVisible({ timeout: 20_000 });
  await expect(page.locator('.market-lab-root .lab-rank tbody tr').first()).toBeVisible({
    timeout: 20_000,
  });
  await page.screenshot({ path: 'test-results/marketlab-batch-start.png' });
  // 回到工作台，继续跑单标的模板回测
  await page.locator('.market-lab-root .lab-tabs button', { hasText: '工作台' }).click();

  // 跑一次内置模板回测：要么出结果区，要么出**明确的错误横幅**（不允许静默无反应）。
  // 本机未装 PyneCore 时应看到「PyneCore 运行时未安装」——这正是 2026-11-15 修掉的
  // 吞错路径（旧实现把 success:false 当成 undefined 数据，按钮闪回原状、页面毫无提示）。
  const runBtn = page.locator('.market-lab-root .lab-panel .lab-run', { hasText: '运行回测' });
  await expect(runBtn).toBeVisible();
  await runBtn.click();

  const resultTitle = page.locator('.market-lab-root .lab-block-title', { hasText: '结果 ·' });
  const errorBanner = page.locator('.market-lab-root .lab-err');
  await expect(resultTitle.or(errorBanner).first()).toBeVisible({ timeout: 60_000 });

  if (await resultTitle.count()) {
    // 运行时可用：结果区 + 净值曲线（我们新写的 EquityLineChart）+ 逐笔表都要真的出来
    await expect(page.locator('.market-lab-root .lab-equity-box canvas').first()).toBeVisible({
      timeout: 20_000,
    });
    await expect(page.locator('.market-lab-root .lab-trades')).toBeVisible();
  } else {
    // 运行时缺失：错误必须落到界面上，且文案来自后端
    await expect(errorBanner).toContainText('回测失败');
  }

  // 面包屑也应显示模块名（确认走的是同一个 activeModule）
  await expect(page.locator('text=回测中心').first()).toBeVisible();

  // 页面级异常（接口 4xx/5xx 不算：数据面可能没跑；但 JS 崩溃必须是 0）
  const pageErrors = errors.filter((e) => !/Failed to load resource|Network Error|AxiosError/.test(e));
  expect(pageErrors, `页面异常：\n${pageErrors.join('\n')}`).toEqual([]);

  await page.screenshot({ path: 'test-results/marketlab-backtest-center.png', fullPage: false });
});

test('回测中心内容区变窄时，工作台按容器宽度塌成单栏', async ({ page }) => {
  test.setTimeout(90_000);
  await login(page);

  await page.locator('button[data-nav-id="backtest"]').click();
  await dismissModals(page);
  await page.locator('aside button[data-module-id="market-lab"]').click();
  const grid = page.locator('.market-lab-root .lab-wb');
  await expect(grid).toBeVisible({ timeout: 20_000 });

  // 1100px 视口 → 去掉 240px 侧栏后内容区 ≈ 820px，触发 @container (max-width: 980px)
  await page.setViewportSize({ width: 1100, height: 800 });
  await expect
    .poll(
      async () =>
        grid.evaluate(
          (el) => getComputedStyle(el).gridTemplateColumns.split(' ').filter(Boolean).length,
        ),
      { timeout: 10_000 },
    )
    .toBe(1);

  await page.screenshot({ path: 'test-results/marketlab-narrow.png', fullPage: false });
});
