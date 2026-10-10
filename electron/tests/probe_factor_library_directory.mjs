/**
 * T-MV-06 验收探针（2026-10-10）：设置页「挖掘方向」目录化两组。
 *
 * 断言「训练特征目录 + 因子值库目录」在活体前端落成：
 *  1) tab 名收敛为「挖掘方向」（旧动态名「L1 因子类别」退场）；
 *  2) 组一：训练特征目录（feature catalog 类别；后端目录不可达时才显示
 *     「内置参考方向」——两者必居其一，探针如实打印是哪一种）；
 *  3) 组二：因子值库——库项「L2 因子 · 因子值库」+ 事实副行现场匹配
 *     `CN · N 列 · YYYY-MM-DD ~ YYYY-MM-DD`（**不写死列数**：列数是磁盘
 *     事实，写死即漂移）；label 本身不带磁盘数字（存 localStorage 不失配）；
 *  4) 组三：标签/泄露库（每日特征）显式展示、带「已排除」徽标、**无复选框**；
 *  5) 勾选库方向 → 保存 → localStorage.selectedMiningDirections 落
 *     'L2 因子 · 因子值库' → 还原原值（探针不留痕）。
 *
 * 后端半边（目录 loader/校验/磁盘事实）由 backend/tests/test_factor_libraries.py
 * 金样钉死；API 形状由 /factor-categories 合并返回。
 *
 * 用法：node electron/tests/probe_factor_library_directory.mjs
 */
import { chromium } from 'playwright';

const BASE = process.env.QM_BASE || 'http://localhost:3080';
let pass = 0;
let fail = 0;
const ok = (cond, label, extra = '') => {
  if (cond) {
    pass++;
    console.log(`  ✓ ${label}${extra ? ` — ${extra}` : ''}`);
  } else {
    fail++;
    console.log(`  ✗ ${label}${extra ? ` — ${extra}` : ''}`);
  }
};

const browser = await chromium.launch({
  executablePath: '/usr/bin/google-chrome-stable',
  args: ['--no-sandbox'],
});
const ctx = await browser.newContext({ viewport: { width: 1720, height: 1150 } });
const page = await ctx.newPage();
const pageErrors = [];
page.on('pageerror', (e) => pageErrors.push(String(e).slice(0, 200)));

// ── 登录（本地开发实例）──
await page.goto(`${BASE}/#/auth/login`, { waitUntil: 'domcontentloaded', timeout: 40000 });
await page.waitForTimeout(3500);
if ((await page.locator('input[type=password]').count()) > 0) {
  await page.locator('input').nth(0).fill('admin');
  await page.locator('input[type=password]').first().fill('admin123');
  const btns = page.locator('button');
  for (let i = 0; i < (await btns.count()); i++) {
    const t = (await btns.nth(i).innerText().catch(() => '')).replace(/\s/g, '');
    if (/登录/.test(t)) {
      await btns.nth(i).click();
      break;
    }
  }
  await page.waitForTimeout(6000);
}
const dismissLater = async () => {
  const btn = page.locator('.ant-modal-wrap button:has-text("稍后再答")');
  if (await btn.count()) {
    await btn.first().click();
    await page.waitForTimeout(600);
  }
};
await dismissLater();

// ── 进 alpha-research → 设置 → 「挖掘方向」tab ──
await page.goto(`${BASE}/#/alpha-research`, { waitUntil: 'domcontentloaded', timeout: 40000 });
await page.waitForTimeout(5000);
await dismissLater();
await page.locator('text=因子池').first().click();
await page.waitForTimeout(2500);
await page.locator('span:text-is("设置")').first().click();
await page.waitForTimeout(3000);

const dirTab = page.locator('button:has-text("挖掘方向")').first();
ok(await dirTab.count(), 'tab 名收敛为「挖掘方向」');
if (await dirTab.count()) {
  await dirTab.click();
  await page.waitForTimeout(2500);
}

