/**
 * 推理中心「模型治理 · 资产健全性」端到端探针。
 *
 * 这一列的价值全在**准**上：机构用它是为了「一眼看出哪些模型点了也跑不起来」，
 * 一旦误报（把健康模型涂红）或漏报（把死模型标绿），这列就退化成噪声。
 * 所以探针不比文案，做的是 UI ↔ API 的逐项对账：
 *
 *   1. 拦页面自己发的 `GET /api/v1/models`，按 asset_gaps 统计「应为」的缺项数
 *   2. 屏幕「资产缺失」数字必须等于它
 *   3. 表里「缺 N 项」徽章数必须等于 API 里带缺项的行数（CN 共 50 行、单页 50，全覆盖）
 *   4. 「仅看风险项」必须真的收缩结果集，且收缩后不留健康行
 */
import { chromium } from 'playwright';

const BASE = 'http://localhost:3080';

const browser = await chromium.launch({
  executablePath: '/usr/bin/google-chrome-stable',
  args: ['--no-sandbox'],
});
const page = await browser.newPage({ viewport: { width: 1760, height: 1000 } });
const logs = [];
page.on('pageerror', (e) => logs.push('[PAGEERROR] ' + (e.message || '').slice(0, 300)));

// 全程收集模型列表响应，最后一条即页面当前渲染所用的那份。
// 用 waitForResponse 单次等待会漏（请求可能在钩子装上之前就回来了，
// 或页面复用了缓存根本不重发），表现为 api=0 的假失败。
const modelsBodies = [];
page.on('response', async (r) => {
  if (r.request().method() !== 'GET' || !/\/models(\?|$)/.test(r.url())) return;
  try {
    const raw = await r.json();
    modelsBodies.push({ url: r.url(), items: raw?.data?.items ?? raw?.items ?? [] });
  } catch {
    /* 非 JSON 响应（如 HTML 兜底页）忽略 */
  }
});

let failures = 0;
const check = (name, ok, detail = '') => {
  console.log(`${ok ? '[PASS]' : '[FAIL]'} ${name}${detail ? ' :: ' + detail : ''}`);
  if (!ok) failures += 1;
};

