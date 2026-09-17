/**
 * 首页六宫格响应式探针：量各窗口宽度下「顶部市场切换 + 六宫格」的实际表现
 *
 * 关注点：
 *  1. 市场切换器是否被裁（父容器 overflow-hidden + nowrap 时的典型症状）
 *  2. 六宫格列数与卡片宽度（写死 3 列时窄窗口会被压扁）
 *  3. 卡片内容是否溢出（scrollWidth > clientWidth）
 *
 * 用法：node electron/tests/probe_dashboard_responsive.mjs [--shot]
 */
import { chromium } from 'playwright';

const BASE = process.env.QM_BASE || 'http://localhost:3080';
const SHOT = process.argv.includes('--shot');

const VIEWPORTS = [
  { width: 1920, height: 1080 },
  { width: 1600, height: 900 },
  { width: 1440, height: 900 },
  { width: 1280, height: 800 },
  { width: 1100, height: 800 },
  { width: 900, height: 800 },
];

const browser = await chromium.launch({
  executablePath: '/usr/bin/google-chrome-stable',
  args: ['--no-sandbox'],
});
const ctx = await browser.newContext({ viewport: VIEWPORTS[0] });
const page = await ctx.newPage();

// 登录
await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(3000);
if ((await page.locator('input[type=password]').count()) > 0) {
  await page.locator('input').nth(0).fill('admin');
  await page.locator('input[type=password]').first().fill('admin123');
  const btns = page.locator('button');
  for (let i = 0; i < (await btns.count()); i++) {
    const t = (await btns.nth(i).innerText().catch(() => '')).replace(/\s/g, '');
    if (/登录/.test(t)) { await btns.nth(i).click(); break; }
  }
  await page.waitForTimeout(7000);
}
// 首启投资适当性评估弹窗：不关掉会遮住整页
const later = page.locator('button', { hasText: '稍后再答' });
if (await later.count()) { await later.first().click().catch(() => {}); await page.waitForTimeout(800); }
await page.waitForTimeout(3000);

for (const vp of VIEWPORTS) {
  await page.setViewportSize(vp);
  await page.waitForTimeout(1200);
  const m = await page.evaluate(() => {
    const out = { doc: {}, header: {}, grid: {}, cards: [] };
    out.doc = {
      innerWidth: window.innerWidth,
      scrollWidth: document.documentElement.scrollWidth,
      hOverflow: document.documentElement.scrollWidth > window.innerWidth + 1,
    };
    // 市场切换器（role=radiogroup）
    const rg = document.querySelector('[role=radiogroup][aria-label=市场切换]');
    if (rg) {
      const r = rg.getBoundingClientRect();
      const parent = rg.closest('.overflow-hidden') || rg.parentElement;
      const pr = parent.getBoundingClientRect();
      out.header = {
        left: Math.round(r.left), right: Math.round(r.right), width: Math.round(r.width),
        parentRight: Math.round(pr.right), parentScrollW: parent.scrollWidth, parentClientW: parent.clientWidth,
        clippedPx: Math.round(Math.max(0, r.right - pr.right)),
        allVisible: r.left >= pr.left - 1 && r.right <= pr.right + 1,
      };
    }
    // 六宫格容器：找包含 6 个直接子元素的 grid
    // 顶部左侧控件簇：哪些控件被 overflow-hidden 裁掉
    const cluster = document.querySelector('.console-header-grid') ||
      [...document.querySelectorAll('div')].find((d) => d.className.includes?.('flex-nowrap') && d.className.includes('overflow-hidden'));
    if (cluster) {
      const cr = cluster.getBoundingClientRect();
      out.header.clusterRight = Math.round(cr.right);
      out.header.children = [...cluster.children].map((ch) => {
        const r = ch.getBoundingClientRect();
        const label = (ch.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 14) ||
          ch.querySelector('[aria-label]')?.getAttribute('aria-label') || ch.className.slice(0, 20);
        return {
          label,
          clipped: Math.round(Math.max(0, r.right - cr.right)),
          width: Math.round(r.width),
        };
      });
    }
    const grids = [...document.querySelectorAll('div.grid')].filter((d) => d.children.length >= 6);
    const g = grids[0];
    if (g) {
      const cs = getComputedStyle(g);
      const rect = g.getBoundingClientRect();
      out.grid = {
        cols: cs.gridTemplateColumns.split(' ').length,
        template: cs.gridTemplateColumns,
        autoRows: cs.gridAutoRows,
        width: Math.round(rect.width), height: Math.round(rect.height),
        scrollH: g.scrollHeight, clientH: g.clientHeight,
        vScroll: g.scrollHeight > g.clientHeight + 1,
      };
      out.cards = [...g.children].map((c) => {
        const r = c.getBoundingClientRect();
        const inner = c.querySelector('.panel-card') || c;
        const body = inner.querySelector('.panel-body');
        const title = inner.querySelector('.panel-title');
        return {
          w: Math.round(r.width), h: Math.round(r.height),
          x: Math.round(r.x),
          title: title ? title.textContent.trim().slice(0, 12) : '?',
          overX: body ? body.scrollWidth - body.clientWidth : 0,
          bodyOverflowX: body ? body.scrollWidth > body.clientWidth + 1 : false,
        };
      });
    }
    return out;
  });
  console.log(`\n===== ${vp.width}x${vp.height} =====`);
  console.log(`  文档横向溢出: ${m.doc.hOverflow ? '✗ 溢出' : '✓ 无'} (scrollW=${m.doc.scrollWidth})`);
  if (m.header.allVisible !== undefined) {
    console.log(
      `  市场切换器: right=${m.header.right} 父容器right=${m.header.parentRight}` +
      ` 裁切=${m.header.clippedPx}px ${m.header.allVisible ? '✓ 完整可见' : '✗ 被裁'}`,
    );
  } else {
    console.log('  市场切换器: 未找到');
  }
  console.log(`  六宫格: 列数=${m.grid.cols} 宽=${m.grid.width} 高=${m.grid.height} 纵向滚动=${m.grid.vScroll ? '是' : '否'}`);
  console.log(`    列宽: [${m.cards.map((c) => c.w).join(', ')}]`);
  console.log(`    行高: [${m.cards.map((c) => c.h).join(', ')}]`);
  const bad = m.cards.filter((c) => c.bodyOverflowX);
  if (bad.length) {
    console.log(`    卡内容横向溢出: ✗`);
    bad.forEach((c) => console.log(`      「${c.title}」 溢出 ${c.overX}px (卡宽 ${c.w})`));
  } else {
    console.log('    卡内容横向溢出: ✓ 无');
  }
  if (m.header.children) {
    const clipped = m.header.children.filter((c) => c.clipped > 0);
    console.log(`  顶部左簇 right=${m.header.clusterRight}`);
    m.header.children.forEach((c) =>
      console.log(`    [${c.label}] w=${c.width}${c.clipped > 0 ? ` ✗ 裁 ${c.clipped}px` : ' ✓'}`));
  }
  if (SHOT) {
    await page.screenshot({ path: `/tmp/qm_resp_${vp.width}.png` });
  }
}
await browser.close();
