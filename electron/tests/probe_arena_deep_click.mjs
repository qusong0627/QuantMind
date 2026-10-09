/**
 * arena 移植件的**下钻验收**（2026-09-22）：把搬过来的页面点到第二层。
 *
 * `probe_live_arena_tabs.mjs` 只证明「四栏 + 设置两块点得开」，这一支往下一层走：
 *   1) 行情回测三个 tab（工作台 / 策略排行 / 单策略选股）逐个点，验 `on` 激活态 + 作用域内真出内容；
 *   2) 设置 → 总控的两个 tab（总览 / 交易所设置）；
 *   3) **模型详情下钻**：总控模型表点一行 → `ModelDetail`（`.mdp-page`）→「← 返回」回列表。
 *      这条是 `ArenaSurface` 里那套「栏内下钻」自研管线的唯一出口，断了就是死链接；
 *   4) 全程零 pageerror、quest 无 5xx。
 *
 * 反「假通过」同 `probe_live_arena_tabs.mjs`：点击必须复查激活样式，内容锚 `.qm-arena-root`。
 * 跑：`PROBE_BASE=http://localhost:3080 node electron/tests/probe_arena_deep_click.mjs`
 */
import { chromium } from 'playwright';

const BASE = process.env.PROBE_BASE || 'http://localhost:3080';
const ALLOWED_404 = /\/agents\/market-research\/(performance|positions)\b/;

const browser = await chromium.launch({ executablePath: '/usr/bin/google-chrome-stable', args: ['--no-sandbox'] });
const page = await browser.newPage({ viewport: { width: 1680, height: 1000 } });

