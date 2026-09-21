/**
 * 开源发行验收探针：`ENABLE_REAL_TRADING=false` 时**看不到任何实盘入口**。
 *
 * 用户点单：「实盘界面功能关闭，不显示，代码功能留着」「机构级别优化和生产环境，
 * 不要显示任何实盘相关的」。这条要求的机器可检查形态就是这个探针 —— 代码留着（
 * `git grep RealTradingPage` 仍有几千行），但翻遍导航/交易页/设置页都摸不到入口。
 *
 * ⚠️ 与本目录其它探针的区别：那些量**几何**（对齐、宽度），这个量**措辞与入口**。
 * 因此断言分两层，别混：
 *
 *   ① **入口词**（硬失败）——「券商实盘接入」「大 QMT」「确认下单」「一键买入」这类
 *      **可点击/可进入**的东西。出现即代表开关没兜住，是真漏。
 *   ② **「实盘」二字**（硬失败，但判据不同）——合规文案里合法地会出现这两个字
 *      （「实盘功能默认关闭，详见 DISCLAIMER.md」正是我们希望用户看到的）。
 *      所以这里不禁止该词，而是要求**它出现在说它关着的句子里**：命中处 ±30 字
 *      窗口内必须含否定/关闭语。这样「实盘交易运行台」会被拦下，「实盘通道默认关闭」
 *      会被放行。用词表禁止会把合规说明一起误杀，用邻域判据不会。
 *
 * 前置：本地 vite dev（`cd electron && VITE_PORT=3000 npx vite`）+ 后端在跑，
 * 且 `VITE_ENABLE_REAL_TRADING` 未开（未设时 PROD=false / DEV=true —— **dev 下默认是开的**，
 * 所以跑本探针须显式 `VITE_ENABLE_REAL_TRADING=false`，否则失败是真实的：那正是「开着」的样子）。
 *
 * ⚠️ 别用 5173 —— 那是另一个项目（inkwell）的 vite。
 */
import { chromium } from 'playwright';

const BASE = process.env.PROBE_BASE || 'http://localhost:3000';

/**
 * 入口词：可点击 / 可进入的实盘功能。这些词**任何页面都不许出现**。
 * 只收「点了会进入实盘」或「说了会替你下单」的词，不收中性名词。
 */
const ENTRY_TOKENS = [
  '券商实盘接入',
  '大 QMT',
  '真单镜像',
  '确认下单',
  '一键买入',
  '一键卖出',
  '强烈看多',
  '偏多研判',
  '看空预警',
  '启动实盘交易',
  '停止实盘交易',
  '实盘交易运行台',
];

/** 「实盘」二字合法出现的判据：邻域里必须正在说它关着。 */
const CLOSED_WORDS = /(关闭|不显示|停用|未启用|禁用|默认不|默认关|不提供|尚未|不支持)/;
const NEIGHBOURHOOD = 30;

/** 要走的页面。深链必须带 `#/`（HashRouter）；不带会落到默认页。 */
const ROUTES = [
  { path: '#/', name: '首页' },
  { path: '#/desk', name: '交易台' },
  { path: '#/trading', name: '模拟交易' },
  { path: '#/stock-terminal', name: '个股终端' },
  { path: '#/inference-center', name: '推理中心' },
];

const results = [];
const check = (name, ok, detail) => {
  results.push({ name, ok, detail });
  console.log(`${ok ? '  ✅' : '  ❌'} ${name}${detail ? ` — ${detail}` : ''}`);
};

const browser = await chromium.launch({
  executablePath: '/usr/bin/google-chrome-stable',
  args: ['--no-sandbox'],
});
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
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
    if (/登录/.test(t)) {
      await btns.nth(i).click();
      break;
    }
  }
  await page.waitForTimeout(6000);
}

