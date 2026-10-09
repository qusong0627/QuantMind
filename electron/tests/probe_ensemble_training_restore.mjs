/**
 * 集合训练（Stacking）入口恢复 E2E 探针（2026-10-09）
 *
 * 背景：2026-09-05 单选化改版（6a7c5b16）把「多选模型 → 集成方法 → Stacking」
 * 的 UI 入口摘掉，后端 train_stacking（OOF + Ridge）链路一直健在。本次恢复
 * Checkbox 多选 + 集成方法 Select + n_folds/meta_alpha + 混合警告，探针钉住：
 *   1) 模型类型区是多选 Checkbox（非单选 Radio）
 *   2) 选 LGB+XGB → 「已选模型」chips + 「集成方法」出现
 *   3) 选 Stacking → OOF 折数 / 元学习器正则 出现；下一步预览卡出现 stacking chip
 *   4) 加选 GRU（树+DL 混合）→ 混合警告出现、集成方法隐藏
 *   5) 取消 GRU → 集成方法回归（无集成态）；重选 Stacking → 参数行回归
 *   6) 取消 XGB（回到单选）→ 集成方法消失（单选绝无集成入口）
 * 全程绝不点「开始训练」（step 3 的按钮=真启动训练）。
 */
import { chromium } from 'playwright';

const BASE = process.env.QM_BASE || 'http://localhost:3080';

const b = await chromium.launch({ executablePath: '/usr/bin/google-chrome-stable', args: ['--no-sandbox'] });
const ctx = await b.newContext({ viewport: { width: 1720, height: 1150 } });
const p = await ctx.newPage();
const errors = [];
const trainReqs = [];
p.on('pageerror', (e) => errors.push((e.message || '').slice(0, 160)));
p.on('request', (r) => {
  if (r.method() === 'POST' && /\/training/.test(r.url())) trainReqs.push(r.url().split('/api/v1')[1] || r.url());
});

const body = () => p.locator('body').innerText().catch(() => '');
const has = async (t) => (await body()).includes(t);
const clickNext = async () => {
  await p.locator('button').filter({ hasText: /下一\s*步/ }).first().click();
  await p.waitForTimeout(900);
};
const dismissLater = async () => {
  const btn = p.locator('.ant-modal-wrap button:has-text("稍后再答")');
  if (await btn.count()) { await btn.first().click(); await p.waitForTimeout(600); }
};
const check = (label, ok) => console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}`);
// 「集成方法」文本在混合警告文案里也出现（"集成方法暂不支持"），文本包含断言会恒真；
// 判选择器有无必须数组件本体（Select=「无集成」态）。
const ensembleSelectCount = () => p.locator('.ant-select').filter({ hasText: '无集成' }).count();

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

// ── 深链进训练页，两步「下一步」到参数配置（step 2；步进无校验）──
await p.goto(`${BASE}/#/model-training`, { waitUntil: 'domcontentloaded' });
await p.waitForTimeout(4000);
await dismissLater();
await clickNext();
await clickNext();

// 1) 多选 Checkbox 而非单选 Radio
const cbCount = await p.locator('.ant-checkbox-wrapper').count();
check(`参数配置区是多选 Checkbox（${cbCount} 个）`, cbCount >= 5 && (await has('可多选模型')));
await p.screenshot({ path: '/tmp/ensemble_step2_single.png' });

// 2) 多选 LGB+XGB → 已选模型 chips + 集成方法
await p.locator('.ant-checkbox-wrapper').filter({ hasText: 'XGBoost' }).first().click();
await p.waitForTimeout(700);
check('多选后出现「已选模型」chips', await has('已选模型'));
check('多选后出现「集成方法」选择器', await has('集成方法'));

// 3) 选 Stacking → OOF 折数 / 元学习器正则
await p.locator('.ant-select').filter({ hasText: '无集成' }).first().click();
await p.waitForTimeout(500);
await p.locator('.ant-select-item-option').filter({ hasText: 'Stacking 集成' }).first().click();
await p.waitForTimeout(700);
check('Stacking 参数行：OOF 折数', await has('OOF 折数'));
check('Stacking 参数行：元学习器正则', await has('元学习器正则'));

// 预览卡核对（进 step 3 只看不点；按钮=「开始训练」，绝不触碰）
await clickNext();
check('预览卡「任务配置核对」出现', await has('任务配置核对'));
check('预览卡显示 stacking chip', await has('stacking'));
await p.screenshot({ path: '/tmp/ensemble_preview.png' });
// 左侧模块导航点回参数配置（step 2）
await p.locator('button').filter({ hasText: '参数配置' }).first().click();
await p.waitForTimeout(800);

// 4) 加选 GRU（树+DL 混合）→ 警告出现、集成方法隐藏
await p.locator('.ant-checkbox-wrapper').filter({ hasText: '门控循环单元' }).first().click();
await p.waitForTimeout(700);
check('混合选型出现警告', await has('树模型与深度学习模型混合训练时'));
check('混合选型隐藏集成方法', (await ensembleSelectCount()) === 0);
await p.screenshot({ path: '/tmp/ensemble_mixed.png' });

// 5) 取消 GRU → 集成方法回归（此时被强制回无集成态）
await p.locator('.ant-checkbox-wrapper').filter({ hasText: '门控循环单元' }).first().click();
await p.waitForTimeout(700);
check('取消 GRU 后警告消失', !(await has('树模型与深度学习模型混合训练时')));
check('取消 GRU 后集成方法回归', (await ensembleSelectCount()) >= 1);
// 重选 Stacking → 参数行回归
await p.locator('.ant-select').filter({ hasText: '无集成' }).first().click();
await p.waitForTimeout(500);
await p.locator('.ant-select-item-option').filter({ hasText: 'Stacking 集成' }).first().click();
await p.waitForTimeout(700);
check('重选 Stacking 后参数行回归', (await has('OOF 折数')) && (await has('元学习器正则')));
await p.screenshot({ path: '/tmp/ensemble_restore.png' });

// 6) 回到单选 → 集成入口整体消失
await p.locator('.ant-checkbox-wrapper').filter({ hasText: 'XGBoost' }).first().click();
await p.waitForTimeout(700);
check('单选后集成方法消失', (await ensembleSelectCount()) === 0);
check('单选后已选模型 chips 消失', !(await has('已选模型')));

console.log('训练 POST 请求（应为 0，全程未点开始训练）:', trainReqs.length ? trainReqs : 'none');
console.log('ERRORS:', errors.length ? errors.slice(0, 4) : 'none');
await b.close();
