/**
 * 模型融合入口 E2E 探针（2026-10-09）
 *
 * 背景：模型管理里「多选已有模型 → 融合为新模型」的入口于 6c469eb7 下线，
 * 本轮以机构级 v2（滚动 ICIR 收缩 + 多样性惩罚 + OOS 回放）恢复。探针钉住：
 *   1) 模型资产库有「模型融合」开关；默认无复选框（常规浏览态）
 *   2) 开启融合模式 → 卡片出现复选框 + 「融合模式 · 勾选成员」提示
 *   3) 勾选 2 个用户模型 → 底部「已选 N 个成员」+「融合为集成模型」可点
 *   4) 打开 Modal → 触发 /models/ensemble/preview → 成员证据表 / 权重% / OOS 回放段
 *   5) 切「手工」→ 出现权重录入框；切回「等权」→ 录入框消失
 *   6) 关闭 Modal、退出融合模式 → 已创建的融合模型卡片有「融合×N」徽标
 *   7) 选中融合模型 → 详情面板出现「融合成员与权重」与成员行
 * 绝不点「创建融合模型」（真建模型 + 进日更推理名单，属副作用）。
 */
import { chromium } from 'playwright';

const BASE = process.env.QM_BASE || 'http://localhost:3080';

const b = await chromium.launch({ executablePath: '/usr/bin/google-chrome-stable', args: ['--no-sandbox'] });
const ctx = await b.newContext({ viewport: { width: 1720, height: 1150 } });
const p = await ctx.newPage();
const errors = [];
const previewReqs = [];
p.on('pageerror', (e) => errors.push((e.message || '').slice(0, 160)));
p.on('response', (r) => {
  if (r.url().includes('/models/ensemble/preview')) previewReqs.push(`${r.status()} ${r.url().split('/api/v1')[1] || r.url()}`);
});

const body = () => p.locator('body').innerText().catch(() => '');
const has = async (t) => (await body()).includes(t);
const check = (label, ok) => { console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}`); return ok; };
const dismissLater = async () => {
  const btn = p.locator('.ant-modal-wrap button:has-text("稍后再答")');
  if (await btn.count()) { await btn.first().click(); await p.waitForTimeout(600); }
};

// ── 登录 ──
await p.goto(`${BASE}/#/auth/login`, { waitUntil: 'domcontentloaded', timeout: 40000 });
await p.waitForTimeout(3500);
if ((await p.locator('input[type=password]').count()) > 0) {
  await p.locator('input').nth(0).fill('admin');
  await p.locator('input[type=password]').first().fill('admin123');
  const btns = p.locator('button');
  for (let i = 0; i < (await btns.count()); i++) {
    const t = (await btns.nth(i).innerText().catch(() => '')).replace(/\s/g, '');
    if (/登录/.test(t)) { await btns.nth(i).click(); break; }
  }
  await p.waitForTimeout(6000);
}
await dismissLater();

// ── 深链进模型资产库 ──
await p.goto(`${BASE}/#/model-registry`, { waitUntil: 'domcontentloaded' });
await p.waitForTimeout(4500);
await dismissLater();
check('模型资产库加载（标题出现）', await has('模型资产库'));

// 1) 默认无复选框 + 融合开关存在
const toggle = p.locator('button[title="模型融合"]');
check('「模型融合」开关存在', (await toggle.count()) > 0);
const cbBefore = await p.locator('.ant-checkbox-input').count();
check(`常规态无复选框（${cbBefore} 个）`, cbBefore === 0);

// 2) 开启融合模式
await toggle.first().click();
await p.waitForTimeout(800);
const cbAfter = await p.locator('.ant-checkbox-input').count();
check(`融合模式出现复选框（${cbAfter} 个）`, cbAfter >= 2);
check('列表头提示「融合模式 · 勾选成员」', await has('融合模式 · 勾选成员'));
check('底部按钮为「融合为集成模型」', await has('融合为集成模型'));
const fuseBtn = p.locator('button:has-text("融合为集成模型")');
check('未满 2 个成员时按钮禁用', await fuseBtn.first().isDisabled());
await p.screenshot({ path: '/tmp/fusion_1_mode_on.png' });

