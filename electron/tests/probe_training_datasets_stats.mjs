/**
 * 训练数据集页（机构级重排）验收探针 —— **全程只读**：绝不点「发布此草稿」
 * 「新建草稿」等任何写操作，只导航与断言。
 *
 * 与 vitest 的分工：这里跑真浏览器 + 真后端，验证三条单测覆盖不到的事实——
 *   1. `GET /admin/training-data/fields` 真的内嵌了 `stats` / `stats_meta`
 *      （结构契约：键数=字段数、窗口 n_dates、matched/total）；
 *   2. 真数据下 DOM 的呈现：列组齐全、缺失渲染「—」（未命中统计时）；
 *   3. 深链 `?market&source` 预选生效——同文档 hash 变化（不重载应用）也要
 *      落到预选来源库上（对应页面里 appliedParamsKeyRef 那条 effect 分支）。
 *
 * 用法：node electron/tests/probe_training_datasets_stats.mjs
 *      QM_BASE=http://localhost:3080 node electron/tests/probe_training_datasets_stats.mjs
 */
import { chromium } from 'playwright';

const BASE = process.env.QM_BASE || 'http://localhost:3080';
/** 深链预选用：CN 市场必然存在的第二个来源库（避免预选到默认源上「看不出来」） */
const DEEP_SOURCE = 'l2_factors';
const SHOT = '/tmp/qm_training_datasets_stats.png';

let pass = 0;
let fail = 0;
const ok = (cond, label, extra = '') => {
  if (cond) { pass++; console.log(`  ✓ ${label}${extra ? ` — ${extra}` : ''}`); }
  else { fail++; console.log(`  ✗ ${label}${extra ? ` — ${extra}` : ''}`); }
};

const browser = await chromium.launch({
  executablePath: '/usr/bin/google-chrome-stable',
  args: ['--no-sandbox'],
});
const ctx = await browser.newContext({ viewport: { width: 1760, height: 1000 } });
const page = await ctx.newPage();

const pageErrors = [];
page.on('pageerror', (e) => pageErrors.push(String(e).slice(0, 160)));

// 抓 /fields 响应：页面「看起来有数」不等于后端给了数
let lastFields = null;
page.on('response', (r) => {
  if (r.url().includes('/admin/training-data/fields')) {
    r.json().then((j) => { lastFields = j; }).catch(() => {});
  }
});
const waitFields = (needle) => page.waitForResponse(
  (r) => r.url().includes('/admin/training-data/fields') && r.url().includes(needle),
  { timeout: 60000 },
);

console.log(`\n===== 训练数据集页探针 @ ${BASE} =====`);

