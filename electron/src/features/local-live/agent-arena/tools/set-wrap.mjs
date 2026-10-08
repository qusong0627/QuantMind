/**
 * 函数式 setState 的搬运期改写（`setX(prev => ...)` → `setX(asUpdater(prev => ...))`）。
 *
 * 从 port-from-arena.mjs 里拆出来单独成模块，是为了能脱离整棵闭包直接单测/调试
 * （见 tools/set-wrap.test.mjs）。详细背景见本文件末尾的 REACT_COMPAT_SRC。
 */
// ──────────────────── 函数式 setState 绕行（本仓 tsc 特性） ────────────────────
// arena 里大量 `setX(prev => ...)`。搬进 electron/ 后一律 TS2345：本仓 tsc 把
// `Dispatch<SetStateAction<S>>` 实例化成了 `(value: S) => void`（函数分支丢了），
// 与写法无关（`useState<number>` 也中招，见 memory: useState-functional-setter-type-bug）。
// 运行时完全无影响（React 见函数即当 updater），所以这里给**整段实参**套一层
// `asUpdater(...)`：helper 返回 `never`，可赋给任何形参；值原样返回。语义逐位不变。

export const REACT_COMPAT_REL = 'reactCompat.ts';
export const REACT_COMPAT_SRC = `/* 由 tools/port-from-arena.mjs 生成，勿手改 */
/**
 * 函数式 setState 的类型绕行。
 *
 * 本仓（electron/）的 tsc 环境把 \`Dispatch<SetStateAction<S>>\` 实例化成 \`(value: S) => void\`，
 * 函数分支丢失 —— 于是 \`setX(prev => ...)\` 一律 TS2345，与写法无关，\`useState<number>\` 同样中招。
 * 这是本仓既有的环境问题（项目代码因此从不用函数式 setter），非 arena 代码有问题。
 *
 * 运行时零影响：React 收到函数就当 updater 处理，\`asUpdater(fn)\` 原样返回 \`fn\`。
 * 返回类型 \`never\` 是为可赋给任何形参 —— 调用点因此不必写任何类型标注。
 */
export function asUpdater<T>(updaterFn: T): never {
  return updaterFn as unknown as never;
}
`;

/** 从 openIdx（指向 `(`）扫到配对的 `)`，跳过字符串/模板/注释/正则；返回 {endIdx, argStart, topLevelComma} */
export function scanCall(code, openIdx) {
  let depth = 0;
  // 实参里的对象/数组字面量也有逗号（`{ ...prev, [idx]: cur }`），按「括号深度 1 且
  // 不在花括号/方括号里」才算分隔实参的逗号 —— 少这一层，函数体里带对象字面量的
  // 函数式 setter 会被误判成多实参而漏掉（ChatStream 的 setSections 就栽在这）。
  let braceDepth = 0;
  let bracketDepth = 0;
  let argStart = -1;
  let topLevelComma = false;
  let i = openIdx;
  while (i < code.length) {
    const c = code[i];
    if (c === '"' || c === "'") {
      i = skipString(code, i);
      if (i < 0) break;
      continue;
    }
    if (c === '`') {
      i = skipTemplate(code, i);
      if (i < 0) break;
      continue;
    }
    if (c === '/' && code[i + 1] === '/') {
      i = code.indexOf('\n', i);
      if (i < 0) break;
      continue;
    }
    if (c === '/' && code[i + 1] === '*') {
      const e = code.indexOf('*/', i + 2);
      if (e < 0) break;
      i = e + 2;
      continue;
    }
    if (c === '/' && isRegexPos(code, i)) {
      i = skipRegex(code, i);
      if (i < 0) break;
      continue;
    }
    if (c === '(') {
      depth++;
      if (depth === 1) {
        // 实参起始 = 括号后第一个非空白
        let j = i + 1;
        while (j < code.length && /\s/.test(code[j])) j++;
        argStart = j;
      }
    } else if (c === ')') {
      depth--;
      if (depth === 0) return { endIdx: i, argStart, topLevelComma };
    } else if (c === '{') {
      braceDepth++;
    } else if (c === '}') {
      braceDepth--;
    } else if (c === '[') {
      bracketDepth++;
    } else if (c === ']') {
      bracketDepth--;
    } else if (c === ',' && depth === 1 && braceDepth === 0 && bracketDepth === 0) {
      // 尾逗号（`setX(fn,\n)`，prettier 换行时的常见收尾）不是分隔符
      let j = i + 1;
      while (j < code.length && /\s/.test(code[j])) j++;
      if (code[j] !== ')') topLevelComma = true;
    }
    i++;
  }
  return null;
}

/**
 * 标出「字符串/模板/注释/正则内部」的位置（1 = 内部）。
 * 正则只认标识符 `set[A-Z](`，字符串里同样会出现这种文本（提示语、拼接），
 * 不剔除就会把字符串改写坏 —— 见 set-wrap.test.mjs 的「不误伤」用例。
 */
export function literalMask(code) {
  const mask = new Uint8Array(code.length);
  let i = 0;
  while (i < code.length) {
    const c = code[i];
    let end = -1;
    if (c === '"' || c === "'") end = skipString(code, i);
    else if (c === '`') end = skipTemplate(code, i);
    else if (c === '/' && code[i + 1] === '/') {
      const nl = code.indexOf('\n', i);
      end = nl < 0 ? code.length : nl;
    } else if (c === '/' && code[i + 1] === '*') {
      const close = code.indexOf('*/', i + 2);
      end = close < 0 ? code.length : close + 2;
    } else if (c === '/' && isRegexPos(code, i)) end = skipRegex(code, i);
    if (end > i) {
      mask.fill(1, i, end);
      i = end;
      continue;
    }
    i++;
  }
  return mask;
}