// 3) 勾选 2 个「已就绪」用户模型（候选/归档/系统模型复选框置灰，点了会出警告）
const cards = p.locator('div.cursor-pointer').filter({ has: p.locator('.ant-checkbox') });
const cardCount = await cards.count();
check(`可勾选卡片 ≥2（${cardCount} 张）`, cardCount >= 2);
const disabledBoxes = await p.locator('.ant-checkbox-input[disabled]').count();
check(`不可作成员的卡片复选框置灰（${disabledBoxes} 个）`, disabledBoxes >= 1);
await cards.filter({ hasText: 'T+10 LightGBM 选股' }).first().click();
await p.waitForTimeout(400);
await cards.filter({ hasText: 'XGBoost基线模型' }).first().click();
await p.waitForTimeout(600);
check('底部显示「已选 2 个成员」', await has('已选 2 个成员'));
check('已选 2 个后创建按钮可点', !(await fuseBtn.first().isDisabled()));
await p.screenshot({ path: '/tmp/fusion_2_two_selected.png' });

// 4) 打开 Modal → 预览
const previewPromise = p.waitForResponse((r) => r.url().includes('/models/ensemble/preview'), { timeout: 90000 }).catch(() => null);
await fuseBtn.first().click();
const previewResp = await previewPromise;
await p.waitForTimeout(1200);
check('Modal 标题「模型融合（机构级）」', await has('模型融合（机构级）'));
check(`preview 请求已发出（${previewReqs.join(' | ') || '无'}）`, previewReqs.length > 0 && previewReqs.every((s) => s.startsWith('200')));
check('成员证据与权重预览段出现', await has('成员证据与权重预览'));
check('OOS 回放段出现', await has('OOS 回放'));
const weightPct = await p.locator('.ant-modal').locator('text=/\\d+\\.\\d%/').count();
check(`权重百分比渲染（${weightPct} 处）`, weightPct >= 2);
const verdictShown =
  (await has('优于全部成员')) || (await has('优于成员中位')) || (await has('低于成员中位')) || (await has('OOS 证据不足')) || (await has('回放样本不足'));
check('OOS 结论三态之一已渲染', verdictShown);
check('「创建融合模型」按钮存在（不点击）', await has('创建融合模型'));
await p.screenshot({ path: '/tmp/fusion_3_preview.png' });

// 5) 权重策略切换：手工 → 录入框；回等权 → 消失
const numBefore = await p.locator('.ant-modal .ant-input-number').count();
await p.locator('.ant-modal .ant-radio-button-wrapper:has-text("手工")').first().click();
await p.waitForTimeout(500);
const numManual = await p.locator('.ant-modal .ant-input-number').count();
check(`切「手工」出现权重录入框（${numBefore} → ${numManual}）`, numManual >= 2);
await p.locator('.ant-modal .ant-radio-button-wrapper:has-text("等权")').first().click();
await p.waitForTimeout(400);
const numBack = await p.locator('.ant-modal .ant-input-number').count();
check(`切回「等权」录入框消失（${numBack}）`, numBack === 0);

// 6) 关闭 Modal + 退出融合模式 → 融合模型卡片徽标
await p.locator('.ant-modal-wrap:visible .ant-modal-close').first().click();
await p.waitForTimeout(500);
await toggle.first().click();
await p.waitForTimeout(700);
check('退出融合模式后复选框消失', (await p.locator('.ant-checkbox-input').count()) === 0);
check('列表存在「融合×N」徽标', await has('融合×'));

// 7) 选中 v2 融合模型（带 source_models）→ 详情面板成员与权重
const ensCard = p.locator('div.cursor-pointer').filter({ hasText: '机构级融合-取数修复验收' }).first();
if (await ensCard.count()) {
  await ensCard.click();
  await p.waitForTimeout(2500);
  check('详情面板「融合成员与权重」', await has('融合成员与权重'));
  check('详情面板「权重策略」', await has('权重策略'));
  check('详情面板成员权重行（%）', (await p.locator('text=/\\d+\\.\\d%/').count()) >= 2);
  check('详情面板成员名（LGB-127）', await has('LGB-127'));
  await p.screenshot({ path: '/tmp/fusion_4_detail.png' });
} else {
  check('融合模型卡片存在', false);
}

console.log(`\npageerrors: ${errors.length ? errors.join(' ;; ') : '无'}`);
console.log(`preview requests: ${previewReqs.join(' | ') || '无'}`);
await b.close();
