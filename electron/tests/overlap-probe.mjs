/** 重叠检测：找出同一容器内垂直方向发生交叠的兄弟元素（排版错乱/重叠 bug） */
import { chromium } from 'playwright';

const BASE = process.env.QM_BASE || 'http://localhost:3080';
const TAB = process.argv[2] || '分析师动向';

const b = await chromium.launch({ executablePath: '/usr/bin/google-chrome-stable', args: ['--no-sandbox'] });
const ctx = await b.newContext({ viewport: { width: 1720, height: 1150 } });
const p = await ctx.newPage();
await p.goto(`${BASE}/`, { waitUntil: 'domcontentloaded', timeout: 40000 });
await p.waitForTimeout(3000);
if ((await p.locator('input[type=password]').count()) > 0) {
  await p.locator('input').nth(0).fill('admin');
  await p.locator('input[type=password]').first().fill('admin123');
  const btns = p.locator('button');
  for (let i = 0; i < (await btns.count()); i++) {
    const t = (await btns.nth(i).innerText().catch(() => '')).replace(/\s/g, '');
    if (/登录|登\s*录/.test(t)) { await btns.nth(i).click(); break; }
  }
  await p.waitForTimeout(7000);
}
const us = p.locator('button[role=radio]', { hasText: '美股' }).first();
if (await us.count()) { await us.click(); await p.waitForTimeout(1500); }
await p.locator('a,button', { hasText: '市场分析' }).first().click({ timeout: 8000 }).catch(() => undefined);
await p.waitForTimeout(9000);
await p.locator('button', { hasText: TAB }).first().click({ timeout: 6000 }).catch(() => undefined);
await p.waitForTimeout(9000);

const report = await p.evaluate(() => {
  const out = [];
  const root = document.querySelector('.overflow-y-auto') || document.body;
  // 对每个容器，检查其直接子元素之间是否垂直交叠
  const walk = (el, depth) => {
    if (depth > 12) return;
    // 内容溢出检测：文本比单元格宽 → 会溢出到相邻列（视觉上就是"重叠/错乱"）
    const ecs = getComputedStyle(el);
    const truncates = ecs.textOverflow === 'ellipsis' || ecs.overflow !== 'visible' || ecs.overflowX !== 'visible';
    if (!truncates && el.clientWidth > 0 && el.scrollWidth > el.clientWidth + 1 && el.children.length === 0) {
      const t = (el.innerText || '').trim();
      if (t && ecs.visibility !== 'hidden') {
        out.push({
          parent: '溢出',
          childA: (el.className || '').toString().slice(0, 40),
          childB: '',
          overlap: el.scrollWidth - el.clientWidth,
          textA: t.slice(0, 26),
          textB: `需 ${el.scrollWidth}px / 实有 ${el.clientWidth}px`,
        });
      }
    }
    const cs = getComputedStyle(el);
    // 只检查纵向布局容器：横向 flex 的兄弟共享垂直空间是正常的
    const isColumn = cs.display === 'block' || (cs.display === 'flex' && cs.flexDirection !== 'row') ||
                     cs.display === 'grid';
    const kids = [...el.children];
    if (!isColumn) { kids.forEach((k) => walk(k, depth + 1)); return; }
    const boxes = kids.map((k) => k.getBoundingClientRect()).filter((r) => r.height > 0 && r.width > 0);
    for (let i = 1; i < boxes.length; i++) {
      const prev = boxes[i - 1];
      const cur = boxes[i];
      const overlap = prev.bottom - cur.top;
      if (overlap > 1.5 && Math.abs(prev.top - cur.top) > 1) {
        out.push({
          parent: (el.className || el.tagName).toString().slice(0, 60),
          childA: (kids[i - 1].className || '').toString().slice(0, 46),
          childB: (kids[i].className || '').toString().slice(0, 46),
          overlap: Math.round(overlap),
          textA: (kids[i - 1].innerText || '').replace(/\s+/g, ' ').slice(0, 30),
          textB: (kids[i].innerText || '').replace(/\s+/g, ' ').slice(0, 30),
        });
      }
    }
    kids.forEach((k) => walk(k, depth + 1));
  };
  walk(root, 0);
  return out;
});

const overflow = report.filter((r) => r.parent === '溢出');
console.log(`== ${TAB} ==`);
if (overflow.length === 0) {
  console.log('  无内容溢出');
} else {
  const uniq = new Map();
  overflow.forEach((r) => {
    if (!uniq.has(r.textA)) uniq.set(r.textA, r);
  });
  console.log(`  内容溢出 ${overflow.length} 处（去重后 ${uniq.size} 类）:`);
  [...uniq.values()].slice(0, 12).forEach((r) => {
    console.log(`   「${r.textA}」 ${r.textB}`);
    console.log(`      单元格: ${r.childA}`);
  });
}
await b.close();
