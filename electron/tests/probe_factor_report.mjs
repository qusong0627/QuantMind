/**
 * 因子报告（机构级）验收探针 —— 按真实用户路径点一遍五个页签并核对数据。
 *
 * 与纯 DOM 断言的区别：本探针**同时抓 `/detail` 的 JSON 响应**。页面把某块渲染成
 * 空图还是渲染成有数据的图，从截图上分不出「没有数据」与「图坏了」；而 JSON 里
 * `available:false` + `reason` 与「字段缺失」是两回事。故两条路都走：
 *   - JSON：值对不对（可交易轨 ≠ 理想轨、成本敏感性有值、容量带假设）
 *   - DOM ：渲染对不对（页签齐、每张图有尺寸、指标环有弧）
 *
 * 用法：node electron/tests/probe_factor_report.mjs [factor]
 *      QM_BASE=http://localhost:3080 node electron/tests/probe_factor_report.mjs a158_ROC20
 */
import { chromium } from 'playwright';

const BASE = process.env.QM_BASE || 'http://localhost:3080';
const FACTOR = process.argv[2] || null;
const TABS = ['概览', 'IC', '分组回测', '相对基准超额', '风格相关性数值汇总'];

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
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
const page = await ctx.newPage();

const pageErrors = [];
page.on('pageerror', (e) => pageErrors.push(String(e).slice(0, 160)));

// 抓明细响应：页面上「看起来有图」不等于「后端给了数」
let detail = null;
let detailCount = 0;
const lastUrls = [];
page.on('response', (r) => {
  if (r.url().includes('/factor-report/detail')) {
    detailCount++;
    lastUrls.push(new URL(r.url()).search.replace(/^[?]?/, '').slice(0, 120));
    if (lastUrls.length > 4) lastUrls.shift();
    r.json().then((j) => { detail = j; }).catch(() => {});
  }
});

const chartStats = () => page.evaluate(() => {
  const els = [...document.querySelectorAll('[_echarts_instance_]')];
  const zero = [];
  els.forEach((el) => {
    const c = el.querySelector('canvas');
    const r = el.getBoundingClientRect();
    if (!c || r.width < 40 || r.height < 40 || c.width === 0 || c.height === 0) {
      zero.push({ w: Math.round(r.width), h: Math.round(r.height), cw: c?.width ?? 0 });
    }
  });
  return { total: els.length, zero };
});

const ringLabels = () => page.evaluate(() =>
  [...document.querySelectorAll('svg[role="img"][aria-label]')].map((s) => s.getAttribute('aria-label')));

const clickTab = async (label) => {
  await page.locator('button', { hasText: new RegExp(`^${label}$`) }).first().click({ timeout: 8000 });
  await page.waitForTimeout(2200);
};

/**
 * 等一次**新**的 /detail 响应落地（`needle` 用来认领是哪一次请求）。
 *
 * 这里刻意不用 `waitForTimeout(3500)`：首次取某组参数要走分区扫描（实测数秒），
 * 固定睡眠会在冷缓存下假失败 —— 请求已经在飞，只是还没回来，断言却已经读完了。
 * 与操作打包在 Promise.all 里，避免「点击先于等待注册」而错过响应。
 */
const detailRound = async (needle, act) => {
  const [resp] = await Promise.all([
    page.waitForResponse((r) => r.url().includes('/factor-report/detail') && r.url().includes(needle),
      { timeout: 60000 }),
    act(),
  ]);
  detail = await resp.json();
  return resp;
};

console.log(`\n===== 因子报告探针 @ ${BASE} =====`);

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

