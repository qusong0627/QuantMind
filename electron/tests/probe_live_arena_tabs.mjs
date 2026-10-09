/**
 * 「arena 整棵移植进实盘栏」验收探针（2026-09-22）。
 *
 * 断的是用户在本次改造里点名的五件事，全按 DOM 文本量，不靠截图肉眼：
 *   1) 侧栏在基础 9 栏之后**多出三栏**：智能体交易 / 实况 / 行情回测，且「设置」恒为最后一栏
 *      （2026-09-23 用户口径「设置放最低 / 最底部」；「关于」同日移进设置页，不再占侧栏一栏）；
 *   2) 设置栏里多出**三块内嵌面板**：总控 / 数据 / 关于；
 *   3) 三栏点开都有内容——尤其「智能体交易」要看见 arena 跑了大半个月的**历史数据**
 *      （agent 权益 + 决策日志条数），不是一句"暂无数据"；
 *   4) 「设置 → 数据 / 总控 / 关于」点开是 arena 页面本体；
 *   5) 全程 **零 pageerror**，arena 请求不出现 5xx。
 *
 * ## 两条反「假通过」的硬规矩（都是踩过才写的）
 *
 * - **点击必须验激活**：侧栏按钮点下去不等于切栏（首启的「投资适当性评估」弹窗会
 *   把点击整下吃掉，`.ant-modal-wrap` 覆盖全屏）。所以 `openTab()` 点完要复查按钮
 *   拿到 `bg-blue-50` 激活样式，再读内容；只看「页面里有没有『权益』两个字」是假通过
 *   ——顶栏的资产概览一直挂着那两个字。
 * - **内容锚在 `.qm-arena-root`**：arena 页面的宿主作用域类（`ArenaSurface`）。
 *   按整页 body 取文本会把顶栏/侧栏算进来，四栏都"有内容"。
 * - **底部让位要真的让到位**：`.bottom-dock` 是 absolute 覆盖层，arena 内容区一直铺到
 *   y≈975 而 Dock 顶沿在 y=936。占位块只有**把最后一行顶到 Dock 顶沿之上**才算数，
 *   所以 `openTab()` 里顺带把滚动容器滚到底、量末内容底边与 Dock 顶沿的差值。
 *
 * 打 3080（quantmind-web 容器，部署产物）还是 3000（vite dev）都行：
 *   PROBE_BASE=http://localhost:3080 node electron/tests/probe_live_arena_tabs.mjs
 * ⚠️ 深链一律带 `#/`（HashRouter），漏了会落到首页而不是目标页。
 */
import { chromium } from 'playwright';

const BASE = process.env.PROBE_BASE || 'http://localhost:3080';
const USER = process.env.PROBE_USER || 'admin';
const PASS = process.env.PROBE_PASS || 'admin123';

/**
 * 上游**明确声明「该 agent 没有账本」**的 404，不算故障：
 * `market-research` 是新闻/研究 agent，没有净值序列与持仓，
 * `/api/agents/market-research/{performance,positions}` 返回
 * `{"detail":"Agent 无净值数据: market-research"}`。前端两处都按「无数据」吃掉
 * （Live.tsx 的 `.filter(Boolean)`、AgentLedgerTab 同款）——**这正是 09-22 崩掉
 * 整页的那个坑**，白名单只放这两条路由，别再扩大。
 */
const ALLOWED_404 = /\/agents\/market-research\/(performance|positions)\b/;

const browser = await chromium.launch({
  executablePath: '/usr/bin/google-chrome-stable',
  args: ['--no-sandbox'],
});
const page = await browser.newPage({ viewport: { width: 1680, height: 1000 } });

const errs = [];
const arenaCalls = [];
page.on('pageerror', (e) => errs.push('[PAGEERROR] ' + (e.message || '').slice(0, 220)));
page.on('response', (r) => {
  const u = r.url();
  if (/\/api\/v1\/agent-arena\//.test(u)) {
    arenaCalls.push({ status: r.status(), url: u.replace(BASE, '') });
  }
});

const text = async (loc) => ((await loc.innerText().catch(() => '')) || '').replace(/\s/g, '');
const clickByText = async (re, scope = 'button') => {
  const items = page.locator(scope);
  for (let i = 0, n = await items.count(); i < n; i++) {
    if (re.test(await text(items.nth(i)))) { await items.nth(i).click({ timeout: 4000 }); return true; }
  }
  return false;
};

/** 关掉所有开着且盖住全屏的弹窗（首启合规/风险评测，antd 的 wrap 是 display:block 挡点击） */
const dismissModals = async () => {
  for (let round = 0; round < 8; round++) {
    const blocking = await page.evaluate(
      () => Array.from(document.querySelectorAll('.ant-modal-wrap'))
        .filter((m) => getComputedStyle(m).display !== 'none').length,
    );
    if (!blocking) return true;
    // 「稍后再答」是风险评测的正当出口（7 天内不再问），等价于用户跳过
    if (await clickByText(/稍后再答|同意|确认|我知道了|已阅读|知道了|跳过/, '.ant-modal:visible button')) {
      await page.waitForTimeout(800);
      continue;
    }
    await page.keyboard.press('Escape').catch(() => {});
    await page.waitForTimeout(600);
  }
  return false;
};

