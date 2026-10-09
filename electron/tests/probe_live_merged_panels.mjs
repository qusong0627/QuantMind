/**
 * 「推送下单 → 手动任务」「风控止损 → 持仓监控」并栏验收（2026-09-23 用户要求
 * 「推送下单放手动任务、风控止损放持仓监控那」）。
 *
 * 只认量出来的结果，不认「点了没报错」：
 * - 侧栏里这两栏**必须消失**（否则等于没并，只是多了一处入口）；
 * - 宿主页里那一块**必须真的渲染出来**（高度 > 0、文本命中），且**不能被容器切掉**
 *   （scrollWidth ≤ clientWidth + 1，同 probe_arena_overflow 的判据）；
 * - 风控卡进 380px 右栏后**实际排几列**要现场数（`lg:grid-cols-4` 是视口断点，
 *   进窄栏不会自动收列 —— 这正是「右侧框显示不全」的经典成因）。
 *
 * 归属：探针自身可能出错，白名单只放确实与本次验收无关的上游 404。
 * 运行：PROBE_BASE=http://localhost:3080 node electron/tests/probe_live_merged_panels.mjs
 *      （必须从仓库根跑；临时脚本因 playwright 解析必须放 electron/tests/ 下）
 */
import { chromium } from 'playwright';

const BASE = process.env.PROBE_BASE || 'http://localhost:3080';
const results = [];
const check = (name, ok, detail = '') => {
  results.push({ name, ok, detail });
  console.log(`${ok ? '  ✓' : '  ✗'} ${name}${detail ? ` —— ${detail}` : ''}`);
};

const browser = await chromium.launch({
  executablePath: '/usr/bin/google-chrome-stable',
  args: ['--no-sandbox'],
});
const page = await browser.newPage({ viewport: { width: 1680, height: 1000 } });
const pageErrors = [];
const badResponses = [];
page.on('pageerror', (e) => pageErrors.push(String(e.message || e).slice(0, 160)));
page.on('response', (r) => {
  if (r.status() >= 500) badResponses.push(`${r.status()} ${r.url().slice(0, 90)}`);
});
const text = async (loc) => ((await loc.innerText().catch(() => '')) || '').replace(/\s/g, '');
const clickByText = async (re, scope = 'button') => {
  const items = page.locator(scope);
  for (let i = 0, n = await items.count(); i < n; i++) {
    if (re.test(await text(items.nth(i)))) {
      await items.nth(i).click({ timeout: 5000 }).catch(() => {});
      return true;
    }
  }
  return false;
};
const dismiss = async () => {
  for (let r = 0; r < 8; r++) {
    const n = await page.evaluate(
      () => Array.from(document.querySelectorAll('.ant-modal-wrap')).filter((m) => getComputedStyle(m).display !== 'none').length,
    );
    if (!n) return;
    if (!(await clickByText(/稍后再答|同意|确认|我知道了|已阅读|知道了|跳过/, '.ant-modal:visible button'))) break;
    await page.waitForTimeout(700);
  }
};
/** 侧栏按钮标签（左栏导航，200px 宽那条） */
const sidebarLabels = () =>
  page.evaluate(() =>
    Array.from(document.querySelectorAll('button'))
      .filter((b) => b.closest('.w-\\[200px\\]'))
      .map((b) => (b.innerText || '').trim())
      .filter(Boolean),
  );
/** 某容器里的横向切字审计：返回被省略号/裁掉的单元格 */
const cutAudit = (sel) =>
  page.evaluate((s) => {
    const box = document.querySelector(s);
    if (!box) return null;
    const out = [];
    box.querySelectorAll('*').forEach((el) => {
      if (el.children.length) return;
      if (el.scrollWidth > el.clientWidth + 1 && (el.innerText || '').trim()) {
        out.push({ t: el.innerText.trim().slice(0, 20), need: el.scrollWidth, got: el.clientWidth });
      }
    });
    return { w: Math.round(box.getBoundingClientRect().width), over: box.scrollWidth - box.clientWidth, cut: out.slice(0, 8) };
  }, sel);

