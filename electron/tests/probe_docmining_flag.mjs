/**
 * 文档挖掘入口的真机验收探针：`VITE_ENABLE_DOC_MINING` 决定界面的两态。
 *
 * 与后端 `ENABLE_DOC_MINING` 是同一件事的两端：前端管「渲染不渲染」，后端管
 * 「端点通不通」。这个探针量的是前者——
 *
 *   PROBE_EXPECT=off（默认）：
 *     因子挖掘首页没有「上传文档」输入方式；挖掘历史没有「文档解析」Tab；
 *     页面与从前一字不差（推荐方向/状态筛选都在）。
 *   PROBE_EXPECT=on：
 *     首页出现「文字指令 ⇄ 上传文档」切换，切到上传文档能看到上传区与
 *     MinerU 出网披露；挖掘历史出现「文档解析」Tab，切过去任务筛选让位。
 *
 * 两个状态都额外扫描：界面上不允许出现内部工单号（T-FM-xx）。
 *
 * 前置：本地 vite dev（`cd electron && VITE_PORT=3000 npx vite`）+ 后端在跑。
 * dev 下开关**默认是开的**，所以跑「off」态必须显式
 * `VITE_ENABLE_DOC_MINING=false` 起 dev（或对生产构建跑）。
 * ⚠️ 别用 5173 —— 那是另一个项目的 vite。
 */
import { chromium } from 'playwright';

const BASE = process.env.PROBE_BASE || 'http://localhost:3000';
const EXPECT = (process.env.PROBE_EXPECT || 'off').toLowerCase();

const results = [];
const check = (name, ok, detail) => {
  results.push({ name, ok, detail });
  console.log(`${ok ? '  ✅' : '  ❌'} ${name}${detail ? ` — ${detail}` : ''}`);
};

/** 内部工单号绝不允许进界面（任何状态、任何页面）。 */
const TICKET_RE = /T-FM-\d+/g;

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

/** 首访合规/适当性弹窗：不点掉它，点击会被遮罩吞掉、读到的也只是弹窗文本。
 *  「稍后再答」是投资适当性评估（首启 1 次）的低干预出口——它同样遮全页。 */
const dismissModals = async () => {
  for (let round = 0; round < 6; round++) {
    const modal = page.locator('.ant-modal:visible');
    if ((await modal.count()) === 0) break;
    const mbtns = modal.first().locator('button');
    let clicked = false;
    for (let i = 0, n = await mbtns.count(); i < n; i++) {
      const t = ((await mbtns.nth(i).innerText().catch(() => '')) || '').replace(/\s/g, '');
      if (/同意|确认|我知道了|已阅读|知道了|稍后再答/.test(t)) {
        await mbtns.nth(i).click({ timeout: 5000 }).catch(() => {});
        clicked = true;
        break;
      }
    }
    if (!clicked) {
      // 适当性评估的「关闭」按钮是右上角 ×（icon 按钮，无文字）
      const closeBtn = modal.first().locator('.ant-modal-close');
      if ((await closeBtn.count()) > 0) {
        await closeBtn.click({ timeout: 5000 }).catch(() => {});
        clicked = true;
      }
    }
    if (!clicked) break;
    await page.waitForTimeout(1200);
  }
};

const bodyText = () => page.evaluate(() => document.body.innerText || '');

/** 按可见文字点一个 button（精确匹配压缩后文本）。
 *  点击被吞（遮罩拦了 / 元素消失）时返回 false——点了不算点到。 */
const clickButtonByText = async (text) => {
  const btns = page.locator('button');
  for (let i = 0, n = await btns.count(); i < n; i++) {
    const t = ((await btns.nth(i).innerText().catch(() => '')) || '').replace(/\s+/g, '');
    if (t === text) {
      try {
        await btns.nth(i).click({ timeout: 5000 });
        return true;
      } catch {
        return false;
      }
    }
  }
  return false;
};

