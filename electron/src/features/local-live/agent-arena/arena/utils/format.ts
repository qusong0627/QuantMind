/** 数值格式化工具（终端风格：定宽数字、显式符号） */

export const fmtMoney = (v: number | null | undefined, currency = '$', digits = 0): string => {
  if (v == null || !Number.isFinite(v)) return '—';
  return `${currency}${v.toLocaleString('en-US', { maximumFractionDigits: digits, minimumFractionDigits: 0 })}`;
};

/** 带符号金额：盈利 `+¥5,705`、亏损 `-¥9`、零 `¥0`、拿不到 `—`。
 *
 *  为什么不用 fmtMoney：负数的负号会被塞进币种后面（`¥-9`），所以调用方常改写成
 *  `Math.abs()` —— 但那样一漏就把亏损显示成无符号金额（2026-09-11 持仓表实录：
 *  福恩股份 -¥8.8 显示为「¥9」，只有颜色不同）。符号统一在这里落字面。 */
export const fmtMoneySigned = (v: number | null | undefined, currency = '$', digits = 0): string => {
  if (v == null || !Number.isFinite(v)) return '—';
  const sign = v > 0 ? '+' : v < 0 ? '-' : '';
  return `${sign}${currency}${Math.abs(v).toLocaleString('en-US', { maximumFractionDigits: digits, minimumFractionDigits: 0 })}`;
};

export const fmtPct = (v: number | null | undefined, digits = 2, signed = true): string => {
  if (v == null || !Number.isFinite(v)) return '—';
  const sign = signed && v > 0 ? '+' : '';
  return `${sign}${(v * 100).toFixed(digits)}%`;
};

export const fmtNum = (v: number | null | undefined, digits = 2): string => {
  if (v == null || !Number.isFinite(v)) return '—';
  return v.toFixed(digits);
};

/** 价格格式化：最少 2 位、最多 3 位小数（¥118.92 / ¥1.995 / ¥5.268）。
 *  价格别用 fmtMoney —— 它按金额四舍五入到元，会抹掉小数（¥118.92 → ¥119）。 */
export const fmtPrice = (v: number | null | undefined, currency = '¥'): string => {
  if (v == null || !Number.isFinite(v) || v === 0) return '—';
  return `${currency}${v.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 3 })}`;
};

export const fmtDate = (d: string | null | undefined): string => {
  if (!d) return '—';
  return d.slice(0, 10);
};

export const pnlClass = (v: number | null | undefined): string => {
  if (v == null || !Number.isFinite(v)) return 'dim';
  if (v > 0) return 'up';
  if (v < 0) return 'down';
  return 'dim';
};
