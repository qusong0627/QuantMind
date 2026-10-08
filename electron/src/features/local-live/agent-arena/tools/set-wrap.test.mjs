/**
 * 函数式 setState 改写的单测：`npx vitest run set-wrap`
 *
 * 这些用例全部来自搬运时踩过的坑（漏包 = 移植后 tsc 报 TS2345，多包/包错 = 运行时语义变），
 * 改动 set-wrap.mjs 后必须全绿再重跑移植脚本。
 *
 * 用 vitest 的 test（不是 node:test）：本仓前端测试统一跑 `npx vitest run`，
 * 而 vitest 打包不了 `node:test` 这个内建模块（会直接报 Failed Suite）。
 */
import assert from 'node:assert/strict';
import { test } from 'vitest';
import { wrapFunctionalSetters } from './set-wrap.mjs';

const wrap = (code) => wrapFunctionalSetters(code, 'X.tsx');

test('带括号箭头的函数式 setter 被包裹', () => {
  const { code, sites } = wrap('const [n, setN] = useState(0);\nsetN((prev) => prev + 1);\n');
  assert.equal(code.includes('setN(asUpdater((prev) => prev + 1))'), true);
  assert.deepEqual(sites, ['X.tsx:2 setN']);
});

test('无括号单参箭头同样被包裹', () => {
  const { code } = wrap('setErr((cur) => cur || list[0]);\n');
  assert.equal(code.includes('setErr(asUpdater((cur) => cur || list[0]))'), true);
});

test('函数体里有对象字面量（含逗号）不算多实参 —— ChatStream setSections 回归', () => {
  const src = [
    'setSections((prev) => {',
    '  const cur = new Set(prev[idx] ?? []);',
    '  if (cur.has(key)) cur.delete(key);',
    '  else cur.add(key);',
    '  return { ...prev, [idx]: cur };',
    '});',
  ].join('\n');
  const { code } = wrap(src);
  assert.equal(code.startsWith('setSections(asUpdater((prev) => {'), true);
  assert.equal(code.endsWith('}));'), true);
});

test('嵌套 setter 一并包裹（外层先包、内层递归）', () => {
  const src = 'setOuter((prev) => {\n  setInner((q) => q + 1);\n  return prev;\n});\n';
  const { code, sites } = wrap(src);
  assert.equal(code.includes('asUpdater((prev) => {'), true);
  assert.equal(code.includes('setInner(asUpdater((q) => q + 1))'), true);
  assert.deepEqual(sites, ['X.tsx:2 setInner', 'X.tsx:1 setOuter']);
});

test('多行实参 + 尾逗号仍算单实参（BatchScreener / Live.tsx 回归）', () => {
  const src = [
    'setSid((cur) =>',
    "  strategies.some((s) => s.id === cur) ? cur : (strategies[0]?.id ?? ''),",
    ');',
  ].join('\n');
  const { code, sites } = wrap(src);
  assert.equal(code.startsWith('setSid(asUpdater((cur) =>'), true);
  assert.equal(code.endsWith('),\n));'), true);
  assert.deepEqual(sites, ['X.tsx:1 setSid']);
});

test('多实参不碰（setTimeout 这类同名形状）', () => {
  const src = 'setTimeout(() => tick(), 1000);\n';
  const { code, sites } = wrap(src);
  assert.equal(code, src);
  assert.deepEqual(sites, []);
});

test('非函数实参不碰', () => {
  const src = 'setOpen(!open);\nsetList(items.filter((i) => i.ok));\n';
  const { code, sites } = wrap(src);
  assert.equal(code, src);
  assert.deepEqual(sites, []);
});

test('字符串/注释/模板里的 setX( 不误伤', () => {
  const src = [
    "const help = 'setOpen(prev => ...)';",
    '// setX((prev) => prev)',
    'setReal((prev) => ({ ...prev, a: `x${prev.n}` }));',
  ].join('\n');
  const { code, sites } = wrap(src);
  assert.equal(code.includes("const help = 'setOpen(prev => ...)';"), true);
  assert.equal(code.includes('// setX((prev) => prev)'), true);
  assert.deepEqual(sites, ['X.tsx:3 setReal']);
});

test('正则字面量里的括号不破坏配对扫描', () => {
  const src = "setPath((prev) => prev.replace(/\\((\\d)\\)/g, '$1'));\n";
  const { code } = wrap(src);
  assert.equal(code.includes('setPath(asUpdater((prev) => prev.replace('), true);
});

test('幂等：已包裹的不再套一层', () => {
  const { code } = wrap('setN(asUpdater((prev) => prev + 1));\n');
  assert.equal(code, 'setN(asUpdater((prev) => prev + 1));\n');
});

test('实参里的 setX( 是字符串拼接也不影响后续包裹', () => {
  const src = "setTip('见 setOpen(' + id + ')');\nsetN((p) => p + 1);\n";
  const { code, sites } = wrap(src);
  assert.deepEqual(sites, ['X.tsx:2 setN']);
  assert.equal(code.includes("setTip('见 setOpen(' + id + ')');"), true);
});
