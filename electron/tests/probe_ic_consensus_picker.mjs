/**
 * 推理中心「共识点名」端到端探针。
 *
 * 验证的不是渲染，而是**这条链路真的能拿到多模型共识**：
 *   点名 N 个模型 → 计数 chip 出现 → 执行推理 → 响应体 consensus_coverage
 *   的 scored 必须 > 1 且 is_thin=false。
 *
 * 判定用「响应体 + 文案」双证：只看文案会被措辞变化误伤，只看响应体则漏掉
 * UI 是否如实呈现。另有一条硬断言：**读路径不得触发任何现场计算**
 * （executed 必须恒为 0），这是 T-CONS-02 的成本护栏。
 */
import { chromium } from 'playwright';

const BASE = 'http://localhost:3080';
const PICK = 3;
const SYMBOL = '600519';

const browser = await chromium.launch({
  executablePath: '/usr/bin/google-chrome-stable',
  args: ['--no-sandbox'],
});
const page = await browser.newPage({ viewport: { width: 1760, height: 1000 } });
const logs = [];
page.on('pageerror', (e) => logs.push('[PAGEERROR] ' + (e.message || '').slice(0, 300)));
page.on('console', (m) => {
  if (m.type() === 'error') logs.push('[console.error] ' + m.text().slice(0, 300));
});

let failures = 0;
const check = (name, ok, detail = '') => {
  console.log(`${ok ? '[PASS]' : '[FAIL]'} ${name}${detail ? ' :: ' + detail : ''}`);
  if (!ok) failures += 1;
};

/** 抓 POST /research/predict-stock 的响应体（后端统一信封 {code,data}） */
const armPredict = () =>
  page.waitForResponse(
    (r) => r.url().includes('/research/predict-stock') && r.request().method() === 'POST',
    { timeout: 280000 },
  );