// ── 登录 ───────────────────────────────────────────────────────────────
await page.goto(`${BASE}/`, { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(3000);
if ((await page.locator('input[type=password]').count()) > 0) {
  await page.locator('input').nth(0).fill('admin');
  await page.locator('input[type=password]').first().fill('admin123');
  const btns = page.locator('button');
  for (let i = 0; i < (await btns.count()); i++) {
    const t = (await btns.nth(i).innerText().catch(() => '')).replace(/\s/g, '');
    if (/登录/.test(t)) { await btns.nth(i).click(); break; }
  }
  await page.locator('input[type=password]').first().waitFor({ state: 'detached', timeout: 30000 }).catch(() => {});
  await page.waitForTimeout(2500);
}
const later = page.locator('button', { hasText: '稍后再答' });
if (await later.count()) { await later.first().click().catch(() => {}); await page.waitForTimeout(800); }

// ── ① 默认进入（CN / 默认源 l1_factors）：等 /fields 落地 ───────────────
// ⚠️ HashRouter：深链必须写全 `#/`，否则静默落首页。
const [resp0] = await Promise.all([
  waitFields('l1_factors'),
  page.goto(`${BASE}/#/admin/training-datasets`, { waitUntil: 'domcontentloaded' }),
]);
const body0 = await resp0.json();
ok(!!body0.stats_meta, '/fields 响应内嵌 stats_meta');
ok(!!body0.stats && typeof body0.stats === 'object', 'stats 是按列的字典型');
const meta0 = body0.stats_meta || {};
if (meta0.available) {
  const w = meta0.window || {};
  ok(Number.isFinite(w.n_dates) && w.n_dates > 0, `统计窗口 n_dates=${w.n_dates}（${w.start} ~ ${w.end} / ${w.horizon}）`);
  ok(Number.isFinite(meta0.matched) && Number.isFinite(meta0.total) && meta0.matched <= meta0.total,
    `指标匹配 ${meta0.matched}/${meta0.total}，快照 ${meta0.report_date}`);
  const keys = Object.keys(body0.stats);
  ok(keys.length === (body0.fields || []).length,
    `stats 键数=字段数（${keys.length}/${(body0.fields || []).length}）—— 每列都有落点（值可为 null）`);
} else {
  console.log(`  · 统计不可用（available=false，reason=${meta0.reason}）——跳过窗口断言`);
}

// ── ② DOM：等表格出数（antd 首行是测量行，行定位用 tr.ant-table-row）────
await page.waitForFunction(() => document.querySelectorAll('tr.ant-table-row').length > 0,
  { timeout: 30000 });

const thTexts = await page.evaluate(() =>
  [...document.querySelectorAll('th')].map((t) => (t.innerText || '').trim()));
const needed = ['编号', '因子', '子库', '分类', '中文释义', '归属', '质量指标', '数据量',
  'IC', 'ICIR', '换手', '单调性', '胜率', '样本量', '有效天数', '窗口覆盖', '状态', '训练配置'];
const missing = needed.filter((t) => !thTexts.includes(t));
ok(missing.length === 0, '列组/列头齐全（归属/质量指标/数据量三组 + 双侧固定列）',
  missing.length ? `缺 ${missing.join('、')}` : `${thTexts.length} 个 th`);

const pageText = await page.evaluate(() => document.body.innerText);
ok(pageText.includes('线上版本'), '发布状态条：线上段在');
ok(pageText.includes('编辑中') || pageText.includes('未创建'), '发布状态条：草稿段在（编辑中 / 未创建）');
ok(pageText.includes('分区文件') && pageText.includes('已发现字段'), '态势条：数据段在（分区文件 / 已发现字段）');
ok(/质量快照|质量统计/.test(pageText), '态势条：质量段在（质量快照 / 质量统计）');

// 缺失「—」：后端命中数 < 字段数时才可能且必须出现（这是设计口径，不是美化）
const matched0 = Number.isFinite(meta0.matched) ? meta0.matched : -1;
const total0 = Number.isFinite(meta0.total) ? meta0.total : -1;
const dashCells = await page.evaluate(() =>
  [...document.querySelectorAll('tr.ant-table-row td')].filter((td) => (td.innerText || '').trim() === '—').length);
if (total0 > matched0 && total0 > 0) {
  ok(dashCells > 0, '未命中统计的行渲染「—」（不是 0）', `${dashCells} 个「—」单元格`);
} else {
  console.log('  · 当前库全部命中统计，无缺失行可验——「—」断言在无快照库 / 切库后仍由 vitest 覆盖');
}

const rowCount = await page.evaluate(() => document.querySelectorAll('tr.ant-table-row').length);
ok(rowCount > 0, `因子目录有数据行（${rowCount} 行，首屏 50/页）`);

// ── ③ 深链预选：同文档 hash 变化（不重载应用），必须落到预选库 ──────────
// 这条走的是页面里「挂载后参数变化」的 effect 分支（首挂载参数由惰性初始化吃）。
const [resp1] = await Promise.all([
  waitFields(DEEP_SOURCE),
  page.goto(`${BASE}/#/admin/training-datasets?market=CN&source=${DEEP_SOURCE}`, { waitUntil: 'domcontentloaded' }),
]);
const q1 = new URL(resp1.url()).searchParams;
ok(q1.get('source_dataset') === DEEP_SOURCE && q1.get('market') === 'CN',
  `深链预选请求参数 source_dataset=${q1.get('source_dataset')} & market=${q1.get('market')}`);
const body1 = await resp1.json();
ok(!!body1.fields && !!body1.stats, '/fields（预选源）含 fields + stats');

await page.waitForFunction(() => document.querySelectorAll('tr.ant-table-row').length > 0,
  { timeout: 30000 });
const selTexts = await page.evaluate(() =>
  [...document.querySelectorAll('.ant-select-selection-item')].map((e) => (e.textContent || '').trim()));
ok(selTexts.some((t) => /L2/i.test(t)),
  '深链预选：数据源 Select 已切到 L2 库', selTexts.join(' | '));

// 缺失「—」再验一遍预选库（哪个库有缺口就用哪个库验，两个都全命中就如实说跳过）
const meta1 = body1.stats_meta || {};
const dashCells1 = await page.evaluate(() =>
  [...document.querySelectorAll('tr.ant-table-row td')].filter((td) => (td.innerText || '').trim() === '—').length);
if (Number.isFinite(meta1.matched) && meta1.matched < meta1.total) {
  ok(dashCells1 > 0, `未命中统计的行渲染「—」（预选库 ${meta1.matched}/${meta1.total}）`,
    `${dashCells1} 个「—」单元格`);
} else {
  console.log(`  · 预选库统计全命中（${meta1.matched}/${meta1.total}），无缺失行可验`);
}
// 只读纪律：从这里到退出，不再有任何点击

// ── ④ 非 CN 市场（HK）：统计收口，缺失一律「—」不许冒充 0 ──────────────
// CN 十库实测全部命中（无报告库由私域快照兜底也全命中），「—」只有非 CN
// 市场是可复现的真数据场景：stats 全 null，整列必须渲染「—」。
const [resp2] = await Promise.all([
  page.waitForResponse((r) => r.url().includes('/admin/training-data/fields') && r.url().includes('market=HK'),
    { timeout: 60000 }),
  page.goto(`${BASE}/#/admin/training-datasets?market=HK&source=l1_factors`, { waitUntil: 'domcontentloaded' }),
]);
const body2 = await resp2.json();
const meta2 = body2.stats_meta || {};
ok(meta2.available === false && /仅覆盖 A 股/.test(String(meta2.reason || '')),
  '非 CN 市场：stats_meta 收口并给出原因', String(meta2.reason || '').slice(0, 40));
const nonnull2 = Object.values(body2.stats || {}).filter(Boolean).length;
ok(nonnull2 === 0, `HK 库 stats 全空（${nonnull2} 个非空值）`);

await page.waitForFunction(() => document.querySelectorAll('tr.ant-table-row').length > 0,
  { timeout: 30000 });
const hkRows = await page.evaluate(() => document.querySelectorAll('tr.ant-table-row').length);
const hkDash = await page.evaluate(() =>
  [...document.querySelectorAll('tr.ant-table-row td')].filter((td) => (td.innerText || '').trim() === '—').length);
// 每行至少有 8 个统计单元格 + 子库列应为「—」；阈值取行数×5，宽松但仍能否定「渲染成 0」
ok(hkRows > 0 && hkDash >= hkRows * 5,
  `HK 行统计列渲染「—」（${hkRows} 行 / ${hkDash} 个「—」单元格，缺失不是 0）`);
const hkText = await page.evaluate(() => document.body.innerText);
ok(hkText.includes('因子报告仅覆盖 A 股'), '态势条展示原因文案（非 CN 收口）');
ok(selTexts.length > 0 && /港/.test(await page.evaluate(() =>
  [...document.querySelectorAll('.ant-select-selection-item')].map((e) => e.textContent || '').join(' '))),
  '市场 Select 已切到「港股」');

// ── ⑤ 收尾：截图 + JS 错误 ─────────────────────────────────────────────
await page.screenshot({ path: SHOT, fullPage: false });
console.log(`  · 截图 ${SHOT}`);

if (pageErrors.length) {
  fail++;
  console.log(`  ✗ 页面 JS 错误：${[...new Set(pageErrors)].slice(0, 5).join(' | ')}`);
} else {
  pass++;
  console.log('  ✓ 全程无页面 JS 错误');
}

console.log(`\n===== ${pass} 通过 / ${fail} 失败 =====`);
await browser.close();
process.exit(fail === 0 ? 0 : 1);