/** 首访合规弹窗：不点掉它，后面每次读到的都是弹窗文本。 */
const dismissModals = async () => {
  for (let round = 0; round < 4; round++) {
    const modal = page.locator('.ant-modal:visible');
    if ((await modal.count()) === 0) break;
    const mbtns = modal.first().locator('button');
    let clicked = false;
    for (let i = 0, n = await mbtns.count(); i < n; i++) {
      const t = ((await mbtns.nth(i).innerText().catch(() => '')) || '').replace(/\s/g, '');
      if (/同意|确认|我知道了|已阅读|知道了/.test(t)) {
        await mbtns.nth(i).click({ timeout: 5000 }).catch(() => {});
        clicked = true;
        break;
      }
    }
    if (!clicked) break;
    await page.waitForTimeout(1200);
  }
};

/** 页面可见文本（innerText 遵守 CSS 可见性，hidden 的元素不会混进来）。 */
const bodyText = () => page.evaluate(() => document.body.innerText || '');

/** 「实盘」出现但不在说它关着的地方 —— 返回上下文片段供人眼判。 */
const unclosedLiveMentions = (text) => {
  const bad = [];
  let i = text.indexOf('实盘');
  while (i >= 0) {
    const win = text.slice(Math.max(0, i - NEIGHBOURHOOD), i + 2 + NEIGHBOURHOOD);
    if (!CLOSED_WORDS.test(win)) bad.push(win.replace(/\s+/g, ' ').trim());
    i = text.indexOf('实盘', i + 2);
  }
  return bad;
};

const scan = (label, text) => {
  const hits = ENTRY_TOKENS.filter((t) => text.includes(t));
  check(`${label}：无实盘入口词`, hits.length === 0, hits.join(' / ') || undefined);

  const bad = unclosedLiveMentions(text);
  check(
    `${label}：「实盘」只出现在说它关闭的句子里`,
    bad.length === 0,
    bad.slice(0, 2).join(' ｜ ') || undefined,
  );
};

console.log(`\n=== 实盘入口隐藏验收 @ ${BASE} ===\n`);

for (const { path, name } of ROUTES) {
  await page.goto(`${BASE}/${path}`, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(5000);
  await dismissModals();
  const text = await bodyText();
  if (text.trim().length < 40) {
    // 空页面会让所有「不含某词」的断言**假通过** —— 零项参与即失败。
    check(`${name}：页面有内容`, false, `可见文本仅 ${text.trim().length} 字，断言无意义`);
    continue;
  }
  scan(name, text);
}

// ── 模拟交易页的「设置」页签：实盘两个页签的唯一入口，必须点进去看 ──────────
await page.goto(`${BASE}/#/trading`, { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(5000);
await dismissModals();
{
  const tabs = page.locator('button');
  let opened = false;
  for (let i = 0, n = await tabs.count(); i < n; i++) {
    const t = ((await tabs.nth(i).innerText().catch(() => '')) || '').replace(/\s/g, '');
    if (t === '设置') {
      await tabs.nth(i).click({ timeout: 5000 }).catch(() => {});
      opened = true;
      break;
    }
  }
  check('交易页存在「设置」页签且可点开', opened);
  if (opened) {
    await page.waitForTimeout(4000);
    await dismissModals();
    scan('交易页 · 设置', await bodyText());
  }
}

// ── 导航标签：关闭态下恒为「模拟交易」，不跟随 localStorage 的偏好 ──────────
{
  await page.evaluate(() => localStorage.setItem('qm:trading_mode_pref', 'real'));
  await page.goto(`${BASE}/#/desk`, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(5000);
  await dismissModals();
  const navText = await page.evaluate(() => {
    const nav = document.querySelector('nav, [data-testid="floating-nav"], aside');
    return (nav?.innerText || document.body.innerText || '').replace(/\s+/g, ' ');
  });
  check(
    'localStorage 写着 real 时导航仍为「模拟交易」',
    navText.includes('模拟交易') && !navText.includes('实盘交易'),
    navText.slice(0, 120),
  );
}

const pageErrs = errs.filter((e) => !/ResizeObserver|favicon/.test(e));
check('无页面级 JS 错误', pageErrs.length === 0, pageErrs.slice(0, 3).join(' | '));

await browser.close();

const failed = results.filter((r) => !r.ok);
console.log(`\n${failed.length === 0 ? 'PASS' : `FAIL（${failed.length} 项）`}：${results.length} 项检查`);
process.exit(failed.length === 0 ? 0 : 1);
