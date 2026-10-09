/**
 * 实况净值图「成交标记必须在线上」探针（2026-10-07）。
 *
 * 背景（用户报障）：实况页 ▲买/▼卖 标记「有些没有在线上」。根因 = 标记 x 误用
 * **本线序号**（nearestIdxOfTime(l.points)）当**多线并集序号**（allTimes/xScale 域）
 * 用——近端成交横移可达 ~260px，y 仍是正确时刻的值 → 视觉上脱离折线。
 * 修复：EquityChart.tsx 标记块经 idxOf(l.points[idx].t) 换算到联合轴序号。
 *
 * 判据（几何不变量，与实现解耦）：
 *   1. 图上至少有 20 枚 ▲/▼ 标记（防空图假通过）；
 *   2. 每枚标记的**尖端**（path d 的 M 起点，画在线上方/下方 4px）到任一条折线
 *      路径的最小距离 ≤ 12px（设计间隙 4px + 曲线/解析容差）；
 *   3. 无未捕获 pageerror。
 *
 * 用法：PROBE_BASE=http://localhost:3080 node tests/probe_live_fill_marks.mjs
 * 退出码：0 全过；1 有 FAIL（打印最差 10 枚的坐标与距离，供归因）。
 */
import { chromium } from 'playwright';

const BASE = process.env.PROBE_BASE || 'http://localhost:3080';
const MAX_DIST = 12; // px：尖端到折线的最小允许距离

const results = [];
const record = (name, ok, detail) => {
  results.push({ name, ok, detail });
  console.log(`${ok ? '✓ PASS' : '✗ FAIL'} ${name}${detail ? ` — ${detail}` : ''}`);
};

const browser = await chromium.launch({
  executablePath: '/usr/bin/google-chrome-stable',
  args: ['--no-sandbox'],
});
const page = await browser.newPage({ viewport: { width: 1920, height: 1080 } });
const pageErrors = [];
page.on('pageerror', (e) => pageErrors.push((e.message || '').slice(0, 200)));

