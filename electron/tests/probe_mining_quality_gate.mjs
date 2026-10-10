/**
 * T-MV-05 验收探针（2026-10-10）：设置页入库闸门开关的文案与落档。
 *
 * 断言「设置页开关 → 派发时携带 off」的前端半边（query/body 形态由 vitest
 * 钉死；后端判定由 test_pool_service.TestAdmissionGate 真库集成验证）：
 *  1) 「启用质量门控」文案 = 诚实口径（入库闸门/默认软闸留痕），**不再**标
 *     「后端暂未生效」；
 *  2) 默认开（checked）；关闭 → 「保存配置」→ localStorage
 *     quantaalpha_config.qualityGateEnabled=false 落档（派发路径经
 *     getStoredQualityGateEnabled 读回），再还原默认不污染后续手动验收。
 *
 * 用法：node electron/tests/probe_mining_quality_gate.mjs
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

// ── 进 alpha-research → 设置 → 「默认参数」tab（高级控制在参数 tab 里）──
await page.goto(`${BASE}/#/alpha-research`, { waitUntil: 'domcontentloaded', timeout: 40000 });
await page.waitForTimeout(5000);
await dismissLater();
await page.locator('text=因子池').first().click();
await page.waitForTimeout(2500);
await page.locator('span:text-is("设置")').first().click();
await page.waitForTimeout(3000);

const paramsTab = page.locator('button:has-text("默认参数")').first();
ok(await paramsTab.count(), '存在「默认参数」tab');
if (await paramsTab.count()) {
  await paramsTab.click();
  await page.waitForTimeout(1200);
}

// ── 1) 「启用质量门控」：诚实文案 + 无「后端暂未生效」 ──
const gateLabel = page.locator('label:has-text("启用质量门控")').first();
ok(await gateLabel.count(), '存在「启用质量门控」开关');
const gateCopy = (await gateLabel.locator('.text-xs').first().innerText()).replace(/\s/g, '');
ok(gateCopy.includes('入库闸门'), '文案说明控制的是入库闸门', gateCopy.slice(0, 80));
ok(gateCopy.includes('软闸') && gateCopy.includes('留痕'), '文案说明默认软闸并留痕');
ok(gateCopy.includes('硬闸由运维配置'), '文案说明硬闸归运维配置');
ok(!gateCopy.includes('后端暂未生效'), '不再标「后端暂未生效」');

const gateCheckbox = gateLabel.locator('input[type=checkbox]').first();
ok(await gateCheckbox.isChecked(), '默认开（选中）');

// ── 2) 关闭 → 保存 → localStorage 落档 false → 还原开 ──
const readStored = () =>
  page.evaluate(() => {
    try {
      return JSON.parse(localStorage.getItem('quantaalpha_config') || '{}')
        .qualityGateEnabled;
    } catch {
      return 'parse-error';
    }
  });
const saveBtn = page.locator('button:has-text("保存配置")').first();

await gateCheckbox.uncheck();
await saveBtn.click();
await page.waitForTimeout(1200);
ok(
  (await readStored()) === false,
  'qualityGateEnabled=false 已落 localStorage',
  String(await readStored()),
);

await gateCheckbox.check();
await saveBtn.click();
await page.waitForTimeout(1200);
ok(
  (await readStored()) === true,
  '还原为 true（探针不留痕）',
  String(await readStored()),
);

ok(pageErrors.length === 0, '无页面 JS 错误', pageErrors.join(' | ').slice(0, 200));

await browser.close();
console.log(`\nRESULT ${fail === 0 ? 'ALL PASS' : `FAILURES: ${fail}`} (pass=${pass} fail=${fail})`);
process.exit(fail === 0 ? 0 : 1);
