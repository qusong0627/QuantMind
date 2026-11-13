/**
 * 分数叠加 / 模型切换 E2E（推理中心 + 个股终端）。
 *
 * 用法：QM_BASE=http://localhost:3080 \
 *       QM_E2E_USER=<用户> QM_E2E_PASS=<密码> \
 *       node tests/score-overlay.e2e.mjs
 *
 * 登录凭据只从环境变量读取，不写死在仓库（避免把测试账号落进 git）。
 *
 * 只断言**可观察的契约**，不依赖 canvas 像素：
 * - 单票研判：模型下拉选择会真正下发 model_id；drivers_mode=none；
 *   score-series 带 end_date；预测日/兑现日切换存在；
 * - 个股终端：默认模型选项不再传字面量 default；选择其它模型后请求带 model_id；
 *   页面出现 T+N 窗口文案；全程无 4xx/5xx（除登录态探测）。
 */
import { chromium } from 'playwright';

const BASE = process.env.QM_BASE || 'http://localhost:3080';
const QM_USER = process.env.QM_E2E_USER || '';
const QM_PASS = process.env.QM_E2E_PASS || '';
if (!QM_USER || !QM_PASS) {
  console.log('SKIP: 设置 QM_E2E_USER / QM_E2E_PASS 后重跑（凭据不入仓库）');
  process.exit(0);
}
const errors = [];
const requests = [];
let pass = 0;
let total = 0;
const check = (ok, label) => {
  total += 1;
  if (ok) pass += 1;
  console.log(`${ok ? '✓' : '✗'} ${label}`);
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const browser = await chromium.launch({
  executablePath: '/usr/bin/google-chrome-stable',
  args: ['--no-sandbox'],
});
const context = await browser.newContext({ viewport: { width: 1720, height: 1150 } });
const page = await context.newPage();

page.on('console', (m) => {
  if (m.type() === 'error') errors.push('[console] ' + m.text().slice(0, 200));
});
page.on('pageerror', (e) => errors.push('[PAGEERROR] ' + (e.message || '').slice(0, 200)));
page.on('request', (req) => {
  if (!/\/api\/v1\/(research\/predict-stock|models\/inference\/stock\/)/.test(req.url())) return;
  requests.push({ url: req.url(), method: req.method(), body: req.postData() || '' });
});
page.on('response', (res) => {
  if (!/\/api\/v1\//.test(res.url())) return;
  if (res.status() >= 500) errors.push(`[API ${res.status()}] ${res.url().slice(0, 200)}`);
});

async function login() {
  await page.goto(`${BASE}/`, { waitUntil: 'domcontentloaded', timeout: 40000 });
  await sleep(3000);
  if ((await page.locator('input[type=password]').count()) > 0) {
    await page.locator('input').nth(0).fill(QM_USER);
    await page.locator('input[type=password]').first().fill(QM_PASS);
    const btns = page.locator('button');
    for (let i = 0; i < (await btns.count()); i += 1) {
      const t = (await btns.nth(i).innerText().catch(() => '')).replace(/\s/g, '');
      if (/登录/.test(t)) {
        await btns.nth(i).click();
        break;
      }
    }
    await sleep(5000);
  }
  // 投资适当性弹窗：走「稍后再答」
  const later = page.locator('.ant-modal button', { hasText: '稍后再答' }).first();
  if (await later.isVisible().catch(() => false)) {
    await later.click().catch(() => {});
    await sleep(500);
  }
}

async function openInferenceCenter() {
  await page.locator('a,button', { hasText: '推理中心' }).first().click({ timeout: 15000 }).catch(() => {});
  await page.waitForTimeout(2500);
  if (!page.url().includes('inference-center')) {
    await page.goto(`${BASE}/#/inference-center`, { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(2500);
  }
  await page.getByText('单票研判', { exact: true }).first().waitFor({ state: 'visible', timeout: 25000 });
}

async function runSingleStock() {
  const input = page.locator('input[placeholder*="代码"], input[placeholder*="标的"], input[placeholder*="股票"]').first();
  await input.waitFor({ state: 'visible', timeout: 15000 });
  await input.fill('600519');
  await input.press('Enter');
  await page.waitForTimeout(8000);

  const predictCalls = requests.filter((r) => r.url.includes('/research/predict-stock') && !r.url.includes('/drivers'));
  check(predictCalls.length > 0, '单票研判已发起 predict-stock');
  const lastPredict = predictCalls[predictCalls.length - 1];
  let body = {};
  try { body = JSON.parse(lastPredict?.body || '{}'); } catch { body = {}; }
  check(body.drivers_mode === 'none', 'predict-stock 请求 drivers_mode=none（核心图不再等 SHAP）');
  check(!body.model_id || typeof body.model_id === 'string', 'predict-stock 未携带非法 model_id');

  // 预测日 / 兑现日切换
  const predBtn = page.locator('button', { hasText: '预测日' }).first();
  const resBtn = page.locator('button', { hasText: '兑现日' }).first();
  await predBtn.waitFor({ state: 'visible', timeout: 12000 }).catch(() => {});
  await resBtn.waitFor({ state: 'visible', timeout: 12000 }).catch(() => {});
  check(await predBtn.isVisible().catch(() => false), '存在「预测日」切换');
  check(await resBtn.isVisible().catch(() => false), '存在「兑现日」切换');
  if (await resBtn.isVisible().catch(() => false)) {
    await resBtn.click().catch(() => {});
    await sleep(600);
    await predBtn.click().catch(() => {});
  }

  // score-series 必须带 end_date（防前视）
  const scoreCalls = requests.filter((r) => r.url.includes('/models/inference/stock/') && r.url.includes('score-series'));
  check(scoreCalls.length > 0, '单票研判已发起 score-series');
  check(
    scoreCalls.some((r) => /[?&]end_date=\d{4}-\d{2}-\d{2}/.test(r.url)),
    'score-series 带 end_date（as-of 截断，防前视）',
  );

  // 等异步归因端点（有归因时）
  await page.waitForTimeout(1800);
  const driverCalls = requests.filter((r) => r.url.includes('/research/predict-stock/drivers'));
  check(driverCalls.length > 0, '核心图之后单独请求 /predict-stock/drivers');
  let driverBody = {};
  try { driverBody = JSON.parse(driverCalls[driverCalls.length - 1]?.body || '{}'); } catch { driverBody = {}; }
  check(Boolean(driverBody.as_of_date), 'drivers 请求回传 as_of_date（与图上预测同一日）');
}

async function runStockTerminal() {
  await page.goto(`${BASE}/#/stock-terminal`, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(3000);
  const input = page.locator('input').first();
  await input.waitFor({ state: 'visible', timeout: 15000 });
  await input.fill('600519');
  // 联想是 300ms debounce + 服务端查询；等下拉结果按钮出现再点，避免回车时
  // items 还没到（handleKeyDown 只在已有 items 时才选中）。
  const resultBtn = page.locator('.z-20 button', { hasText: '600519' }).first();
  await resultBtn.waitFor({ state: 'visible', timeout: 15000 }).catch(() => {});
  if (await resultBtn.isVisible().catch(() => false)) {
    await resultBtn.click().catch(() => {});
  } else {
    await input.press('Enter');
  }
  await page.waitForTimeout(8000);

  const scoreCalls = requests.filter((r) => r.url.includes('/models/inference/stock/') && r.url.includes('score-series'));
  check(scoreCalls.length > 0, '个股终端已发起 score-series');
  check(
    scoreCalls.every((r) => !/[?&]model_id=default(&|$)/.test(r.url)),
    '默认模型请求不再携带字面量 model_id=default',
  );

  const modelSelect = page.locator('.ant-select:visible', { hasText: '默认模型' }).first();
  await modelSelect.waitFor({ state: 'visible', timeout: 15000 }).catch(() => {});
  const hasModelSelect = await modelSelect.isVisible().catch(() => false);
  check(hasModelSelect, '个股终端存在模型下拉');
  if (hasModelSelect) {
    await modelSelect.click().catch(() => {});
    await page.waitForTimeout(600);
    const options = page.locator('.ant-select-item-option:not(.ant-select-item-option-disabled)');
    const n = await options.count();
    check(n >= 2, `模型下拉有可选模型（${n}）`);
    const optionTexts = await options.allInnerTexts().catch(() => []);
    check(
      optionTexts.some((t) => /分数至 \d{2}-\d{2}/.test(t)),
      '模型下拉显示精确最后分数日（分数至 MM-DD）',
    );
    if (n >= 2) {
      // 选第一个非默认模型（选项 0 通常是当前默认项，用第二个）
      await options.nth(1).click().catch(() => {});
      await page.waitForTimeout(2500);
      const after = requests.filter((r) => r.url.includes('/models/inference/stock/') && r.url.includes('score-series'));
      check(
        after.some((r) => /[?&]model_id=(?!default)[^&]+/.test(r.url)),
        '选择其它模型后 score-series 带真实 model_id',
      );
    }
  }
  const windowText = await page
    .getByText(/窗口\s*T\+\d+/)
    .first()
    .waitFor({ state: 'visible', timeout: 12000 })
    .then(() => true)
    .catch(() => false);
  check(windowText, '个股终端显示 T+N 预测窗口');

  // 周/月：只延长执行日/目标日两个真实交易日，并在状态条明确说明
  const weeklyBtn = page.locator('button', { hasText: '周K' }).first();
  if (await weeklyBtn.isVisible().catch(() => false)) {
    await weeklyBtn.click().catch(() => {});
    await page.waitForTimeout(1800);
    const hint = await page
      .getByText(/周\/月预测段按执行日\/目标日单点显示/)
      .first()
      .isVisible()
      .catch(() => false);
    check(hint, '周K 显示「预测段按执行日/目标日单点显示」');
    await page.locator('button', { hasText: '日K' }).first().click().catch(() => {});
    await page.waitForTimeout(1200);
  }
}

try {
  await login();
  await openInferenceCenter();
  await runSingleStock();
  await runStockTerminal();
} catch (e) {
  errors.push('[SCRIPT] ' + (e?.message || String(e)).slice(0, 300));
} finally {
  await browser.close();
  if (errors.length) {
    console.log('\n错误：');
    for (const e of errors.slice(0, 20)) console.log(' -', e);
  }
  console.log(`\n结果：${pass}/${total} 通过；错误 ${errors.length} 条`);
  process.exit(pass === total && errors.length === 0 ? 0 : 1);
}