async function login() {
  await page.goto(`${BASE}/#/auth/login`, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(4000);
  const inputs = page.locator('input:visible');
  if ((await inputs.count()) >= 2) {
    await inputs.nth(0).fill('admin');
    await inputs.nth(1).fill('admin123');
    const btns = page.locator('button');
    for (let i = 0, n = await btns.count(); i < n; i++) {
      const t = ((await btns.nth(i).innerText().catch(() => '')) || '').replace(/\s/g, '');
      if (/登录/.test(t)) { await btns.nth(i).click(); break; }
    }
    await page.waitForTimeout(6000);
  }
}

/** 关掉盖住全屏的弹窗（首启「投资适当性评估」的 .ant-modal-wrap 拦截一切点击；
 *  「稍后再答」是正当出口，7 天内不再问——与 probe_live_arena_tabs.mjs 同款） */
async function dismissModals() {
  for (let round = 0; round < 8; round++) {
    const blocking = await page.evaluate(
      () => Array.from(document.querySelectorAll('.ant-modal-wrap'))
        .filter((m) => getComputedStyle(m).display !== 'none').length,
    );
    if (!blocking) return true;
    let clicked = false;
    const btns = page.locator('.ant-modal:visible button');
    for (let i = 0, n = await btns.count(); i < n && !clicked; i++) {
      const t = ((await btns.nth(i).innerText().catch(() => '')) || '').replace(/\s/g, '');
      if (/稍后再答|同意|确认|我已知晓|已阅读|知道了|跳过/.test(t)) {
        await btns.nth(i).click().catch(() => {});
        clicked = true;
      }
    }
    if (!clicked) await page.keyboard.press('Escape').catch(() => {});
    await page.waitForTimeout(700);
  }
  return false;
}

/** 切到「盘中实况」栏并按 bg-blue-50 验证真的激活（点下去 ≠ 切栏） */
async function openLiveTab() {
  await page.goto(`${BASE}/#/live`, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(8000);
  await dismissModals();
  if (await page.locator('.eq-fill-mark').first().isVisible().catch(() => false)) return true;
  const btn = page.locator('button').filter({ hasText: /^盘中实况$/ }).first();
  await btn.click({ timeout: 8000 }).catch(async () => {
    await dismissModals();
    await btn.click({ force: true });
  });
  await page.waitForTimeout(9000);
  await dismissModals();
  return page.evaluate(() => {
    const b = Array.from(document.querySelectorAll('button'))
      .find((x) => (x.textContent || '').trim() === '盘中实况');
    return !!b && /bg-blue-50/.test(b.className);
  });
}

await login();
await dismissModals();
const liveActive = await openLiveTab();
record('「盘中实况」栏激活（bg-blue-50）', liveActive === true);
await page.waitForSelector('.eq-fill-mark', { timeout: 30000 }).catch(() => {});
await page.waitForTimeout(2500); // 稳定一帧（图表重绘/动画）

const report = await page.evaluate(() => {
  const marks = [...document.querySelectorAll('path.eq-fill-mark')]
    .filter((el) => el.dataset && (el.dataset.side === 'buy' || el.dataset.side === 'sell'));
  // 折线路径：带 stroke、无渐变填充的 path（Area 垫层 fill=url(#grad) 被排除；
  // 装饰/轴线是 <line> 不是 <path>）。标记自身排除。
  const linePaths = [...document.querySelectorAll('svg path')]
    .filter((p) => !p.classList.contains('eq-fill-mark'))
    .filter((p) => {
      const stroke = p.getAttribute('stroke');
      const fill = p.getAttribute('fill');
      return stroke && stroke !== 'none' && (!fill || fill === 'none' || fill === 'transparent');
    })
    .filter((p) => (p.getTotalLength?.() ?? 0) > 30);
  const samples = linePaths.map((p) => {
    const L = p.getTotalLength();
    const step = Math.max(1.5, L / 3000);
    const arr = [];
    for (let s = 0; s <= L; s += step) { const q = p.getPointAtLength(s); arr.push([q.x, q.y]); }
    return arr;
  });
  const rows = [];
  for (const el of marks) {
    const d = el.getAttribute('d') || '';
    const m = d.match(/^M\s*(-?[\d.]+)[ ,]+(-?[\d.]+)/);
    if (!m) continue;
    const x = parseFloat(m[1]); const y = parseFloat(m[2]);
    let best = Infinity;
    for (const arr of samples) {
      for (const [a, b] of arr) {
        const dd = (a - x) ** 2 + (b - y) ** 2;
        if (dd < best) best = dd;
      }
    }
    rows.push({ side: el.dataset.side, x: +x.toFixed(1), y: +y.toFixed(1), dist: +Math.sqrt(best).toFixed(1) });
  }
  return { marks: rows.length, lines: linePaths.length, rows };
});

record('图上存在折线路径（≥2 条）', report.lines >= 2, `lines=${report.lines}`);
record('成交标记存在且非空（防空图假通过）', report.marks >= 20, `marks=${report.marks}`);
const bad = report.rows.filter((r) => r.dist > MAX_DIST);
record(
  `全部标记尖端在线上（≤${MAX_DIST}px）`,
  report.marks > 0 && bad.length === 0,
  bad.length
    ? `${bad.length}/${report.marks} 枚超限；最差 ${Math.max(...bad.map((r) => r.dist))}px`
    : `max=${Math.max(...report.rows.map((r) => r.dist))}px`,
);
if (bad.length) {
  console.log('  最差 10 枚（side x y dist）：');
  for (const r of bad.sort((a, b) => b.dist - a.dist).slice(0, 10)) {
    console.log(`    ${r.side}  x=${r.x}  y=${r.y}  dist=${r.dist}px`);
  }
}
record('无未捕获 pageerror', pageErrors.length === 0, pageErrors.slice(0, 3).join(' | '));

await page.screenshot({ path: '/tmp/probe_live_fill_marks.png' });
await browser.close();

const failed = results.filter((r) => !r.ok).length;
console.log(`\n${failed ? '✗ FAIL' : '✓ ALL PASS'} (${results.length - failed}/${results.length}) 截图: /tmp/probe_live_fill_marks.png`);
process.exit(failed ? 1 : 0);
