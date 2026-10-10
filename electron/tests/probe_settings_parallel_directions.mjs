/**
 * T-MV-04 验收探针（2026-10-10）：设置页并行方向数文案与落档。
 *
 * 断言「设置页 N 方向 → 实际派发 N 任务」的前端半边（派发语义由 API 探针
 * probe_tmv04 验证）：
 *  1) 「实验默认参数」tab 的「并行方向数」：诚实文案（类别方向生效/自由文本恒 1
 *     条/超限自动排队），且**不再**标「后端暂未生效」；
 *  2) 「启用并行执行」文案指向真实机制（并行方向数驱动），自身仍如实标未接入；
 *  3) 改 N=3 → localStorage quantaalpha_config.defaultNumDirections 落档（提交时
 *     经 defaults 读回），再还原为 2 不污染后续手动验收。
 *
 * 用法：node electron/tests/probe_settings_parallel_directions.mjs
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

// ── 进 alpha-research → 设置 → 「实验默认参数」tab ──
await page.goto(`${BASE}/#/alpha-research`, { waitUntil: 'domcontentloaded', timeout: 40000 });
await page.waitForTimeout(5000);
await dismissLater();
await page.locator('text=因子池').first().click();
await page.waitForTimeout(2500);
await page.locator('span:text-is("设置")').first().click();
await page.waitForTimeout(3000);

// 注意：不得用 button:text-is()——按钮内含 span 时匹配到的是最小文本元素而非按钮
const paramsTab = page.locator('button:has-text("默认参数")').first();
ok(await paramsTab.count(), '存在「默认参数」tab');
if (await paramsTab.count()) {
  await paramsTab.click();
  await page.waitForTimeout(1200);
}

// ── 1) 并行方向数：诚实文案 + 无「后端暂未生效」 ──
const dirLabel = page.locator('label:text-is("并行方向数")').first();
ok(await dirLabel.count(), '设置页存在「并行方向数」');
const dirBlock = dirLabel.locator('xpath=..');
const dirCopy = (await dirBlock.locator('p').first().innerText()).replace(/\s/g, '');
ok(dirCopy.includes('类别方向生效'), '文案说明类别方向生效', dirCopy.slice(0, 60));
ok(dirCopy.includes('自由文本') && dirCopy.includes('恒为1条'), '文案说明自由文本恒 1 条');
ok(dirCopy.includes('自动排队'), '文案说明超限自动排队');
ok(!dirCopy.includes('后端暂未生效'), '不再标「后端暂未生效」');

const numInput = dirBlock.locator('input[type=number]').first();
ok((await numInput.inputValue()) === '2', '默认值 2');

// ── 2) 启用并行执行：指向真实机制 ──
const parLabel = page.locator('label:has-text("启用并行执行")').first();
ok(await parLabel.count(), '存在「启用并行执行」开关');
const parCopy = (await parLabel.locator('.text-xs').first().innerText()).replace(/\s/g, '');
ok(parCopy.includes('并行方向数'), '文案指向「并行方向数」这条真实机制', parCopy.slice(0, 60));
ok(parCopy.includes('未接入后端'), '自身如实标注未接入后端');

// ── 3) 改 N=3 → 「保存配置」→ localStorage 落档（提交路径经 defaults 读回）→ 还原 ──
const readStored = () =>
  page.evaluate(() => {
    try {
      return JSON.parse(localStorage.getItem('quantaalpha_config') || '{}')
        .defaultNumDirections;
    } catch {
      return 'parse-error';
    }
  });
const saveBtn = page.locator('button:has-text("保存配置")').first();
const initialVal = await numInput.inputValue();

await numInput.fill('3');
await numInput.dispatchEvent('change');
await saveBtn.click();
await page.waitForTimeout(1200);
ok((await readStored()) === 3, 'defaultNumDirections=3 已落 localStorage', String(await readStored()));

await numInput.fill(initialVal);
await numInput.dispatchEvent('change');
await saveBtn.click();
await page.waitForTimeout(1200);
ok(
  (await readStored()) === Number(initialVal),
  `还原为 ${initialVal}（探针不留痕）`,
  String(await readStored()),
);

ok(pageErrors.length === 0, '无页面 JS 错误', pageErrors.join(' | ').slice(0, 200));

await browser.close();
console.log(`\nRESULT ${fail === 0 ? 'ALL PASS' : `FAILURES: ${fail}`} (pass=${pass} fail=${fail})`);
process.exit(fail === 0 ? 0 : 1);
