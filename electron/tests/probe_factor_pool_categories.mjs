/** 因子池「因子分类概览」探针：分类卡渲染 / 点卡下钻 / chips 过滤 / 行内徽章。
 *  真实后端数据（a_share 全部股票池），非 mock。 */
import { chromium } from 'playwright';

const BASE = process.env.QM_BASE || 'http://localhost:3080';
const b = await chromium.launch({ executablePath: '/usr/bin/google-chrome-stable', args: ['--no-sandbox'] });
const ctx = await b.newContext({ viewport: { width: 1720, height: 1150 } });
const p = await ctx.newPage();

let failed = 0;
const check = (name, ok, extra = '') => {
  console.log(`${ok ? 'PASS' : 'FAIL'} ${name}${extra ? ' | ' + extra : ''}`);
  if (!ok) failed++;
};

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
const dismissLater = async () => {
  const btn = p.locator('.ant-modal-wrap button:has-text("稍后再答")');
  if (await btn.count()) { await btn.first().click(); await p.waitForTimeout(600); }
};
await dismissLater();

await p.goto(`${BASE}/#/alpha-research`, { waitUntil: 'domcontentloaded', timeout: 40000 });
await p.waitForTimeout(5000);
// 侧栏进「因子池」
await p.locator('text=因子池').first().click();
await p.waitForTimeout(4000);

// 1) 分类概览卡渲染
await p.waitForSelector('text=因子分类概览', { timeout: 20000 });
const cards = p.locator('button[title^="查看「"]');
await cards.first().waitFor({ timeout: 20000 });
const cardCount = await cards.count();
check('分类卡数量 > 5', cardCount > 5, `cards=${cardCount}`);

const firstCardText = (await cards.first().innerText()).replace(/\n+/g, ' · ');
check('首卡含类名/计数/占比', /·/.test(firstCardText) && /%/.test(firstCardText), firstCardText.slice(0, 120));

// 计数和（与下钻后头部「池内因子 （N）」核对，见步骤 3 末）
const sumCounts = await cards.evaluateAll((els) =>
  els.reduce((acc, el) => {
    const m = (el.innerText.match(/(\d+)\s*·\s*[\d.]+%/) || [])[1];
    return acc + (m ? Number(m) : 0);
  }, 0),
);
check('分类卡计数和可解析', sumCounts > 0, `sum=${sumCounts}`);

await p.screenshot({ path: '/tmp/pool_cat_overview.png', fullPage: false });

// 2) 点第一张卡 → 下钻到池因子页签（类别过滤 + 行内徽章）
const firstTitle = await cards.first().getAttribute('title');
const firstLabel = firstTitle.match(/「(.+)」/)[1];
await cards.first().click();
await p.waitForTimeout(1500);
const headerText = await p.locator('text=池内因子').first().locator('..').innerText().catch(() => '');
check('下钻后页签标题带类别', headerText.includes(`类别「${firstLabel}」`), headerText.slice(0, 80));

// 徽章：池因子表行内出现类别徽章（title 以 原始标签/按因子名 开头）
const badges = p.locator('span[title^="原始标签"], span[title^="按因子名"]');
await badges.first().waitFor({ timeout: 15000 }).catch(() => {});
check('行内类别徽章渲染', (await badges.count()) > 0, `badges=${await badges.count()}`);

// chips：active chip 高亮（violet）且含计数
const chips = p.locator('button:has-text("全部")');
check('chips 区有「全部」', (await chips.count()) > 0);
const activeChip = p.locator(`button:has-text("${firstLabel}")`).first();
check('该类 chip 呈激活态', ((await activeChip.getAttribute('class')) || '').includes('violet'));
await p.screenshot({ path: '/tmp/pool_cat_drilldown.png' });

// 3) 点「全部」清除过滤 → 行数恢复且分类计数和 = 池总量
await chips.first().click();
await p.waitForTimeout(1500);
const header2 = await p.locator('text=池内因子').first().locator('..').innerText().catch(() => '');
check('清除后标题不再带类别', !header2.includes('类别「'), header2.slice(0, 80));
const totalM = header2.match(/（(\d+)）/);
check('分类计数和 = 池总量', totalM != null && Number(totalM[1]) === sumCounts, `sum=${sumCounts} header=${(totalM || [])[1]}`);

// 4) 回总览确认状态仍在
await p.locator('text=池总览').first().click();
await p.waitForTimeout(1200);
check('回总览分类卡仍在', (await p.locator('button[title^="查看「"]').count()) > 5);

await b.close();
console.log(failed === 0 ? 'ALL PASS' : `${failed} FAILED`);
process.exit(failed === 0 ? 0 : 1);