const errs = [];
const calls = [];
page.on('pageerror', (e) => errs.push('[PAGEERROR] ' + (e.message || '').slice(0, 220)));
page.on('response', (r) => {
  const u = r.url();
  if (/\/api\/v1\/agent-arena\//.test(u)) calls.push({ status: r.status(), url: u.replace(BASE, '') });
});

const text = async (loc) => ((await loc.innerText().catch(() => '')) || '').replace(/\s/g, '');
const clickByText = async (re, scope = 'button') => {
  const items = page.locator(scope);
  for (let i = 0, n = await items.count(); i < n; i++) {
    if (re.test(await text(items.nth(i)))) { await items.nth(i).click({ timeout: 5000 }).catch(() => {}); return true; }
  }
  return false;
};
const dismissModals = async () => {
  for (let round = 0; round < 8; round++) {
    const blocking = await page.evaluate(
      () => Array.from(document.querySelectorAll('.ant-modal-wrap')).filter((m) => getComputedStyle(m).display !== 'none').length,
    );
    if (!blocking) return;
    if (!(await clickByText(/稍后再答|同意|确认|我知道了|已阅读|知道了|跳过/, '.ant-modal:visible button'))) break;
    await page.waitForTimeout(700);
  }
};

const results = [];
const check = (name, pass, detail = '') => {
  results.push({ name, pass });
  console.log(`${pass ? '✅' : '❌'} ${name}${detail ? `  — ${detail}` : ''}`);
};

/** 点 arena 侧栏某栏并确认激活 */
const openTab = async (label, waitMs = 10000) => {
  await dismissModals();
  const btn = page.locator('button').filter({ hasText: new RegExp(`^${label}$`) }).first();
  await btn.click({ timeout: 8000 }).catch(async () => { await dismissModals(); await btn.click({ force: true }); });
  await page.waitForTimeout(waitMs);
  await dismissModals();
  return page.evaluate((lbl) => {
    const b = Array.from(document.querySelectorAll('button')).find((x) => (x.textContent || '').trim() === lbl);
    return !!b && /bg-blue-50/.test(b.className);
  }, label);
};

/**
 * 点作用域内某个**具体**按钮（按文案精确匹配），返回点击后 { clicked, inner }。
 * `cls` 是激活时要出现的类名 —— 点了不等于生效，必须复查。
 *
 * ⚠️ 激活态必须在**点击之后**重查：React 是异步重渲染，同一个 evaluate 里读完
 * className 再点，读到的永远是点击前的旧 class（会稳定误报 clicked-inactive）。
 */
const clickInArena = async (label, cls) => {
  const found = await page.evaluate((lbl) => {
    const root = document.querySelector('.qm-arena-root');
    if (!root) return 'no-root';
    const b = Array.from(root.querySelectorAll('button')).find((x) => (x.textContent || '').trim().startsWith(lbl));
    if (!b) return 'not-found';
    b.click();
    return 'ok';
  }, label);
  await page.waitForTimeout(6000);
  const state = await page.evaluate(({ lbl, want }) => {
    const root = document.querySelector('.qm-arena-root');
    const b = Array.from(root.querySelectorAll('button')).find((x) => (x.textContent || '').trim().startsWith(lbl));
    return {
      active: !want || (b ? b.className.includes(want) : false),
      inner: root ? root.innerText.replace(/\s+/g, ' ') : '',
    };
  }, { lbl: label, want: cls });
  return { clicked: state.active ? found : 'clicked-inactive', inner: state.inner };
};

// ── 登录 ──────────────────────────────────────────────────────────────────
await page.goto(`${BASE}/#/auth/login`, { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(4000);
const inputs = page.locator('input:visible');
if ((await inputs.count()) >= 2) {
  await inputs.nth(0).fill(process.env.PROBE_USER || 'admin');
  await inputs.nth(1).fill(process.env.PROBE_PASS || 'admin123');
  await clickByText(/登录/);
  await page.waitForTimeout(6000);
}
await dismissModals();
await page.goto(`${BASE}/#/live`, { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(8000);
await dismissModals();

// ── 1) 行情回测三 tab ────────────────────────────────────────────────────
{
  check('「行情回测」栏打开', await openTab('行情回测', 14000));
  const shots = { 策略排行: '/tmp/qm_arena_lab_rank.png', 单策略选股: '/tmp/qm_arena_lab_screener.png' };
  for (const [label, shot] of Object.entries(shots)) {
    const { clicked, inner } = await clickInArena(label, 'on');
    const table = await page.evaluate(() => {
      const root = document.querySelector('.qm-arena-root');
      const vis = (el) => el && el.getBoundingClientRect().height > 0;
      const rows = Array.from(root.querySelectorAll('tr')).filter(vis);
      const inputs2 = Array.from(root.querySelectorAll('select,input')).filter(vis);
      return { rows: rows.length, inputs: inputs2.length };
    });
    check(`「行情回测 → ${label}」点得动且激活（on）`, clicked === 'ok', clicked);
    check(`「行情回测 → ${label}」作用域内出内容`, inner.length > 400, `${inner.length} 字`);
    check(`「行情回测 → ${label}」出真表格/控件`, table.rows + table.inputs > 0, `可见行 ${table.rows} / 控件 ${table.inputs}`);
    await page.screenshot({ path: shot });
  }
  // 选股页点一行 → 应跳回工作台（onPickSymbol）
  // 行的首格是 `名称 + <span class="code">代码</span>`（BatchScreener.tsx:175），
  // 所以 innerText 以**名称**开头，代码不一定在行首 —— 只能「包含」不能「以之开头」。
  const jumped = await page.evaluate(() => {
    const root = document.querySelector('.qm-arena-root');
    const tr = Array.from(root.querySelectorAll('tr')).find(
      (r) => r.getBoundingClientRect().height > 0 && /\d{6}\.(SH|SZ|BJ)/.test(r.innerText),
    );
    if (!tr) return 'no-row';
    tr.click();
    return 'clicked';
  });
  if (jumped === 'clicked') {
    await page.waitForTimeout(6000);
    const back = await page.evaluate(() => {
      const b = Array.from(document.querySelectorAll('.qm-arena-root button')).find((x) => (x.textContent || '').trim().startsWith('工作台'));
      return !!b && b.className.includes('on');
    });
    check('「单策略选股」点一行 → 跳回工作台（选中态跟过去）', back, back ? '' : '没跳回或工作台没激活');
  } else {
    check('「单策略选股」点一行 → 跳回工作台', false, `没找到形如 600309.SH 的数据行（${jumped}）`);
  }
  await clickInArena('工作台', 'on');
}

// ── 2) 设置 → 总控 两个 tab ──────────────────────────────────────────────
await openTab('设置', 5000);
await clickByText(/^总控$/);
await page.waitForTimeout(12000);
{
  const overview = await page.evaluate(() => {
    const r = document.querySelector('.qm-arena-root');
    return r ? { len: r.innerText.replace(/\s+/g, ' ').length, tables: r.querySelectorAll('table').length } : null;
  });
  check('「设置 → 总控」总览出内容（含表格）', (overview?.len ?? 0) > 300 && (overview?.tables ?? 0) > 0, `${overview?.len} 字 / ${overview?.tables} 表`);

  // ── 3) 模型详情下钻：点一行 → ModelDetail → 返回 ──────────────────────
  const rowClicked = await page.evaluate(() => {
    const tr = Array.from(document.querySelectorAll('.qm-arena-root tr.clickable')).find((r) => r.getBoundingClientRect().height > 0);
    if (!tr) return 'no-row';
    tr.click();
    return 'clicked';
  });
  await page.waitForTimeout(8000);
  const detail = await page.evaluate(() => {
    const root = document.querySelector('.qm-arena-root');
    const name = root?.querySelector('h1.mdp-name')?.textContent?.trim() || null;
    const backBtn = root?.querySelector('button.mdp-back');
    return { name, hasBack: !!backBtn, text: root ? root.innerText.replace(/\s+/g, ' ') : '' };
  });
  check('总控点模型行 → 下钻进模型详情（.mdp-page + 模型名）', rowClicked === 'clicked' && !!detail.name, `${rowClicked} / ${detail.name ?? '无 .mdp-name'}`);
  check('模型详情有「← 返回」出口', detail.hasBack);
  await page.screenshot({ path: '/tmp/qm_arena_model_detail.png' });

  if (detail.hasBack) {
    await page.evaluate(() => document.querySelector('.qm-arena-root button.mdp-back').click());
    await page.waitForTimeout(6000);
    const after = await page.evaluate(() => {
      const root = document.querySelector('.qm-arena-root');
      return { stillDrill: !!root?.querySelector('.mdp-page'), len: root ? root.innerText.replace(/\s+/g, ' ').length : 0 };
    });
    check('「← 返回」回到总控列表（下钻层已卸载）', !after.stillDrill && after.len > 300, `仍在详情=${after.stillDrill} / ${after.len} 字`);
  }

  // 交易所设置 tab（原 /trading，上游并进总控）
  const { clicked, inner } = await clickInArena('交易所设置', 'active');
  check('「总控 → 交易所设置」点得动且激活', clicked === 'ok', clicked);
  check('「总控 → 交易所设置」作用域内出内容', inner.length > 200, `${inner.length} 字`);
  await page.screenshot({ path: '/tmp/qm_arena_control_exchange.png' });
}

// ── 4) 错误面 ────────────────────────────────────────────────────────────
const serverErrors = calls.filter((c) => c.status >= 500);
const unexpected404 = calls.filter((c) => c.status === 404 && !ALLOWED_404.test(c.url));
check(`arena 请求无 5xx（共 ${calls.length} 次）`, serverErrors.length === 0, serverErrors.slice(0, 5).map((c) => `${c.status} ${c.url}`).join(' ; '));
check('无意外 404', unexpected404.length === 0, unexpected404.slice(0, 5).map((c) => `${c.status} ${c.url}`).join(' ; '));
check('无 pageerror', errs.length === 0, errs.slice(0, 3).join(' ; '));

const failed = results.filter((r) => !r.pass);
console.log(`\n${results.length - failed.length}/${results.length} 通过`);
await browser.close();
process.exit(failed.length ? 1 : 0);
