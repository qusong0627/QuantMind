/**
 * 推理中心右栏几何探针 —— 量「挤压」到底发生在哪。
 *
 * 默认打 3000（本仓库自己的 vite dev，`VITE_PORT=3000 npx vite`），源码改动立刻可见，
 * 不必先跑 deploy_frontend.sh。⚠️ **别用 5173** —— 那是另一个项目（inkwell）的 vite，
 * 落点会是一块手写板而不是本应用。
 *
 * 测量口径：每个关键区块取 getBoundingClientRect + 是否发生换行/溢出，
 * 不做像素级视觉判断 —— 「挤压」要落到具体数字上才好改。
 */
import { chromium } from 'playwright';

const BASE = process.env.PROBE_BASE || 'http://localhost:3000';
const WIDTHS = [1440, 1680, 1920, 2560];

const browser = await chromium.launch({
  executablePath: '/usr/bin/google-chrome-stable',
  args: ['--no-sandbox'],
});
const page = await browser.newPage({ viewport: { width: 1920, height: 1080 } });
const errs = [];
page.on('pageerror', (e) => errs.push('[PAGEERROR] ' + (e.message || '').slice(0, 200)));

await page.goto(`${BASE}/#/auth/login`, { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(4000);
// 只看可见输入框：dev 版页面顶部有个隐藏的 file input（移动端拍照上传），
// 按 nth(0) 取会拿到它然后超时。
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

await page.goto(`${BASE}/#/inference-center`, { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(5000);

// 首访合规弹窗
for (let round = 0; round < 4; round++) {
  const modal = page.locator('.ant-modal:visible');
  if ((await modal.count()) === 0) break;
  const mbtns = modal.first().locator('button');
  let clicked = false;
  for (let i = 0, n = await mbtns.count(); i < n; i++) {
    const t = ((await mbtns.nth(i).innerText().catch(() => '')) || '').replace(/\s/g, '');
    if (/同意|确认|我知道了|已阅读|知道了/.test(t)) {
      await mbtns.nth(i).click({ timeout: 5000 }).catch(() => {});
      clicked = true; break;
    }
  }
  if (!clicked) await page.keyboard.press('Escape').catch(() => {});
  await page.waitForTimeout(1200);
}
await page.waitForTimeout(8000);

// 落点自检：量不到任何东西时先确认「人到底在哪一页」
{
  const dump = await page.evaluate(() => ({
    url: location.href,
    hash: location.hash,
    text: (document.body.innerText || '').replace(/\n+/g, ' | ').slice(0, 500),
    inputs: [...document.querySelectorAll('input')]
      .filter((el) => el.getBoundingClientRect().width > 0).length,
  }));
  console.log('[落点]', JSON.stringify(dump, null, 1));
}

/** 触发一次个股推理：优先点左栏排名行首行，失败退回输入框 + 回车 */
async function pickFirstRank() {
  for (let i = 0; i < 5; i++) {
    const row = page.locator('text=绿地控股').first();
    if (await row.count()) {
      await row.click({ timeout: 4000 }).catch(() => {});
      await page.waitForTimeout(12000);
      if (await page.locator('text=模型推理指标').count()) return 'row';
    }
    await page.waitForTimeout(2500);
  }
  const box = page.locator('input[placeholder*="SH"], input[placeholder*="代码"], input[placeholder*="股票"]').first();
  await box.click({ timeout: 5000 }).catch(() => {});
  await box.fill('SH600036').catch(() => {});
  await page.keyboard.press('Enter');
  await page.waitForTimeout(14000);
  return 'input';
}

const measure = async () => page.evaluate(() => {
  const g = (sel) => {
    const el = document.querySelector(sel);
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return {
      w: Math.round(r.width), h: Math.round(r.height),
      x: Math.round(r.left), right: Math.round(r.right),
    };
  };
  /**
   * 工具条折了几行。不能数 `top` 的不同值 —— 外层是 `items-center`，
   * 高矮不一的子元素天然顶边不齐（h-8 的按钮 vs h-5 的分隔线），
   * 会把单行误判成多行。改数「垂直中心」的聚类数，并用行高交叉校验。
   */
  const barRows = (el) => {
    if (!el) return null;
    const centers = [...el.children].map((c) => {
      const r = c.getBoundingClientRect();
      return Math.round(r.top + r.height / 2);
    });
    const uniq = [];
    centers.forEach((c) => {
      if (!uniq.some((u) => Math.abs(u - c) <= 6)) uniq.push(c);
    });
    return { children: centers.length, rows: uniq.length, barH: Math.round(el.getBoundingClientRect().height) };
  };
  // 找出所有被水平压扁（内容宽 > 容器宽）的元素
  const squeezed = [];
  document.querySelectorAll('div,span.h-full,section').forEach((el) => {
    if (el.scrollWidth > el.clientWidth + 2 && el.clientWidth > 0
        && el.getBoundingClientRect().width > 60
        && el.getBoundingClientRect().height > 16) {
      squeezed.push({
        cls: (el.className || '').toString().slice(0, 90),
        w: Math.round(el.getBoundingClientRect().width),
        over: el.scrollWidth - el.clientWidth,
      });
    }
  });
  // 右栏根：class 里同时有 flex-1 min-w-0 flex flex-col bg-white rounded-xl
  const wb = [...document.querySelectorAll('div.flex-1.min-w-0.flex.flex-col')]
    .find((el) => el.className.includes('bg-white') && el.className.includes('rounded-xl'));
  // 结果区里每个直接子卡片（K线卡 / 指标卡 / 分数曲线 / 归因 / 共识）
  const cards = [];
  const resultArea = wb && [...wb.children].find((c) => c.className.includes('overflow-y-auto'));
  if (resultArea) {
    const inner = resultArea.firstElementChild;             // flex flex-col gap-3
    const walk = (root, depth) => {
      [...root.children].forEach((el) => {
        const r = el.getBoundingClientRect();
        const cls = (el.className || '').toString();
        if (cls.includes('grid') || cls.includes('bg-white')) {
          cards.push({
            d: depth, w: Math.round(r.width), h: Math.round(r.height),
            cls: cls.slice(0, 70),
            text: (el.innerText || '').replace(/\n+/g, '/').slice(0, 34),
          });
        }
        if (depth < 1) walk(el, depth + 1);
      });
    };
    if (inner) walk(inner, 0);
  }
  const sep = document.querySelector('[role="separator"][aria-label="调整排名榜宽度"]');
  const railEl = sep ? sep.previousElementSibling : null;
  return {
    viewport: window.innerWidth,
    rail: railEl ? (() => { const r = railEl.getBoundingClientRect(); return { w: Math.round(r.width), h: Math.round(r.height) }; })() : null,
    railWidthSaved: localStorage.getItem('qm:inference-center:rail-width'),
    workbench: wb ? (() => { const r = wb.getBoundingClientRect(); return { w: Math.round(r.width), h: Math.round(r.height) }; })() : null,
    toolbar: barRows(wb && wb.children[0]),
    consensusBar: g('[data-testid="consensus-picker"]'),
    cards,
    squeezed: squeezed.slice(0, 10),
  };
});

/** 拖分隔条：验证「右栏能不能按用户的意愿变大」，以及落盘/复位是否成立 */
async function dragSplit(dx) {
  const sep = page.locator('[role="separator"][aria-label="调整排名榜宽度"]');
  const box = await sep.boundingBox();
  if (!box) return 'no-handle';
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width / 2 + dx, box.y + box.height / 2, { steps: 12 });
  await page.mouse.up();
  await page.waitForTimeout(1200);
  return 'ok';
}

for (const w of WIDTHS) {
  await page.setViewportSize({ width: w, height: 1080 });
  await page.waitForTimeout(2500);
  if (w === WIDTHS[0]) console.log('[选股方式]', await pickFirstRank());
  const m = await measure();
  console.log(`\n===== ${w}px =====`);
  console.log('rail        ', JSON.stringify(m.rail), 'saved=', m.railWidthSaved);
  console.log('workbench   ', JSON.stringify(m.workbench));
  console.log('toolbar     ', JSON.stringify(m.toolbar));
  console.log('consensusBar', JSON.stringify(m.consensusBar));
  m.cards.forEach((c) => console.log(`  card d${c.d} ${String(c.w).padStart(5)}x${String(c.h).padStart(4)}  ${c.text}`));
  console.log('squeezed    ', JSON.stringify(m.squeezed, null, 1));
  await page.screenshot({ path: `/tmp/ic_layout_${w}.png` });
}

// ── 分隔条：拖窄 → 复查右栏是否真变大；再双击复位 ────────────
await page.setViewportSize({ width: 1440, height: 1080 });
await page.waitForTimeout(1500);
console.log('\n===== 拖动分隔条 =====');
console.log('拖前  ', JSON.stringify((await measure()).rail));
console.log('drag  ', await dragSplit(-140));
const afterDrag = await measure();
console.log('拖后  ', JSON.stringify(afterDrag.rail), 'saved=', afterDrag.railWidthSaved);
console.log('窄栏下溢出', JSON.stringify(afterDrag.squeezed));
await dragSplit(400);
console.log('拖到上限', JSON.stringify((await measure()).rail));
const sep = page.locator('[role="separator"][aria-label="调整排名榜宽度"]');
await sep.dblclick().catch(() => {});
await page.waitForTimeout(1200);
const afterReset = await measure();
console.log('双击复位', JSON.stringify(afterReset.rail), 'saved=', afterReset.railWidthSaved);
await page.screenshot({ path: '/tmp/ic_layout_split.png' });

console.log('\n[ERRORS]', errs.length ? errs.join(' || ') : 'none');
await browser.close();
