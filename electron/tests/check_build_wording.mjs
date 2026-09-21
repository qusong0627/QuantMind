/**
 * 生产构建产物措辞体检：**打包出来的 JS 里不许再出现方向性下单措辞**。
 *
 * 为什么要单独查构建产物，而不只查源码（`grep -r src/`）：源码里没有，不等于发出去的包里没有 ——
 * 依赖、模板字符串拼接、别处的副本都可能把词带进来，而用户装到的是**包**不是仓库。
 * 同理，源码里有也不等于包里有（JSDoc 注释会被压缩器剥掉，「强烈看多」目前就只活在注释里）。
 * 两边都查，判据不同：源码查是给改代码的人看，构建产物查是给发版前把关。
 *
 * ⚠️ **零文件即失败**：`dist-react/assets/*.js` 一个都没匹配到时必须报错退出，不能静默通过。
 * 构建目录改名、跑错目录、忘了先 `npm run build` —— 这三种情况都会让 glob 空手而归，
 * 而「没扫到文件」的报告长得和「扫了、干净」一模一样。验收口径里最危险的就是这种假通过。
 *
 * 用法（必须先构建）：
 *   cd electron && npm run dashboard:build && node tests/check_build_wording.mjs
 */
import { readFileSync, readdirSync, existsSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const ASSETS_DIR = resolve(HERE, '..', 'dist-react', 'assets');

/**
 * 硬禁词：**荐股软件形态的措辞**。这些是「替用户决定买卖」的字面表达，
 * 展示面与执行面都不该有（执行面说的是「买入/卖出」这个动作本身，不是「一键」「强烈」「建议」）。
 * 出现即失败。
 */
const BANNED = [
  '一键买入',
  '一键卖出',
  '强烈看多',
  '偏多研判',
  '看空预警',
  '建议买入',
  '建议卖出',
  '建议清仓',
  '立即买入',
  '立即卖出',
];

/**
 * 观察词：实盘功能的**入口标签**。这些字面量**按设计**留在包里 ——
 * 用户拍板「代码功能留着，给需要的人改」，`RealTradingPage`/`QmtMirrorCard` 整棵树都还在，
 * 只是开关关闭时不挂载。所以这里只报数、不判失败。
 * 「用户看不到」这件事由运行时探针 `probe_live_trading_hidden.mjs` 负责证，
 * 两件事别混：**包里有字符串 ≠ 界面上有入口**。
 */
const OBSERVE = ['券商实盘接入', '大 QMT', '真单镜像', '确认下单'];

const fail = (msg) => {
  console.error(`\n❌ ${msg}\n`);
  process.exit(1);
};

if (!existsSync(ASSETS_DIR)) {
  fail(`构建产物目录不存在：${ASSETS_DIR}\n   先在 electron/ 下跑 npm run dashboard:build。`);
}

const files = readdirSync(ASSETS_DIR).filter((f) => f.endsWith('.js'));
if (files.length === 0) {
  fail(`${ASSETS_DIR} 里一个 .js 都没有 —— 零文件参与等于没验，按失败处理。`);
}

const hits = [];
const observed = new Map(OBSERVE.map((t) => [t, 0]));

for (const name of files) {
  const text = readFileSync(join(ASSETS_DIR, name), 'utf8');
  for (const token of BANNED) {
    if (text.includes(token)) hits.push({ name, token });
  }
  for (const token of OBSERVE) {
    if (text.includes(token)) observed.set(token, observed.get(token) + 1);
  }
}

console.log(`\n=== 构建产物措辞体检 @ ${ASSETS_DIR} ===`);
console.log(`扫描 ${files.length} 个 .js 文件\n`);

console.log('观察词（不判失败：入口标签随「代码留着」长期存在，隐藏由运行时探针证）：');
for (const [token, n] of observed) console.log(`  ${n > 0 ? '·' : '✓'} ${token} — 命中 ${n} 个文件`);

if (hits.length > 0) {
  console.error('\n硬禁词命中：');
  for (const { name, token } of hits.slice(0, 40)) console.error(`  ❌ ${name} ← ${token}`);
  if (hits.length > 40) console.error(`  … 另有 ${hits.length - 40} 处`);
  fail(`构建产物含 ${hits.length} 处方向性下单措辞`);
}

console.log(`\n✅ 硬禁词 0 命中（${BANNED.length} 个词 × ${files.length} 个文件）`);
