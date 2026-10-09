/**
 * 量 arena 各页面的**横向溢出**：多列卡片行最右一列被容器右沿切掉（用户 2026-09-22 反馈
 * 「右侧框都显示全 别留空白」）。逐面输出 root 的 scrollWidth/clientWidth，并列出右边缘
 * 超出容器的元素（含它的父链，便于定位是哪一层 grid 撑破了）。
 */
import { chromium } from 'playwright';
const BASE = process.env.PROBE_BASE || 'http://localhost:3080';
const browser = await chromium.launch({ executablePath: '/usr/bin/google-chrome-stable', args: ['--no-sandbox'] });
const page = await browser.newPage({ viewport: { width: 1680, height: 1000 } });
const text = async (loc) => ((await loc.innerText().catch(() => '')) || '').replace(/\s/g, '');
const clickByText = async (re, scope = 'button') => {
  const items = page.locator(scope);
  for (let i = 0, n = await items.count(); i < n; i++) {
    if (re.test(await text(items.nth(i)))) { await items.nth(i).click({ timeout: 5000 }).catch(() => {}); return true; }
  }
  return false;
};
const dismiss = async () => {
  for (let r = 0; r < 8; r++) {
    const n = await page.evaluate(() => Array.from(document.querySelectorAll('.ant-modal-wrap')).filter((m) => getComputedStyle(m).display !== 'none').length);
    if (!n) return;
    if (!(await clickByText(/稍后再答|同意|确认|我知道了|已阅读|知道了|跳过/, '.ant-modal:visible button'))) break;
    await page.waitForTimeout(700);
  }
};
await page.goto(`${BASE}/#/auth/login`, { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(4000);
const inputs = page.locator('input:visible');
if ((await inputs.count()) >= 2) {
  await inputs.nth(0).fill('admin'); await inputs.nth(1).fill('admin123');
  await clickByText(/登录/); await page.waitForTimeout(6000);
}
await dismiss();
await page.goto(`${BASE}/#/live`, { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(8000);
await dismiss();

const scan = async (label) => {
  const m = await page.evaluate(() => {
    const root = document.querySelector('.qm-arena-root');
    if (!root) return { found: false };
    const rr = root.getBoundingClientRect();
    const right = rr.right - parseFloat(getComputedStyle(root).paddingRight || '0');
    const out = [];
    root.querySelectorAll('*').forEach((el) => {
      const r = el.getBoundingClientRect();
      if (r.width === 0 || r.height === 0) return;
      const over = Math.round(r.right - right);
      if (over > 2) {
        // 只看「自己没溢出、但被父级/容器切掉」的：找最近的滚动/裁剪祖先
        const path = [];
        let p = el.parentElement;
        while (p && p !== root) {
          const cs = getComputedStyle(p);
          if (/auto|scroll|hidden/.test(cs.overflowX)) { path.push(`${p.tagName}.${(p.className || '').toString().split(' ').slice(0, 2).join('.')}(ovX=${cs.overflowX})`); break; }
          p = p.parentElement;
        }
        out.push({
          tag: el.tagName,
          cls: (el.className || '').toString().slice(0, 48),
          over,
          w: Math.round(r.width),
          txt: (el.innerText || '').replace(/\s+/g, ' ').slice(0, 34),
          clip: path[0] || '无裁剪祖先',
        });
      }
    });
    return {
      found: true,
      rootW: Math.round(rr.width),
      clientW: root.clientWidth,
      scrollW: root.scrollWidth,
      horizontalOverflow: root.scrollWidth - root.clientWidth,
      offenders: out.slice(0, 12),
      total: out.length,
      // 三列卡片行：每张卡的可用宽 vs 表实际需要宽，并列出被省略号截断的单元格
      mkTables: Array.from(root.querySelectorAll('.mk-table')).map((w) => {
        const t = w.querySelector('table');
        const cut = [];
        if (t) {
          t.querySelectorAll('td,th').forEach((c) => {
            if (c.scrollWidth > c.clientWidth + 1) cut.push(`${c.innerText.trim().slice(0, 12)}(${c.scrollWidth}>${c.clientWidth})`);
          });
        }
        // 每一列**实际需要多宽**（该列所有单元格 scrollWidth 的最大值，含 padding）：
        // 定宽列宽要调的百分比就是这个数 ÷ 表宽，别再靠肉眼猜。
        const cols = [];
        if (t) {
          const head = Array.from(t.querySelectorAll('thead th')).map((c) => c.innerText.trim());
          for (const tr of t.querySelectorAll('tr')) {
            Array.from(tr.children).forEach((c, i) => {
              const name = head[i] || `#${i + 1}`;
              cols[i] = cols[i] || { name, need: 0, got: 0 };
              cols[i].need = Math.max(cols[i].need, c.scrollWidth);
              cols[i].got = Math.max(cols[i].got, c.clientWidth);
            });
          }
        }
        return {
          card: Math.round(w.getBoundingClientRect().width),
          need: t ? Math.round(t.scrollWidth) : null,
          cut: cut.slice(0, 6),
          head: (w.querySelector('.mk-head')?.innerText || '').replace(/\s+/g, ' ').slice(0, 22),
          cols,
        };
      }),
    };
  });
  console.log(`\n===== ${label}`);
  if (!m.found) { console.log('  没找到 .qm-arena-root'); return; }
  console.log(`  root 可视宽 ${m.clientW} / 内容宽 ${m.scrollWidth} → 横向溢出 ${m.horizontalOverflow}px；越界元素 ${m.total} 个`);
  for (const t of m.mkTables ?? []) {
    console.log(`   [卡] ${t.head} 可用 ${t.card}px / 表需 ${t.need}px → 差 ${t.need - t.card}px${t.cut?.length ? ` ⚠️ 省略号截断: ${t.cut.join(' ')}` : ''}`);
    if (process.env.PROBE_COLS && t.cols?.some((c) => c && c.need > c.got + 1)) {
      t.cols.forEach((c, i) => {
        if (!c) return;
        console.log(`        col${i + 1} ${c.name}: 需 ${c.need} 得 ${c.got}${c.need > c.got + 1 ? '  ← 差 ' + (c.need - c.got) : ''}`);
      });
    }
  }
  for (const o of m.offenders) {
    console.log(`   · ${o.tag}.${o.cls} 右溢 ${o.over}px 宽${o.w} 裁剪祖先=${o.clip} 「${o.txt}」`);
  }
};

await dismiss();
await page.locator('button').filter({ hasText: /^设置$/ }).first().click({ timeout: 8000 }).catch(() => {});
await page.waitForTimeout(4000);
for (const label of ['总控', '数据']) {
  await dismiss();
  await clickByText(new RegExp(`^${label}$`));
  await page.waitForTimeout(13000);
  await scan(`设置 → ${label}`);
}
// 模型详情下钻 + 总控的第二个页签：都在 `.qm-arena-root` 里，但换的是另一棵子树，
// 不点开就扫不到（下钻那条曾经是「点得开但排版出卡」的高风险面）。
// 先下钻再切页签：下钻要的是**刚进总控**那份已加载好的表格，切走再切回来得等重新拉数。
// 注意上面那个 for 循环跑完停在「数据」页签上，得先点回总控，否则找不到模型行。
await dismiss();
await clickByText(/^总控$/);
await page.waitForTimeout(6000);
await dismiss();
const drilled = await page.evaluate(() => {
  const tr = document.querySelector('.qm-arena-root tr.clickable');
  if (!tr) return false;
  tr.click();
  return true;
});
if (drilled) {
  await page.waitForTimeout(9000);
  await scan('总控 下钻 → 模型详情');
  await page.evaluate(() => document.querySelector('.qm-arena-root button.mdp-back')?.click());
  await page.waitForTimeout(7000);
} else {
  console.log('\n===== 总控 下钻 → 模型详情\n  ⚠️ 没找到可点击的模型行，这一面没扫到');
}
await dismiss();
await clickByText(/^交易所设置$/);
await page.waitForTimeout(10000);
await scan('设置 → 总控 → 交易所设置');
await dismiss();
for (const [tab, wait] of [['行情回测', 12000], ['实况', 14000], ['智能体交易', 12000]]) {
  await dismiss();
  const btn = page.locator('button').filter({ hasText: new RegExp(`^${tab}$`) }).first();
  await btn.click({ timeout: 8000 }).catch(() => btn.click({ force: true }));
  await page.waitForTimeout(wait);
  await scan(tab);
}

// 「关于」2026-09-23 起在设置页里（侧栏不再有这一栏）—— 先点设置，再点面板按钮
await dismiss();
await clickByText(/^设置$/);
await page.waitForTimeout(6000);
await dismiss();
await clickByText(/^关于$/);
await page.waitForTimeout(6000);
await scan('设置 → 关于');
await browser.close();
