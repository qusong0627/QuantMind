/**
 * T-MV-02 验收探针（2026-10-10）：类别供给面 + 方向历史模式徽章。
 *
 * 断言的是「用户实际看到的」两面：
 *  1) 设置页「挖掘方向」tab → 池内供给面面板：真实池数据（A股·csi300 共 191 个）
 *     渲染类名/计数/均值·中位 IC/饱和度；最满类（动量与趋势）饱和 = 100%。
 *  2) 挖掘历史页：类别选择路径的行带模式徽章（random → 「随机抽取」）；
 *     自由文本路径的行**不得**出现任何模式徽章（mode 列是事实，不是参数回声）。
 *
 * 依赖前置（探针前置数据由 API 探针写入，见本次交付记录）：
 *  - 一条 direction_mode=random 的任务（方向=动量类因子 (38)，无 direction_meta证据——
 *    T-MV-02 时代落档，早于抽样证据列）
 *  - 一条 direction_mode=random + direction_meta 齐全的任务（方向=基础行情类因子 (6)，
 *    T-MV-03 加权抽样落档）；其徽章工具提示必须含加权口径/seed/候选次数，不得标「均匀兜底」
 *  - 一条自由文本方向的任务（无 directions → mode NULL）
 * 均已 cancel，历史页应显示「已取消」。
 *
 * 用法：node electron/tests/probe_supply_face_direction_history.mjs
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

// ── 进 alpha-research → 因子池（Layout 侧栏出现）→ 设置 ──
await page.goto(`${BASE}/#/alpha-research`, { waitUntil: 'domcontentloaded', timeout: 40000 });
await page.waitForTimeout(5000);
await dismissLater();
await page.locator('text=因子池').first().click();
await page.waitForTimeout(3000);
await page.locator('span:text-is("设置")').first().click();
await page.waitForTimeout(3000);

// ── 1) 设置页：切到「挖掘方向」tab → 池内供给面面板 ──（tab 名随 T-MV-06 目录化改版）
const l1Tab = page.locator('button:has-text("挖掘方向")').first();
if (await l1Tab.count()) {
  await l1Tab.click();
  await page.waitForTimeout(1500);
}
await page.waitForSelector('text=池内供给面', { timeout: 20000 });
ok(true, '设置页出现「池内供给面」面板');

ok(
  (await page.locator('text=与上方方向词表非一一对应').count()) > 0,
  '面板带诚实注记（与方向词表非一一对应）',
);
ok((await page.locator('text=A股 · csi300').count()) > 0, '口径标签 A股 · csi300');

// 真实池数据断言（当前池：动量与趋势 47 / 共 191）
const hasMomentum = (await page.locator('text=动量与趋势').count()) > 0;
ok(hasMomentum, '渲染真实大类「动量与趋势」');
ok((await page.locator('text=47 个').count()) > 0, '类别计数 47 个');
ok((await page.locator('text=100%').count()) > 0, '最满类饱和度 100%');
ok((await page.locator('text=66%').count()) > 0, '次满类饱和度 66%（31/47）');
const totalLine = page.locator('text=/共 191 个在池因子/');
ok((await totalLine.count()) > 0, '总计行「共 191 个在池因子」');

// 均值/中位 IC 有样本渲染（不出现整列「—」即可：抽查一行含 IC 数字）
const icDigits = await page
  .locator('span')
  .filter({ hasText: /均值 IC -?0\.\d{4}/ })
  .count();
ok(icDigits > 0, '均值 IC 以四位小数渲染', `rows=${icDigits}`);

await page.screenshot({ path: '/tmp/supply_face_panel.png' });

// ── 2) 挖掘历史页：模式徽章 ──
await page.locator('span:text-is("挖掘历史")').first().click();
await page.waitForSelector('table', { timeout: 20000 });
await page.waitForTimeout(1500);

const randomRow = page.locator('tr').filter({ hasText: '动量类因子 (38)' }).first();
ok((await randomRow.count()) > 0, '历史行存在（random 探针方向）');
if (await randomRow.count()) {
  const badge = randomRow.locator('span:text-is("随机抽取")');
  ok((await badge.count()) > 0, '该行带「随机抽取」徽章');
  ok(
    (await randomRow.locator('span:text-is("类别选定")').count()) === 0,
    '该行不带「类别选定」徽章',
  );
  // 无抽样证据的旧行（T-MV-02 落档，meta=NULL）：工具提示只有基础句，不编造证据
  const legacyTitle = (await badge.first().getAttribute('title')) || '';
  ok(
    legacyTitle.includes('方向如何被选中') && !legacyTitle.includes('seed='),
    '无 meta 行工具提示不编造抽样证据',
    legacyTitle.slice(0, 60),
  );
}

// T-MV-03：带完整抽样证据的行 → 工具提示呈现加权口径/seed/候选次数
const metaRow = page.locator('tr').filter({ hasText: '基础行情类因子 (6)' }).first();
ok((await metaRow.count()) > 0, '历史行存在（T-MV-03 加权抽样方向）');
if (await metaRow.count()) {
  const metaBadge = metaRow.locator('span:text-is("随机抽取")');
  ok((await metaBadge.count()) > 0, '该行带「随机抽取」徽章');
  const title = (await metaBadge.first().getAttribute('title')) || '';
  ok(title.includes('按空白度加权'), '工具提示标注加权口径', title.slice(0, 80));
  ok(/seed=\d+/.test(title), '工具提示含可复现 seed');
  ok(
    title.includes('动量类因子 (38)=1次'),
    '工具提示含逐候选挖掘史次数（权重数据源可见）',
  );
  ok(!title.includes('均匀兜底'), '该行走通真加权路径（非 read-failure 兜底）');
}

const textRow = page
  .locator('tr')
  .filter({ hasText: '自由文本探针：量价背离与尾盘流动性' })
  .first();
ok((await textRow.count()) > 0, '历史行存在（自由文本探针方向）');
if (await textRow.count()) {
  const anyBadge = await textRow
    .locator('span:text-is("随机抽取"), span:text-is("类别选定")')
    .count();
  ok(anyBadge === 0, '自由文本行不渲染任何模式徽章（mode=NULL）');
}

ok(pageErrors.length === 0, '页面无 JS 报错', pageErrors.slice(0, 2).join(' | '));

await page.screenshot({ path: '/tmp/direction_mode_history.png' });

await browser.close();
console.log(fail === 0 ? `ALL PASS (${pass})` : `${fail} FAILED (${pass} pass)`);
process.exit(fail === 0 ? 0 : 1);