const checkNoTickets = (label, text) => {
  const hits = text.match(TICKET_RE) || [];
  check(`${label}：无内部工单号`, hits.length === 0, hits.slice(0, 3).join(' / ') || undefined);
};

console.log(`\n=== 文档挖掘入口验收（expect=${EXPECT}） @ ${BASE} ===\n`);

// ── 1. 因子挖掘首页 ─────────────────────────────────────────────────
await page.goto(`${BASE}/#/alpha-research`, { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(6000);
await dismissModals();
{
  const text = await bodyText();
  if (text.trim().length < 40) {
    check('首页：页面有内容', false, `可见文本仅 ${text.trim().length} 字，断言无意义`);
  } else {
    const hasToggle = text.includes('上传文档');
    if (EXPECT === 'off') {
      check('首页：无「上传文档」输入方式', !hasToggle);
      check('首页：文字链完好（推荐方向在）', text.includes('推荐方向'));
    } else {
      check('首页：「上传文档」输入方式在', hasToggle);
      if (hasToggle) {
        const clicked = await clickButtonByText('上传文档');
        await page.waitForTimeout(1500);
        const docText = await bodyText();
        check(
          '首页：切到上传文档出现上传区',
          clicked && docText.includes('点击选择文件，或拖入此区域'),
          clicked ? undefined : '切换按钮点击被吞（遮罩？）',
        );
        check(
          '首页：MinerU 出网披露在（数据出网明示）',
          docText.includes('文档将上传至 MinerU 云端服务'),
        );
        if (clicked) {
          await clickButtonByText('文字指令');
          await page.waitForTimeout(1200);
          const backText = await bodyText();
          check('首页：切回文字指令后上传区消失', !backText.includes('点击选择文件，或拖入此区域'));
        }
      }
    }
    checkNoTickets('首页', text);
  }
}

// ── 2. 挖掘历史（侧栏进入） ─────────────────────────────────────────
{
  await page.goto(`${BASE}/#/alpha-research`, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(5000);
  await dismissModals();
  const opened = await clickButtonByText('挖掘历史');
  await page.waitForTimeout(4000);
  await dismissModals();
  const text = await bodyText();
  check('挖掘历史：可进入', opened && text.includes('挖掘历史'), opened ? undefined : '侧栏按钮没找到');

  const hasDocsTab = text.includes('文档解析');
  if (EXPECT === 'off') {
    check('挖掘历史：无「文档解析」Tab', !hasDocsTab);
    check('挖掘历史：任务筛选完好（状态筛选在）', text.includes('全部状态'));
  } else {
    check('挖掘历史：「文档解析」Tab 在', hasDocsTab);
    if (hasDocsTab) {
      await clickButtonByText('文档解析');
      await page.waitForTimeout(4000);
      const docsText = await bodyText();
      check('挖掘历史：切到文档页签后任务筛选让位', !docsText.includes('全部状态'));
      // 数据面三种合法态：空态 / 有列表 / 后端闸门未开（只开前端的错配，UI 无责）
      const good =
        docsText.includes('还没有上传过文档') ||
        /共 \d+ 份/.test(docsText) ||
        docsText.includes('doc_mining_disabled');
      check(
        '挖掘历史：文档页签有数据面（空态/列表/后端未开提示）',
        good,
        good ? undefined : docsText.slice(0, 160).replace(/\s+/g, ' '),
      );
    }
  }
  checkNoTickets('挖掘历史', text);
}

const pageErrs = errs.filter((e) => !/ResizeObserver|favicon/.test(e));
check('无页面级 JS 错误', pageErrs.length === 0, pageErrs.slice(0, 3).join(' | '));

await browser.close();

const failed = results.filter((r) => !r.ok);
console.log(`\n${failed.length === 0 ? 'PASS' : `FAIL（${failed.length} 项）`}：${results.length} 项检查`);
process.exit(failed.length === 0 ? 0 : 1);