/** `/` 出现在这些字符之后只可能是正则开头（除法左操作数不会以它们结尾） */
function isRegexPos(code, i) {
  for (let j = i - 1; j >= 0; j--) {
    if (/\s/.test(code[j])) continue;
    return /[([{,;:=!&|?+\-*%~^<>]/.test(code[j]);
  }
  return true;
}

export function skipString(code, i) {
  const q = code[i];
  i++;
  while (i < code.length) {
    if (code[i] === '\\') {
      i += 2;
      continue;
    }
    if (code[i] === q) return i + 1;
    if (code[i] === '\n' && q !== '`') return -1; // 未闭合
    i++;
  }
  return -1;
}

function skipTemplate(code, i) {
  i++;
  while (i < code.length) {
    if (code[i] === '\\') {
      i += 2;
      continue;
    }
    if (code[i] === '`') return i + 1;
    if (code[i] === '$' && code[i + 1] === '{') {
      const end = skipBraced(code, i + 1);
      if (end < 0) return -1;
      i = end + 1;
      continue;
    }
    i++;
  }
  return -1;
}

function skipBraced(code, i) {
  let depth = 0;
  while (i < code.length) {
    const c = code[i];
    if (c === '"' || c === "'") {
      i = skipString(code, i);
      if (i < 0) return -1;
      continue;
    }
    if (c === '`') {
      i = skipTemplate(code, i);
      if (i < 0) return -1;
      continue;
    }
    if (c === '{') depth++;
    else if (c === '}') {
      depth--;
      if (depth === 0) return i;
    }
    i++;
  }
  return -1;
}

function skipRegex(code, i) {
  i++;
  let inClass = false;
  while (i < code.length) {
    const c = code[i];
    if (c === '\\') {
      i += 2;
      continue;
    }
    if (c === '\n') return -1;
    if (c === '[') inClass = true;
    else if (c === ']') inClass = false;
    else if (c === '/' && !inClass) return i + 1;
    i++;
  }
  return -1;
}

/**
 * `setX(整个实参)` → `setX(asUpdater(整个实参))`，只动函数式实参。
 * 区间递归：包裹之后仍要进实参内部找嵌套的 setter（`setA((p) => setB((q) => ...))`）。
 */
function wrapRange(code, from, to, rel, sites, re, mask) {
  const parts = [];
  let cursor = from;
  re.lastIndex = from;
  let m;
  while ((m = re.exec(code)) && m.index < to) {
    if (mask[m.index]) {
      re.lastIndex = m.index + m[0].length; // 字符串/注释里的同名文本，跳过
      continue;
    }
    const openIdx = m.index + m[0].length - 1;
    const scan = scanCall(code, openIdx);
    if (!scan) throw new Error(`[port] ${rel}: ${m[0]} 的括号扫不到配对，拒绝继续`);
    const { endIdx, argStart, topLevelComma } = scan;
    if (endIdx > to) {
      re.lastIndex = openIdx + 1; // 跨出本区间（外层调用），交给外层那一遍处理
      continue;
    }
    const inner = wrapRange(code, argStart, endIdx, rel, sites, re, mask);
    const arg = code.slice(argStart, endIdx).trimStart();
    // 函数式实参：带括号的箭头/async/function，或无括号单参箭头 `prev => ...`
    const isFn =
      /^(\(|async\b|function\b)/.test(arg) || /^[A-Za-z_$][\w$]*\s*=>/.test(arg);
    const already = arg.startsWith('asUpdater(');
    parts.push(code.slice(cursor, argStart));
    // 多实参一律不碰（setTimeout(fn, 100) 这类同样命中 set[A-Z]，但没有 updater 语义）
    if (isFn && !topLevelComma && !already) {
      parts.push('asUpdater(', inner, ')');
      sites.push(`${rel}:${code.slice(0, m.index).split('\n').length} ${m[0].slice(0, -1)}`);
    } else {
      parts.push(inner);
    }
    parts.push(')');
    cursor = endIdx + 1;
    re.lastIndex = cursor;
  }
  parts.push(code.slice(cursor, to));
  return parts.join('');
}

export function wrapFunctionalSetters(code, rel) {
  const sites = [];
  const re = /\bset([A-Z][A-Za-z0-9_]*)\(/g;
  const mask = literalMask(code);
  return { code: wrapRange(code, 0, code.length, rel, sites, re, mask), sites };
}

/** 把 reactCompat 的 import 插在最后一条顶层 import 之后（没 import 就放最前） */
export function ensureCompatImport(code, spec) {
  if (code.includes(`from '${spec}'`) || code.includes(`from "${spec}"`)) return code;
  const stmt = `import { asUpdater } from '${spec}';`;
  const re = /^import[\s\S]*?;[ \t]*$/gm;
  let last = null;
  let m;
  while ((m = re.exec(code))) last = m;
  if (!last) return `${stmt}\n${code}`;
  const at = last.index + last[0].length;
  return `${code.slice(0, at)}\n${stmt}${code.slice(at)}`;
}

/** 相对 arena 根算出 reactCompat 的 import 说明符 */
export function compatSpec(rel) {
  const depth = rel.split('/').length - 1;
  return depth === 0 ? './reactCompat' : `${'../'.repeat(depth)}reactCompat`;
}