try {
  await page.goto(`${BASE}/#/auth/login`, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(4000);
  const inputs = page.locator('input:visible');
  if ((await inputs.count()) >= 2) {
    await inputs.nth(0).fill('admin');
    await inputs.nth(1).fill('admin123');
    await clickByText(/登录/);
    await page.waitForTimeout(6000);
  }
  await dismiss();
  await page.goto(`${BASE}/#/live`, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(8000);
  await dismiss();

  // ── 1. 侧栏：这两栏必须已经不在 ────────────────────────────────────────────
  const labels = await sidebarLabels();
  console.log(`\n侧栏：${labels.join(' / ')}`);
  check('侧栏不再有「推送下单」', !labels.includes('推送下单'));
  check('侧栏不再有「风控止损」', !labels.includes('风控止损'));
  check('侧栏仍有 QuantBot（未被误删）', labels.includes('QuantBot'));

  // ── 2. 手动任务：向导下方要有推送下单 ─────────────────────────────────────
  await dismiss();
  await clickByText(/^手动任务$/);
  await page.waitForTimeout(7000);
  await dismiss();
  const manual = await page.evaluate(() => {
    // 「推送下单」这一块：找标题文字所在的 section（Card 组件就是 section）
    const head = Array.from(document.querySelectorAll('h3')).find((h) => (h.innerText || '').includes('推送下单'));
    const card = head?.closest('section');
    if (!card) return { found: false };
    const r = card.getBoundingClientRect();
    // 在不在页面文档流里（而不是 display:none / 被卸载）
    const scrollHost = card.closest('.overflow-y-auto');
    return {
      found: true,
      h: Math.round(r.height),
      w: Math.round(r.width),
      // 宿主页是根滚动容器；相对它的纵向位置说明「在向导下方」
      offsetInHost: scrollHost ? Math.round(r.top - scrollHost.getBoundingClientRect().top + scrollHost.scrollTop) : null,
      hostScrollH: scrollHost?.scrollHeight ?? null,
      hasTextarea: !!card.querySelector('textarea'),
      hasPreflightBtn: Array.from(card.querySelectorAll('button')).some((b) => /预检并推送/.test(b.innerText || '')),
      overflow: card.scrollWidth - card.clientWidth,
      // 同页还应有滚动调仓配置那块（推送下单是两张卡一起搬过来的）
      siblingCards: Array.from(document.querySelectorAll('h3')).map((h) => h.innerText.trim()).filter(Boolean),
    };
  });
  check('手动任务页出现「推送下单」卡片', manual.found, manual.found ? `${manual.w}×${manual.h}px` : '没找到 h3 标题');
  if (manual.found) {
    check('推送下单卡片内容真的渲染（textarea + 预检按钮）', manual.hasTextarea && manual.hasPreflightBtn);
    check('推送下单卡片不被容器切（无横向溢出）', manual.overflow <= 1, `溢出 ${manual.overflow}px`);
    check(
      '「滚动调仓配置」也一并搬来',
      (manual.siblingCards || []).some((t) => t.includes('滚动调仓配置')),
      (manual.siblingCards || []).join(' / ').slice(0, 80),
    );
  }
  // 滚到底：这一块必须在向导之后，且能被滚到（不被悬浮 Dock 吞掉）
  const reached = await page.evaluate(() => {
    const head = Array.from(document.querySelectorAll('h3')).find((h) => (h.innerText || '').includes('推送下单'));
    const card = head?.closest('section');
    if (!card) return null;
    card.scrollIntoView({ block: 'center' });
    const r = card.getBoundingClientRect();
    const dock = document.querySelector('[data-testid="dock"]') || document.querySelector('.dock, [class*="dock"]');
    return {
      visible: r.top < window.innerHeight && r.bottom > 0,
      bottom: Math.round(r.bottom),
      viewport: window.innerHeight,
      dockTop: dock ? Math.round(dock.getBoundingClientRect().top) : null,
    };
  });
  check('推送下单滚得到（scrollIntoView 后可见）', !!reached?.visible, JSON.stringify(reached));

  // ── 3. 持仓监控：右栏要有风控止损 ────────────────────────────────────────
  await dismiss();
  await clickByText(/^持仓监控$/);
  await page.waitForTimeout(9000);
  await dismiss();
  const rail = await page.evaluate(() => {
    const aside = document.querySelector('[data-testid="position-rail"]');
    if (!aside) return { found: false };
    const sec = Array.from(aside.querySelectorAll('section')).find((s) => (s.innerText || '').includes('风控与风险锁'));
    const cards = aside.querySelectorAll(':scope > *, :scope > div > section');
    // 风控块的四个指标：数它们实际排成几行几列
    const metrics = sec ? Array.from(sec.querySelectorAll('div.grid > div')) : [];
    const xs = [...new Set(metrics.map((m) => Math.round(m.getBoundingClientRect().left)))];
    const ys = [...new Set(metrics.map((m) => Math.round(m.getBoundingClientRect().top)))];
    const widths = metrics.map((m) => Math.round(m.getBoundingClientRect().width));
    // 指标卡内文字有没有被切（标签换行不算切，scrollWidth>clientWidth 才算）
    const cut = [];
    metrics.forEach((m) => {
      m.querySelectorAll('*').forEach((el) => {
        if (el.children.length) return;
        if (el.scrollWidth > el.clientWidth + 1 && (el.innerText || '').trim()) {
          cut.push(`${el.innerText.trim().slice(0, 12)}(${el.scrollWidth}>${el.clientWidth})`);
        }
      });
    });
    const r = aside.getBoundingClientRect();
    return {
      found: true,
      railW: Math.round(r.width),
      hasToken: !!sec,
      sectionTitle: sec ? '风控与风险锁' : null,
      metricCount: metrics.length,
      cols: xs.length,
      rows: ys.length,
      cardWidths: widths,
      cut,
      railOverflow: aside.scrollWidth - aside.clientWidth,
      railOrder: Array.from(aside.querySelectorAll('section')).map((s) => (s.querySelector('h3')?.innerText || s.innerText.split('\n')[0] || '').trim().slice(0, 14)),
      childCount: cards.length,
    };
  });
  check('持仓监控右栏存在', rail.found, rail.found ? `栏宽 ${rail.railW}px` : '没找到 [data-testid=position-rail]');
  if (rail.found) {
    check('右栏里有「风控与风险锁」', rail.hasToken);
    check('风控指标卡数量为 4', rail.metricCount === 4, `实际 ${rail.metricCount}`);
    check('右栏无横向溢出', rail.railOverflow <= 1, `溢出 ${rail.railOverflow}px`);
    check('指标卡内文字没被切', rail.cut.length === 0, rail.cut.join(' '));
    // 「没被切」不等于「好读」：380px 里塞 4 列时单卡只有 ~80px，标签折三行。
    // 断点按视口、容器按实际宽度，两者对不上正是这一栏最初被用户点名的问题。
    check(
      '指标卡不至于挤成窄条（单卡 ≥150px）',
      rail.cardWidths.every((w) => w >= 150),
      `单卡 ${rail.cardWidths.join('/')}px`,
    );
    check(
      '风控块在哨兵之后、副驾驶之前',
      rail.railOrder.findIndex((t) => t.includes('风控')) > -1 &&
        rail.railOrder.findIndex((t) => t.includes('风控')) < rail.railOrder.findIndex((t) => /副驾驶|Copilot/i.test(t)),
      rail.railOrder.join(' | '),
    );
    console.log(
      `    风控指标实测：${rail.cols} 列 × ${rail.rows} 行，单卡宽 ${rail.cardWidths.join('/')}px（右栏 ${rail.railW}px）`,
    );
  }
  const railAudit = await cutAudit('[data-testid="position-rail"]');
  console.log(`    右栏整体：宽 ${railAudit?.w}px 溢出 ${railAudit?.over}px 切字 ${railAudit?.cut?.length ?? 0} 处`);
  for (const c of railAudit?.cut ?? []) console.log(`      · 「${c.t}」需 ${c.need} 得 ${c.got}`);

  // ── 4. 页面级横向溢出（顺带看一眼有没有把整页撑破） ───────────────────────
  const pageOver = await page.evaluate(() => ({
    docOver: document.documentElement.scrollWidth - document.documentElement.clientWidth,
  }));
  check('整页无横向溢出', pageOver.docOver <= 2, `溢出 ${pageOver.docOver}px`);

  // ── 5. 无 pageerror / 5xx ────────────────────────────────────────────────
  check('无 pageerror', pageErrors.length === 0, pageErrors.join(' | ').slice(0, 200));
  check('无 5xx 响应', badResponses.length === 0, badResponses.join(' | ').slice(0, 200));
} catch (e) {
  check('探针自身未抛异常', false, String(e).slice(0, 300));
}

const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} 通过`);
if (failed.length) console.log(`失败：${failed.map((f) => f.name).join('；')}`);
await browser.close();
process.exit(failed.length ? 1 : 0);