// ── 进入因子报告 ────────────────────────────────────────────────────────
// ⚠️ 应用是 **HashRouter**：直接 goto `/factor-research?...` 会静默落到首页
// （path 被忽略、不报错），必须走 `#/` 前缀。
await page.goto(`${BASE}/#/factor-research?tab=factor-report`, { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(2500);
await page.waitForSelector('svg[role="img"][aria-label]', { timeout: 30000 }).catch(() => {});
await page.waitForTimeout(2500);

if (FACTOR) {
  const input = page.locator('input[placeholder]').first();
  if (await input.count()) {
    await input.fill(FACTOR);
    await page.waitForTimeout(1200);
    const first = page.locator('aside button').filter({ hasText: FACTOR }).first();
    if (await first.count()) { await first.click(); await page.waitForTimeout(3000); }
  }
}
await page.waitForTimeout(1500);

// ── ① 五个页签齐全 ──────────────────────────────────────────────────────
const tabLabels = await page.evaluate(() =>
  [...document.querySelectorAll('button')].map((b) => (b.innerText || '').trim()));
const missing = TABS.filter((t) => !tabLabels.includes(t));
ok(missing.length === 0, '五个页签齐全（概览/IC/分组回测/相对基准超额/风格相关性数值汇总）',
  missing.length ? `缺 ${missing.join('、')}` : '');

// ── ② 7 指标环有值、弧长随指标变化 ───────────────────────────────────────
const rings = await ringLabels();
ok(rings.length === 7, '7 个指标环', `实际 ${rings.length}`);
const unnamed = rings.filter((r) => /—\s*，/.test(r) || r.includes('未知'));
ok(unnamed.length === 0, '七个环都有数值（无「—」/「百分位未知」）', unnamed.join(' | '));
const pcts = rings.map((r) => /百分位\s*(\d+)%/.exec(r)?.[1]).filter(Boolean);
ok(new Set(pcts).size >= 3, '弧长（全库百分位）逐个不同，不是恒定弧', `百分位 ${pcts.join('/')}`);

const ringsBefore = rings.join('|');

// ── ③ 每个页签都有图，且没有零尺寸画布 ──────────────────────────────────
for (const t of TABS) {
  await clickTab(t);
  const st = await chartStats();
  ok(st.total >= 3 && st.zero.length === 0, `页签「${t}」图表渲染正常`,
    `${st.total} 张${st.zero.length ? `，${st.zero.length} 张零尺寸 ${JSON.stringify(st.zero.slice(0, 3))}` : ''}`);
}

// ── ④ 明细 JSON：机构级块的存在性与数值合理性 ───────────────────────────
ok(detail != null && detailCount > 0, '抓到 /detail 响应', `${detailCount} 次`);
const b = detail?.blocks || {};
const has = (k) => ok(!!b[k], `块存在：${k}`);

['headline', 'significance', 'ic_block', 'group_block', 'cost_block',
  'excess_block', 'style_block', 'robust_block', 'definitions'].forEach(has);

const head = b.headline || {};
ok(head.returns != null && head.ir != null && head.fitness != null && head.margin != null,
  '7 指标环四则量在后端有值',
  `Returns=${head.returns} IR=${head.ir} Fitness=${head.fitness} Margin=${head.margin}`);
// BRAIN 恒等式（后端算的，前端只是展示）
if (head.ir != null && head.returns != null && head.turnover) {
  const expect = head.ir * Math.sqrt(Math.abs(head.returns) / Math.max(head.turnover, 0.125));
  ok(Math.abs(expect - head.fitness) < 1e-6, '恒等式 Fitness ≡ IR·√(|Returns|/max(Turnover,0.125))',
    `${head.fitness?.toFixed(4)} vs ${expect.toFixed(4)}`);
}
if (head.returns != null && head.turnover) {
  ok(Math.abs(head.returns / head.turnover - head.margin) < 1e-6, '恒等式 Margin ≡ Returns/Turnover');
}

// ── ⑤ 可交易轨 ≠ 理想轨 ─────────────────────────────────────────────────
const tr = b.group_block?.tradable;
if (tr && tr.available !== false) {
  const ideal = tr.ideal_cum_end;
  const real = tr.tradable_cum_end;
  ok(ideal != null && real != null && Math.abs(ideal - real) > 1e-9,
    '可交易轨与理想轨不是同一条线', `理想 ${ideal} vs 可交易 ${real}`);
  // 「cum_curve 忘了 -1」唯一的可判据是**与原始净值比**：写 `|值| < 1` 这种启发式
  // 会在真正赚钱的因子上假失败（全窗口累计 +104% 就是 1.047，看着像没减 1 其实减了）。
  const rawNav = b.group_block?.ls_cum?.at(-1);
  ok(rawNav != null && Math.abs(ideal - (rawNav - 1)) < 1e-9 && Math.abs(ideal - rawNav) > 1e-9,
    '累计终值 = 净值因子 − 1（不是直接吐净值）', `净值 ${rawNav} → 收益 ${ideal}`);
  ok(typeof tr.invalid_days === 'number', '披露了坏数据天数 invalid_days', `invalid_days=${tr.invalid_days}`);
  ok(typeof tr.blocked_days === 'number', '披露了被挡天数 blocked_days', `blocked_days=${tr.blocked_days}`);
  ok(tr.lost_return == null || tr.lost_return >= -1e-9, '可交易轨不优于理想轨（掩码没写反）',
    `lost=${tr.lost_return}`);
} else {
  fail++; console.log(`  ✗ 可交易轨不可用：${tr?.reason || '块缺失'}`);
}

// ── ⑤b 两根轴：累计曲线走全窗口、日频统计走请求窗口 ─────────────────────
// 这两类量在同一个块里，各自带轴。轴与序列不等长时前端整张图不画（静默少一张图），
// 故「等长」本身就是要断言的东西，不只是长度好看。
const gbx = b.group_block || {};
const cumN = gbx.cum_dates_full?.length ?? 0;
const dayN = gbx.dates?.length ?? 0;
ok(cumN > dayN, '累计曲线走全窗口、日收益走请求窗口（两根轴并存）',
  `日频 ${dayN} vs 累计 ${cumN}`);
ok(gbx.ls_cum?.length === cumN && gbx.long_cum?.length === cumN && gbx.short_book_cum?.length === cumN,
  'group_block 各累计曲线与其横轴 cum_dates_full 等长');
ok(gbx.ls_daily?.length === dayN, '日收益与其横轴 dates 等长');

// ⚠️ 上面那条原先写的是「各累计曲线」却只查了 group_block —— 标签比实际范围大，
// 于是 ic_block 的三条同轴断言成了漏洞：`cum_ic_top_full` 名字带 `_full` 却按窗口切，
// 与 2600 类目的横轴同图，两条半 IC 只画在最左侧 ~10%（看着有图、其实只画了窗口那段）。
// 教训：**断言的文字必须与它的实际覆盖范围一致**，否则漏洞藏在「看起来已经验过了」里。
const icb = b.ic_block || {};
const icCumN = icb.cum_dates_full?.length ?? 0;
ok(icCumN === cumN && icCumN > 0, 'ic_block 与 group_block 的累计轴同长',
  `ic ${icCumN} vs grp ${cumN}`);
for (const k of ['ic_cum_full', 'cum_ic_top_full', 'cum_ic_bot_full', 'ir_rolling_full']) {
  ok(icb[k]?.length === icCumN, `ic_block.${k} 与其横轴 cum_dates_full 等长`,
    `len=${icb[k]?.length ?? 'null'} vs ${icCumN}`);
}
ok(tr.ls_cum?.length === (tr.cum_dates_full?.length ?? 0), '可交易轨累计与其横轴等长');

// ── ⑥ 成本敏感性 / 盈亏平衡 / 容量 ──────────────────────────────────────
// ⚠️ 这里的断言刻意写成**双向**（有 note ⟺ 值为负）：写成 `值<0 ? 检查note : true`
// 会在值为正时静默通过 —— 那正是本仓栽过的「空集合报通过」。
const cb = b.cost_block || {};
const rows = cb.sensitivity?.rows;
ok(Array.isArray(rows) && rows.length >= 4, '成本敏感性有多档', `${rows?.length ?? 0} 档`);
const be = cb.sensitivity?.break_even_bps;
if (typeof be === 'number' && Number.isFinite(be)) {
  const note = cb.sensitivity?.break_even_note;
  ok((be < 0) === !!note, '盈亏平衡点与解释文案一一对应',
    `值 ${be.toFixed(2)}bp，文案${note ? '有' : '无'}`);
  if (be > 0) {
    // 正向盈亏平衡点下，超过它的成本档净 IR 必须已转负（否则「盈亏平衡」名不副实）
    const over = (rows || []).filter((r) => r.bps > be && r.net_ir != null);
    ok(over.length === 0 || over.every((r) => r.net_ir <= 0),
      '成本超过盈亏平衡点后净 IR ≤ 0', `${over.length} 档越线`);
  }
  const netIrs = (rows || []).map((r) => r.net_ir).filter((v) => v != null);
  ok(netIrs.length >= 2 && netIrs[0] >= netIrs[netIrs.length - 1], '净 IR 随成本单调不升',
    `${netIrs[0]?.toFixed(3)} → ${netIrs[netIrs.length - 1]?.toFixed(3)}`);
} else {
  fail++; console.log(`  ✗ 盈亏平衡成本缺失或非有限值（${be}）`);
}
const cap = cb.capacity;
if (cap) {
  ok(cap.assumed_participation != null && /假设/.test(JSON.stringify(cap)),
    '容量带参与率假设与「简化模型」标注',
    `参与率 ${cap.assumed_participation}`);
} else {
  fail++; console.log('  ✗ 容量块缺失');
}

// ── ⑦ 口径文案与后端同源 ────────────────────────────────────────────────
const defs = b.definitions || {};
ok(Object.keys(defs).length >= 6, '口径文案来自后端 definitions', `${Object.keys(defs).length} 条`);
ok(/0\.125/.test(JSON.stringify(defs)), 'Fitness 文案含 0.125 换手地板');

// ── ⑧ 缓存键：改分组 / 改成本必须重新取数且结果变化 ─────────────────────
const nBefore = detailCount;
const selects = page.locator('aside ~ main select, main select');
const selCount = await selects.count();
if (selCount >= 2) {
  await detailRound('long_group=1', () => selects.nth(0).selectOption('1'));   // G1 多
  ok(detailCount > nBefore, '改分组触发了新的 /detail 请求（缓存键含分组）',
    `${nBefore} → ${detailCount}｜${lastUrls.at(-1) ?? '无请求'}`);
  await page.waitForTimeout(1200);
  const ringsAfter = (await ringLabels()).join('|');
  ok(ringsAfter !== ringsBefore, '改分组后 7 指标环的数值随之改变');
  const lsAfter = detail?.blocks?.group_block?.ls_daily;
  ok(Array.isArray(lsAfter), '改分组后多空日收益序列仍在');
  await detailRound('long_group=3', () => selects.nth(0).selectOption('3'));   // 复原
} else {
  fail++; console.log(`  ✗ 找不到分组下拉（找到 ${selCount} 个 select）`);
}

const nBeforeCost = detailCount;
const costBtn = page.locator('button', { hasText: /^50$/ }).first();
if (await costBtn.count()) {
  const grossBefore = detail?.blocks?.headline?.returns;
  await detailRound('cost_bps=50', () => costBtn.click());
  ok(detailCount > nBeforeCost, '改成本触发了新的 /detail 请求（缓存键含成本）',
    `${nBeforeCost} → ${detailCount}｜${lastUrls.at(-1) ?? '无请求'}`);
  ok(detail?.blocks?.headline?.returns === grossBefore, '改成本不改变毛口径 Returns',
    `${grossBefore} → ${detail?.blocks?.headline?.returns}`);
  ok(detail?.blocks?.headline?.net_returns !== b.headline?.net_returns,
    '改成本后净口径 Returns 变化', `${b.headline?.net_returns} → ${detail?.blocks?.headline?.net_returns}`);
  await detailRound('cost_bps=20', () => page.locator('button', { hasText: /^20$/ }).first().click());
} else {
  fail++; console.log('  ✗ 找不到成本档按钮');
}

// ── ⑨ 基准切换 ──────────────────────────────────────────────────────────
await clickTab('相对基准超额');
const benchBefore = detail?.blocks?.excess_block?.bench_symbol;
const b1000 = page.locator('button', { hasText: '中证1000' }).first();
if (await b1000.count()) {
  const nBeforeBench = detailCount;
  await detailRound('bench=000852', () => b1000.click());
  const benchAfter = detail?.blocks?.excess_block?.bench_symbol;
  ok(detailCount > nBeforeBench, '切基准触发了新的 /detail 请求');
  ok(benchAfter && benchAfter !== benchBefore, '切基准后 bench_symbol 确实变了',
    `${benchBefore} → ${benchAfter}`);
} else {
  fail++; console.log('  ✗ 超额页签找不到「中证1000」按钮');
}
const ex = detail?.blocks?.excess_block || {};
const benchRows = ex.benchmarks || [];
ok(benchRows.length >= 3, '多基准并列（300/500/1000）', `${benchRows.length} 条`);
// 「分年度」若被请求窗口截断就只剩一两根柱子（看着像「这因子只活了一年」）——
// 必须覆盖全样本，故断言 > 1 而不是 > 0（后者在窗口=250 时也能通过）。
ok(Array.isArray(ex.annual) && ex.annual.length > 1, '分年度超额覆盖全样本（不是只剩窗口内那一年）',
  `${ex.annual?.length ?? 0} 年：${(ex.annual ?? []).map((a) => a.year).join('/')}`);
ok(ex.dates?.length === (b.group_block?.cum_dates_full?.length ?? -1),
  '超额曲线与多头组累计同轴（否则「多头 vs 指数」整张图不画）',
  `超额 ${ex.dates?.length} vs 多头 ${b.group_block?.cum_dates_full?.length}`);
ok((ex.ls_daily?.length ?? 0) < (ex.dates?.length ?? 0), '超额块里日频序列仍走请求窗口',
  `日频 ${ex.ls_daily?.length} vs 累计 ${ex.dates?.length}`);

// ── ⑩ 风格块 ────────────────────────────────────────────────────────────
await clickTab('风格相关性数值汇总');
const st = detail?.blocks?.style_block || {};
if (st.available === false) {
  console.log(`  ⚠ 风格块降级：${st.reason}`);
} else {
  ok(Array.isArray(st.exposures) && st.exposures.length === 10, 'Barra 十大风格表 10 行',
    `${st.exposures?.length ?? 0} 行`);
  const att = st.attribution;
  ok(att != null && (att.alpha != null || att.reason), '风格归因回归有结果或显式原因',
    att?.alpha != null ? `α=${att.alpha}` : att?.reason);
  // 「超额风格相关性数值汇总」是用户点名要的表之一。空数组**必须**带原因，
  // 否则「空」既可能是没算、也可能是基准取不到 —— 两种在界面上长得一样。
  const exCorr = st.excess_corr || [];
  ok(exCorr.length === 10 || (exCorr.length === 0 && !!st.excess_corr_reason),
    '超额风格相关性汇总：10 行，或空数组带具体原因',
    exCorr.length ? `${exCorr.length} 行，首行 ${exCorr[0]?.label ?? '（无中文名!）'} ${exCorr[0]?.corr?.toFixed(3)}`
                  : `空 — ${st.excess_corr_reason || '（未给原因 ✗）'}`);
  if (exCorr.length) {
    ok(exCorr.every((r) => r.label && r.label !== r.style), '每行都带中文风格名（不是只给英文 id）');
    ok(exCorr.every((r) => r.n_days > 0), '每行都有有效天数');
  }
}

// ── ⑪ 响应式：缩到 1024 / 768 后图表重绘不留白 ──────────────────────────
for (const w of [1024, 768]) {
  await page.setViewportSize({ width: w, height: 900 });
  await page.waitForTimeout(2500);
  const s = await chartStats();
  const overflow = await page.evaluate(() =>
    document.documentElement.scrollWidth - window.innerWidth);
  ok(s.zero.length === 0 && overflow <= 2, `${w}px 下图表重绘无留白且无横向溢出`,
    `${s.total} 张图，溢出 ${overflow}px`);
}
await page.setViewportSize({ width: 1440, height: 900 });

// ── 报告 ────────────────────────────────────────────────────────────────
if (pageErrors.length) {
  fail++;
  console.log(`  ✗ 页面 JS 错误：${[...new Set(pageErrors)].slice(0, 5).join(' | ')}`);
} else {
  pass++;
  console.log('  ✓ 全程无页面 JS 错误');
}

await page.screenshot({ path: '/tmp/qm_factor_report.png', fullPage: false });
console.log(`\n===== ${pass} 通过 / ${fail} 失败 =====`);
await browser.close();
process.exit(fail === 0 ? 0 : 1);