const results = [];
const check = (name, pass, detail = '') => {
  results.push({ name, pass, detail });
  console.log(`${pass ? '✅' : '❌'} ${name}${detail ? `  — ${detail}` : ''}`);
};

// ── 登录 ──────────────────────────────────────────────────────────────────
await page.goto(`${BASE}/#/auth/login`, { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(4000);
const inputs = page.locator('input:visible');
if ((await inputs.count()) >= 2) {
  await inputs.nth(0).fill(USER);
  await inputs.nth(1).fill(PASS);
  await clickByText(/登录/);
  await page.waitForTimeout(6000);
}
await dismissModals();

await page.goto(`${BASE}/#/live`, { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(8000);
await dismissModals();

/** 侧栏（「设置」所在的那条列）的全部按钮文案 */
const sidebarLabels = async () =>
  page.evaluate(() => {
    const b = Array.from(document.querySelectorAll('button'))
      .find((x) => (x.textContent || '').trim() === '设置');
    const strip = b?.parentElement?.parentElement;
    return strip ? Array.from(strip.querySelectorAll('button')).map((x) => (x.textContent || '').trim()) : [];
  });

/**
 * 点开某栏并**确认它真的激活了**。
 * 返回 { ok, content, clearance }——content 是 `.qm-arena-root` 作用域内的文本（没有则为 ''）；
 * clearance = Dock 顶沿 − 滚到底后末内容底边（正数=在 Dock 之上；无 Dock 时为 null）。
 */
const openTab = async (label, waitMs = 9000) => {
  await dismissModals();
  const btn = page.locator('button').filter({ hasText: new RegExp(`^${label}$`) }).first();
  await btn.click({ timeout: 8000 }).catch(async () => {
    await dismissModals();
    await btn.click({ force: true });
  });
  await page.waitForTimeout(waitMs);
  await dismissModals();
  const state = await page.evaluate((lbl) => {
    const b = Array.from(document.querySelectorAll('button'))
      .find((x) => (x.textContent || '').trim() === lbl);
    const root = document.querySelector('.qm-arena-root');
    const dock = document.querySelector('.bottom-dock');
    let clearance = null;
    if (root && dock) {
      // 整栏形态：根节点自己滚。滚到底后量**末内容**（跳过 aria-hidden 的让位占位块）。
      if (root.scrollHeight > root.clientHeight + 1) root.scrollTop = root.scrollHeight;
      let last = null;
      for (let i = root.children.length - 1; i >= 0; i--) {
        if (root.children[i].getAttribute('aria-hidden') === 'true') continue;
        last = root.children[i];
        break;
      }
      clearance = last
        ? Math.round(dock.getBoundingClientRect().top - last.getBoundingClientRect().bottom)
        : null;
    }
    return {
      active: !!b && /bg-blue-50/.test(b.className),
      content: root ? root.innerText.replace(/\s+/g, ' ') : null,
      clearance,
    };
  }, label);
  return { ok: state.active, content: state.content, clearance: state.clearance };
};

const side = await sidebarLabels();
console.log('侧栏：', side.join(' | '), '\n');
for (const label of ['智能体交易', '实况', '行情回测']) {
  check(`侧栏有「${label}」栏`, side.includes(label), side.includes(label) ? '' : `实得 ${JSON.stringify(side)}`);
}
check(
  '追加三栏排在基础栏之后、且「设置」恒为最后一栏（用户点名：设置放最底部）',
  side.indexOf('设置') === side.length - 1 &&
    ['智能体交易', '实况', '行情回测'].every((l) => side.indexOf(l) < side.indexOf('设置')),
  `实得 ${JSON.stringify(side)}`,
);
check('「关于」不再占侧栏一栏（2026-09-23 移进设置页）', !side.includes('关于'), `实得 ${JSON.stringify(side)}`);

// ── 三栏：点开 + 激活 + 作用域内内容 ──────────────────────────────────────
{
  const { ok, content, clearance } = await openTab('智能体交易', 14000);
  check('「智能体交易」点开后真的激活（按钮高亮）', ok);
  check('「智能体交易」滚到底不被悬浮 Dock 盖住', clearance == null || clearance > 0, `离 Dock 顶沿 ${clearance}px`);
  const c = content ?? '';
  check('台账内容在 arena 作用域内渲染', c.length > 200, `作用域内 ${c.length} 字`);
  check('有台账标题', /智能体交易\s*·\s*台账|智能体交易·台账/.test(c), c.slice(0, 80));
  check('决策日志/模型对话区在', /决策日志|模型对话/.test(c));
  check(
    '看得见历史决策记录（条数 > 0，不是"暂无"）',
    (() => {
      const m = /(\d+)\s*条/.exec(c);
      return !!m && Number(m[1]) > 0;
    })(),
    (/决策日志[^0-9]{0,20}(\d+)\s*条/.exec(c) || ['未匹配到条数'])[0],
  );
  check('每个 agent 一张卡（含权益数字）', /deepseek|glm|qwen|oai|ds-|gpt/i.test(c) && /\d[\d,]*\.\d{2}/.test(c));
}

{
  const { ok, content, clearance } = await openTab('实况', 14000);
  check('「实况」点开后真的激活', ok);
  check('实况内容在 arena 作用域内渲染', (content ?? '').length > 500, `作用域内 ${(content ?? '').length} 字`);
  check('「实况」滚到底不被悬浮 Dock 盖住', clearance == null || clearance > 0, `离 Dock 顶沿 ${clearance}px`);
}
await page.screenshot({ path: '/tmp/qm_live_arena_live.png' });

{
  const { ok, content, clearance } = await openTab('行情回测');
  check('「行情回测」点开后真的激活', ok);
  check('行情回测内容在 arena 作用域内渲染', (content ?? '').length > 200, `作用域内 ${(content ?? '').length} 字`);
  check('「行情回测」滚到底不被悬浮 Dock 盖住', clearance == null || clearance > 0, `离 Dock 顶沿 ${clearance}px`);
}

// ── 设置 → 总控 / 数据 / 关于（三块内嵌面板）──────────────────────────────
{
  const { ok } = await openTab('设置', 5000);
  check('设置栏可点开并激活', ok);
  const tabs = await page.evaluate(() =>
    Array.from(document.querySelectorAll('button')).map((b) => (b.textContent || '').trim()).filter(Boolean),
  );
  check('设置栏出现「总控」页签', tabs.includes('总控'));
  check('设置栏出现「数据」页签', tabs.includes('数据'));
  check('设置栏出现「关于」页签（2026-09-23 自侧栏移入）', tabs.includes('关于'));

  // 第三项是等待毫秒：数据/总控都是要拉后端数据的重面板，关于是纯静态文档页
  for (const [label, shot, waitMs] of [
    ['数据', '/tmp/qm_live_arena_settings_data.png', 12000],
    ['总控', '/tmp/qm_live_arena_settings_control.png', 12000],
    ['关于', '/tmp/qm_live_arena_settings_about.png', 6000],
  ]) {
    await dismissModals();
    const clicked = await clickByText(new RegExp(`^${label}$`));
    await page.waitForTimeout(waitMs);
    const panel = await page.evaluate(() => {
      const roots = Array.from(document.querySelectorAll('.qm-arena-root'));
      const root = roots[0];
      let clearance = null;
      if (root) {
        // 内嵌形态滚的是外层设置卡片（根节点是 min-h-full，自己不滚）
        let el = root.parentElement;
        while (el && !(el.scrollHeight > el.clientHeight + 1 && /auto|scroll|hidden/.test(getComputedStyle(el).overflowY))) el = el.parentElement;
        if (el) el.scrollTop = el.scrollHeight;
        const dock = document.querySelector('.bottom-dock');
        let last = null;
        for (let i = root.children.length - 1; i >= 0; i--) {
          if (root.children[i].getAttribute('aria-hidden') === 'true') continue;
          last = root.children[i];
          break;
        }
        if (dock && last) clearance = Math.round(dock.getBoundingClientRect().top - last.getBoundingClientRect().bottom);
      }
      return {
        content: roots.length ? roots.map((r) => r.innerText.replace(/\s+/g, ' ')).join(' ') : null,
        clearance,
      };
    });
    check(`「设置 → ${label}」点开，arena 面板在作用域内渲染`, clicked && (panel.content ?? '').length > 200, `作用域内 ${(panel.content ?? '').length} 字`);
    check(`「设置 → ${label}」滚到底不被悬浮 Dock 盖住`, panel.clearance == null || panel.clearance > 0, `离 Dock 顶沿 ${panel.clearance}px`);
    await page.screenshot({ path: shot });
  }
}

// ── 网络与错误 ────────────────────────────────────────────────────────────
const serverErrors = arenaCalls.filter((c) => c.status >= 500);
const unexpected404 = arenaCalls.filter((c) => c.status === 404 && !ALLOWED_404.test(c.url));
check(`arena 请求无 5xx（共 ${arenaCalls.length} 次）`, serverErrors.length === 0, serverErrors.slice(0, 5).map((c) => `${c.status} ${c.url}`).join(' ; '));
check('无意外 404（白名单只含 market-research 的无账本声明）', unexpected404.length === 0, unexpected404.slice(0, 5).map((c) => `${c.status} ${c.url}`).join(' ; '));
check('无 pageerror', errs.length === 0, errs.slice(0, 3).join(' ; '));

const allowed = arenaCalls.filter((c) => c.status === 404).length;
console.log(`\narena 调用样本（404 白名单命中 ${allowed} 次）：`);
console.log([...new Set(arenaCalls.map((c) => `${c.status} ${c.url}`))].slice(0, 20).join('\n'));

const failed = results.filter((r) => !r.pass);
console.log(`\n${results.length - failed.length}/${results.length} 通过`);
await browser.close();
process.exit(failed.length ? 1 : 0);