// ── 1) 组一：训练特征目录（或诚实回落的内置参考）──
const catalogHeader = page.locator('text=训练特征目录（L1 因子类别）').first();
const referenceHeader = page.locator('text=内置参考方向（后端目录不可达）').first();
const catalogCount = await catalogHeader.count();
const referenceCount = await referenceHeader.count();
ok(catalogCount + referenceCount > 0, '组一标题在（训练特征目录 / 内置参考 二者其一）',
  catalogCount ? '训练特征目录' : '内置参考（后端目录不可达）');

// ── 2) 组二：因子值库——库项 + 现场事实副行 ──
const libHeader = page.locator('text=因子值库（6_ml_datasets 宽表）').first();
ok(await libHeader.count(), '组二标题「因子值库（6_ml_datasets 宽表）」在');

const l2Row = page.locator('label:has-text("L2 因子 · 因子值库")').first();
ok(await l2Row.count(), '库项「L2 因子 · 因子值库」可勾选（label 无磁盘数字）');

const factRow = page
  .locator('span', { hasText: /CN · \d+ 列 · \d{4}-\d{2}-\d{2} ~ \d{4}-\d{2}-\d{2}/ })
  .first();
const factText = (await factRow.count()) ? await factRow.innerText() : '';
ok(
  /CN · \d+ 列 · \d{4}-\d{2}-\d{2} ~ \d{4}-\d{2}-\d{2}/.test(factText),
  '事实副行现场读取（CN · N 列 · 起 ~ 止）',
  factText.slice(0, 60),
);

// ── 3) 组三：标签/泄露库——显式展示且不可勾 ──
ok(
  (await page.locator('text=标签/泄露库（不可选）').count()) > 0,
  '组三标题「标签/泄露库（不可选）」在',
);
const excludedRow = page.locator('div:has-text("已排除")').filter({ hasText: '每日特征' }).last();
ok(await excludedRow.count(), '「每日特征（技术+估值）」行显式展示');
if (await excludedRow.count()) {
  ok((await excludedRow.locator('input[type=checkbox]').count()) === 0, '已排除库行内无复选框');
}

// ── 4) 勾选库方向 → 保存 → 落档 → 还原 ──
const storedDirs = () =>
  page.evaluate(() => {
    try {
      const c = JSON.parse(localStorage.getItem('quantaalpha_config') || '{}');
      return c.selectedMiningDirections;
    } catch {
      return 'parse-error';
    }
  });
const before = await storedDirs();

if (await l2Row.count()) {
  const cb = l2Row.locator('input[type=checkbox]');
  if (!(await cb.isChecked())) await cb.check(); // check() 幂等：已选中则不动
  await page.waitForTimeout(400);
  await page.locator('button:has-text("保存配置")').first().click();
  await page.waitForTimeout(1200);
  const after = await storedDirs();
  ok(
    Array.isArray(after) && after.includes('L2 因子 · 因子值库'),
    '勾选库方向 → 保存 → localStorage 落档',
    JSON.stringify(after)?.slice(0, 100),
  );
}

// 还原 localStorage 原值（探针不留痕；UI 状态随页面关闭丢弃）
await page.evaluate((orig) => {
  const c = JSON.parse(localStorage.getItem('quantaalpha_config') || '{}');
  if (orig === null || orig === undefined) delete c.selectedMiningDirections;
  else c.selectedMiningDirections = orig;
  localStorage.setItem('quantaalpha_config', JSON.stringify(c));
}, before ?? null);
const restored = await storedDirs();
ok(
  JSON.stringify(restored ?? null) === JSON.stringify(before ?? null),
  '探针还原 selectedMiningDirections 原值',
  JSON.stringify(restored)?.slice(0, 80),
);

ok(pageErrors.length === 0, '无页面 JS 错误', pageErrors.join(' | ').slice(0, 200));

await browser.close();
console.log(`\nRESULT ${fail === 0 ? 'ALL PASS' : `FAILURES: ${fail}`} (pass=${pass} fail=${fail})`);
process.exit(fail === 0 ? 0 : 1);