await page.goto(`${BASE}/#/auth/login`, { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(3500);
const inputs = page.locator('input');
if ((await inputs.count()) >= 2) {
  await inputs.nth(0).fill('admin');
  await inputs.nth(1).fill('admin123');
  const btns = page.locator('button');
  for (let i = 0, n = await btns.count(); i < n; i++) {
    const t = ((await btns.nth(i).innerText().catch(() => '')) || '').replace(/\s/g, '');
    if (/登录/.test(t)) { await btns.nth(i).click(); break; }
  }
  await page.waitForTimeout(5000);
}

await page.goto(`${BASE}/#/inference-center`, { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(6000);

for (let round = 0; round < 4; round++) {
  const modal = page.locator('.ant-modal:visible');
  if ((await modal.count()) === 0) break;
  const mbtns = modal.first().locator('button');
  let clicked = false;
  for (let i = 0, n = await mbtns.count(); i < n; i++) {
    const t = ((await mbtns.nth(i).innerText().catch(() => '')) || '').replace(/\s/g, '');
    if (/同意|确认|我知道了|已阅读|知道了/.test(t)) {
      await mbtns.nth(i).scrollIntoViewIfNeeded().catch(() => {});
      await mbtns.nth(i).click({ timeout: 5000 }).catch(async () => {
        await mbtns.nth(i).click({ force: true }).catch(() => {});
      });
      clicked = true;
      break;
    }
  }
  if (!clicked) await page.keyboard.press('Escape').catch(() => {});
  await page.waitForTimeout(1200);
}
await page.waitForTimeout(9000);

// ── API 侧真值 ────────────────────────────────────────────────
// 治理面板消费的是 InferenceCenterShell 传给它的 registeredModels，
// 即页面上最后一次 /models 响应；用最后一条而非第一条，避免对上旧数据。
const last = modelsBodies[modelsBodies.length - 1] ?? { url: '(未拦到)', items: [] };
const apiModels = last.items;
check(
  '拦到模型列表响应',
  apiModels.length > 0,
  `items=${apiModels.length} url=${String(last.url).slice(-70)}`,
);
const apiWithGaps = apiModels.filter((m) => (m.asset_gaps || []).length > 0);
const apiGapKinds = {};
for (const m of apiWithGaps) {
  const k = (m.asset_gaps || []).join('+');
  apiGapKinds[k] = (apiGapKinds[k] || 0) + 1;
}
console.log(`[info] API 侧：${apiWithGaps.length}/${apiModels.length} 行带缺项 ${JSON.stringify(apiGapKinds)}`);

// ── 切到「模型治理」工作区 ────────────────────────────────────
const tabs = page.locator('nav[aria-label="推理中心工作区"] button');
let switched = false;
for (let i = 0, n = await tabs.count(); i < n; i++) {
  const t = ((await tabs.nth(i).innerText().catch(() => '')) || '').replace(/\s/g, '');
  if (t.includes('模型治理')) { await tabs.nth(i).click(); switched = true; break; }
}
check('存在「模型治理」工作区', switched);
await page.waitForTimeout(2500);

const summary = page.locator('[data-testid="governance-summary"]');
const table = page.locator('[data-testid="governance-table"]');
check('体检摘要条已渲染', (await summary.count()) > 0);
check('治理表已渲染', (await table.count()) > 0);

// ── 对账 1：摘要里的「资产缺失」== API 真值 ───────────────────
// 读 DOM 而非整条文案：摘要里紧跟其后就是「N 个模型点了也跑不起来」徽章，
// 抹掉空白后两个数字会连成一个（23 + 23 → 2323），正则必然读错。
const gapStat = summary.locator('span', { hasText: '资产缺失' }).first();
const gapStatText = ((await gapStat.locator('strong').first().innerText().catch(() => '')) || '').trim();
check('摘要条含「资产缺失」计数', /^\d+$/.test(gapStatText), `读到 "${gapStatText}"`);
if (/^\d+$/.test(gapStatText)) {
  check(
    '资产缺失数 == API 带缺项行数',
    Number(gapStatText) === apiWithGaps.length,
    `UI=${gapStatText} API=${apiWithGaps.length}`,
  );
}

// 徽章文案由 JSX 的 `缺 {n} 项` 渲染出空格，比对前统一抹掉
const badgeText = ((await table.first().innerText().catch(() => '')) || '').replace(/\s+/g, '');
const gapBadges = (badgeText.match(/缺\d+项/g) || []).length;
const okBadges = (badgeText.match(/健全/g) || []).length;
console.log(`[info] 表内徽章：缺项 ${gapBadges} / 健全 ${okBadges}`);

check('表内出现「缺 N 项」徽章', gapBadges > 0, `count=${gapBadges}`);
check('表内出现「健全」徽章', okBadges > 0, `count=${okBadges}`);
check(
  '缺项徽章数 == API 带缺项行数（单页需覆盖全部行）',
  apiModels.length <= 50 ? gapBadges === apiWithGaps.length : gapBadges > 0,
  `badges=${gapBadges} api=${apiWithGaps.length} rows=${apiModels.length}`,
);

// 徽章上的 N 必须与 API 的缺项个数分布一致：只允许出现 API 里出现过的 N
const badgeNs = new Set((badgeText.match(/缺(\d+)项/g) || []).map((s) => Number(s.replace(/\D/g, ''))));
const apiNs = new Set(apiWithGaps.map((x) => x.asset_gaps.length));
const nsMatch = [...badgeNs].every((n) => apiNs.has(n));
check('徽章缺项个数均在 API 分布内', nsMatch, `UI N=${[...badgeNs]} API N=${[...apiNs]}`);

// ── 对账 2：「仅看风险项」必须真的收缩 ────────────────────────
const rowsBefore = await table.locator('tbody tr.ant-table-row').count();
const riskBtn = page.locator('button', { hasText: '仅看风险项' });
check('存在「仅看风险项」开关', (await riskBtn.count()) > 0);
if ((await riskBtn.count()) > 0) {
  await riskBtn.first().click();
  await page.waitForTimeout(1200);
  const rowsAfter = await table.locator('tbody tr.ant-table-row').count();
  const afterText = ((await table.first().innerText().catch(() => '')) || '').replace(/\s+/g, '');
  const gapAfter = (afterText.match(/缺\d+项/g) || []).length;
  const okAfter = (afterText.match(/健全/g) || []).length;

  check(
    '筛选后行数严格减少（非空集，防假通过）',
    rowsBefore > 0 && rowsAfter < rowsBefore,
    `before=${rowsBefore} after=${rowsAfter}`,
  );
  // 空结果集也算「没有健康行」——必须先要求非空，否则关掉筛选或渲染失败都判通过
  check('筛选后不残留健康行（且结果非空）', okAfter === 0 && rowsAfter > 0, `健全徽章=${okAfter} rows=${rowsAfter}`);
  check('筛选后每行都是风险项', rowsAfter > 0 && gapAfter > 0, `缺项徽章=${gapAfter} rows=${rowsAfter}`);
}

console.log('[ERRORS]', logs.length ? logs.slice(0, 5) : 'none');
console.log(failures === 0 ? '[RESULT] ALL PASS' : `[RESULT] ${failures} FAILED`);
await browser.close();
process.exit(failures === 0 ? 0 : 1);
