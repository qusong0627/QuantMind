/** 个股终端跨市场共用的小工具：代码格式归一与数值格式化 */

/** suffix(600519.SH) -> prefix(SH600519)，自选表用 prefix 格式；无后缀时原样返回（美股 ticker 即此情形） */
export function toPrefix(symbol: string): string {
  const [code, ex] = symbol.split('.');
  return ex && code ? `${ex}${code}` : symbol;
}

/** 涨跌幅格式化：+1.23% */
export function fmtPct(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return '--';
  return `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`;
}

/** 市值格式化：亿元口径（A 股/港股）；美股请用 fmtCapUsd */
export function fmtMv(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return '--';
  if (v >= 10000) return `${(v / 10000).toFixed(1)}万亿`;
  return `${v.toFixed(0)}亿`;
}

/** 市值格式化：美元（美股 f10 的 market_cap 是原始美元值） */
export function fmtCapUsd(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return '--';
  const abs = Math.abs(v);
  if (abs >= 1e12) return `$${(v / 1e12).toFixed(2)}万亿`;
  if (abs >= 1e8) return `$${(v / 1e8).toFixed(0)}亿`;
  if (abs >= 1e4) return `$${(v / 1e4).toFixed(1)}万`;
  return `$${v.toFixed(0)}`;
}