const readBody = async (resp) => {
  const raw = await resp.json();
  return raw?.data ?? raw;
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

// ── 单票研判工作区 ────────────────────────────────────────────
const tabs = page.locator('nav[aria-label="推理中心工作区"] button');
for (let i = 0, n = await tabs.count(); i < n; i++) {
  const t = ((await tabs.nth(i).innerText().catch(() => '')) || '').replace(/\s/g, '');
  if (t.includes('单票研判')) { await tabs.nth(i).click(); break; }
}
await page.waitForTimeout(2000);

const picker = page.locator('[data-testid="consensus-picker"]');
check('共识点名条已渲染', (await picker.count()) > 0);

// ── 基线：输代码出图（读路径）────────────────────────────────
const baseResp = armPredict();
await page.locator('input[placeholder*="600519"]').first().fill(SYMBOL);
await page.keyboard.press('Enter');
let base = null;
try {
  base = await readBody(await baseResp);
} catch (e) {
  check('基线读路径返回', false, String(e).slice(0, 160));
}
if (base) {
  const cov = base.consensus_coverage || {};
  check('读路径不触发任何现场补算', (cov.executed || 0) === 0, `executed=${cov.executed}`);
  console.log(
    `[info] 基线共识：scored=${cov.scored} total=${cov.total} is_thin=${cov.is_thin} ` +
      `skip=${JSON.stringify(cov.skip_reasons || {})}`,
  );
}
await page.waitForTimeout(1500);

// ── 点名 N 个模型 ─────────────────────────────────────────────
await picker.locator('.ant-select-selector').first().click();
await page.waitForTimeout(1200);
const dropdown = page.locator('.ant-select-dropdown:not(.ant-select-dropdown-hidden)').last();
const opts = dropdown.locator('.ant-select-item-option');
const optCount = await opts.count();
check('共识模型下拉有可选项', optCount > 0, `${optCount} 项`);
const pickedNames = [];
for (let i = 0; i < optCount && pickedNames.length < PICK; i++) {
  const cls = (await opts.nth(i).getAttribute('class')) || '';
  if (cls.includes('option-disabled') || cls.includes('option-selected')) continue;
  pickedNames.push(((await opts.nth(i).innerText().catch(() => '')) || '').trim());
  await opts.nth(i).click().catch(() => {});
  await page.waitForTimeout(300);
}
await page.keyboard.press('Escape');
await page.waitForTimeout(600);
check(`已点名 ${PICK} 个模型`, pickedNames.length === PICK, pickedNames.join(' / '));

const chipText = ((await page.locator('[data-testid="consensus-picker-count"]').innerText().catch(() => '')) || '').trim();
check('计数 chip 显示 n/4', chipText === `${pickedNames.length}/4`, `chip="${chipText}"`);
await page.screenshot({ path: '/tmp/ic_consensus_picked.png' });

// ── 执行推理：唯一会现场补算共识模型的路径 ─────────────────────
const runResp = armPredict();
const runBtns = page.locator('button');
for (let i = 0, n = await runBtns.count(); i < n; i++) {
  const t = ((await runBtns.nth(i).innerText().catch(() => '')) || '').replace(/\s/g, '');
  if (/开始个股推理/.test(t)) { await runBtns.nth(i).click(); break; }
}

let body = null;
try {
  body = await readBody(await runResp);
} catch (e) {
  check('执行推理返回', false, String(e).slice(0, 160));
}

if (body) {
  const cov = body.consensus_coverage || {};
  const rows = (body.consensus || []).length;
  const failed = cov.failed_models || [];
  console.log(
    `[info] 执行后共识：scored=${cov.scored} total=${cov.total} is_thin=${cov.is_thin} ` +
      `executed=${cov.executed} skip=${JSON.stringify(cov.skip_reasons || {})} rows=${rows}`,
  );
  if (failed.length) {
    for (const f of failed) console.log(`[info]   点名失败 ${f.model_id} :: ${f.error} :: ${f.detail || ''}`);
  }
  check('响应含 consensus_coverage', Boolean(body.consensus_coverage));
  check('共识样本数 > 1（现场补算生效）', (cov.scored || 0) > 1, `scored=${cov.scored}`);
  // 空集合守卫：0===0 会假通过，所以先要求 rows>0
  check('共识矩阵行数与 scored 一致', rows > 0 && rows === cov.scored, `rows=${rows} scored=${cov.scored}`);
  check('分母为点名范围（不含未点名的模型）', cov.total === pickedNames.length, `total=${cov.total} picked=${pickedNames.length}`);
  // 措辞一致性：thin 判定只看样本数阈值，不依赖具体跑到几个模型
  check('is_thin 与样本数自洽', cov.is_thin === (cov.scored < 3), `is_thin=${cov.is_thin} scored=${cov.scored}`);
  // 诚实性：样本不足必须有说明句，否则前端只剩一个孤零零的百分比
  check('样本不足时给出说明句', !cov.is_thin || Boolean(body.consensus_note), `note=${body.consensus_note || '(空)'}`);
  // 失败可指认：每个失败模型都要有机器码，且被写进说明句
  check('失败模型带原因码', failed.every((f) => Boolean(f.error)), JSON.stringify(failed.map((f) => f.error)));
  check(
    '失败模型在说明句里被点名',
    failed.length === 0 || failed.every((f) => String(body.consensus_note || '').includes(f.model_id)),
    `note=${String(body.consensus_note || '').slice(0, 200)}`,
  );
}

await page.waitForTimeout(4000);
const after = ((await page.locator('body').innerText().catch(() => '')) || '').replace(/\n+/g, ' | ');
check('结果区渲染出自选模式说明', /自选模式|已匹配/.test(after), after.match(/自选模式[^|]*|已匹配[^|]*/)?.[0] || '(未匹配)');
await page.screenshot({ path: '/tmp/ic_consensus_done.png' });

console.log('[ERRORS]', logs.filter(Boolean).join(' || ') || 'none');
console.log(failures === 0 ? '[RESULT] ALL PASS' : `[RESULT] ${failures} FAILED`);
await browser.close();
process.exit(failures === 0 ? 0 : 1);
