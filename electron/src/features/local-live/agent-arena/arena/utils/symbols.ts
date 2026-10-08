/** 证券代码归一化 + 中文名解析（跨数据源代码格式差异）。
 *
 *  背景：同一只票在不同数据源里写法不一致——
 *    · 名称表 / 行情（quantdb）：`600519.SH`、`00700.HK`、`NVDA`
 *    · 通达信桥持仓：`SH600519`、`SZ000001`、`BJ920950`
 *    · 模拟盘成交 / 持仓快照：前缀后缀两种混用
 *  直接 `names[sym]` 会整列 miss（2026-09-10 详情页"股票名称不显示"根因）。
 *  这里把「名称表的键」和「要查的代码」两侧都展开成别名集合，
 *  任一侧的任意写法都能互相命中。
 */

export type NameMap = Record<string, string> | null | undefined;

/** 交易所别名 → 规范后缀（部分数据源把上交所写成 SS）。 */
const MKT_ALIAS: Record<string, string> = { SS: 'SH' };

const RE_SUFFIX = /^(\d{5,6})\.([A-Z]{2})$/; // 600519.SH / 00700.HK
const RE_PREFIX = /^([A-Z]{2})(\d{5,6})$/; // SH600519 / HK00700

/** A股/港股代码 → 交易所后缀（裸代码补全时可推断）。 */
function exchangeOf(code: string): string | null {
  if (code.length === 5) return 'HK';
  if (code.length !== 6) return null;
  if (/^(43|83|87|88|92)/.test(code)) return 'BJ'; // 北交所 / 新三板
  if (code[0] === '6') return 'SH';
  if (code[0] === '0' || code[0] === '3') return 'SZ';
  return null;
}

/** 单个代码的全部等价写法（大写、去空白后）。第一项是原样。 */
export function symbolAliases(raw: string | null | undefined): string[] {
  const s = String(raw ?? '').trim().toUpperCase().replace(/\s+/g, '');
  if (!s) return [];
  const out = new Set<string>([s]);

  const mSuffix = s.match(RE_SUFFIX);
  const mPrefix = s.match(RE_PREFIX);
  if (mSuffix || mPrefix) {
    const code = (mSuffix ? mSuffix[1] : (mPrefix as RegExpMatchArray)[2]) as string;
    const rawMkt = (mSuffix ? mSuffix[2] : (mPrefix as RegExpMatchArray)[1]) as string;
    const mkt = MKT_ALIAS[rawMkt] ?? rawMkt;
    out.add(`${code}.${mkt}`);
    out.add(`${mkt}${code}`);
    out.add(`${mkt}.${code}`);
    out.add(code);
    return [...out];
  }

  // 裸代码（600519 / 00700）：补全可推断的交易所写法
  const ex = exchangeOf(s);
  if (ex) {
    out.add(`${s}.${ex}`);
    out.add(`${ex}${s}`);
  }
  return [...out];
}

/** symbol（任意写法）→ 名称表里对应值的索引；按 names 对象缓存。 */
const INDEX_CACHE = new WeakMap<object, Map<string, string>>();
const EMPTY_INDEX: Map<string, string> = new Map();

function indexOf(names: NameMap): Map<string, string> {
  if (!names) return EMPTY_INDEX;
  const cached = INDEX_CACHE.get(names);
  if (cached) return cached;
  const idx = new Map<string, string>();
  for (const [key, value] of Object.entries(names)) {
    if (!value) continue;
    for (const alias of symbolAliases(key)) {
      if (!idx.has(alias)) idx.set(alias, value);
    }
  }
  INDEX_CACHE.set(names, idx);
  return idx;
}

/** symbol → 中文名；多格式匹配，找不到返回 undefined（调用方回退代码）。 */
export function stockName(names: NameMap, sym: string | null | undefined): string | undefined {
  if (!names || !sym) return undefined;
  const idx = indexOf(names);
  for (const alias of symbolAliases(sym)) {
    const hit = idx.get(alias);
    if (hit) return hit;
  }
  return undefined;
}

/** symbol → 展示名：有中文名用中文名，没有就原样回代码。 */
export function stockLabel(names: NameMap, sym: string | null | undefined): string {
  const code = sym == null ? '' : String(sym);
  return stockName(names, code) ?? code;
}

/** 两个代码是否指同一只票（成交与结构化决策按代码匹配时用）。 */
export function sameSymbol(a: string | null | undefined, b: string | null | undefined): boolean {
  const A = symbolAliases(a);
  if (!A.length) return false;
  const B = new Set(symbolAliases(b));
  return A.some((x) => B.has(x));
}
