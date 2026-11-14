/** 大盘全景看板的统一格式化与颜色语义（A 股口径：红涨绿跌）。 */

export function isNum(v: unknown): v is number {
  return typeof v === 'number' && Number.isFinite(v);
}

export function fmtPct(v: number | null | undefined, digits = 2): string {
  if (!isNum(v)) return '—';
  return `${v > 0 ? '+' : ''}${v.toFixed(digits)}%`;
}

export function fmtPrice(v: number | null | undefined): string {
  if (!isNum(v)) return '—';
  return v.toFixed(2);
}

export function fmtCount(v: number | null | undefined): string {
  if (!isNum(v)) return '—';
  return v.toLocaleString('zh-CN');
}

/** 输入单位：元。 */
export function fmtYiFromYuan(v: number | null | undefined, digits = 1): string {
  if (!isNum(v)) return '—';
  return fmtYi(v / 1e8, digits);
}

/** 输入单位：亿元。 */
export function fmtYi(v: number | null | undefined, digits = 1): string {
  if (!isNum(v)) return '—';
  const d = Math.abs(v) >= 100 ? 0 : digits;
  return `${v > 0 ? '+' : ''}${v.toFixed(d)} 亿`;
}

/** 输入 0~1 的比例（如 main_breadth / main_net_ratio）。 */
export function fmtRatioPct(v: number | null | undefined, digits = 1): string {
  if (!isNum(v)) return '—';
  return `${(v * 100).toFixed(digits)}%`;
}

export function toneBySign(v: number | null | undefined): string {
  if (!isNum(v) || v === 0) return 'text-slate-500';
  return v > 0 ? 'text-rose-600' : 'text-emerald-600';
}

export function bgBySign(v: number | null | undefined): string {
  if (!isNum(v) || v === 0) return 'bg-slate-400';
  return v > 0 ? 'bg-rose-500' : 'bg-emerald-500';
}

export function clamp(v: number, min = 0, max = 100): number {
  return Math.min(max, Math.max(min, v));
}
