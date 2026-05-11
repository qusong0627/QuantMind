export const safeNum = (value: unknown, fallback = 0): number =>
  typeof value === 'number' && Number.isFinite(value) ? value : fallback;

export const normalizeSymbol = (raw: string): string => {
  const s = (raw || '').trim().toUpperCase();
  if (!s) return s;
  if (/^(SH|SZ|BJ)\d{6}$/.test(s)) return s;
  const suffixMatch = s.match(/^(\d{6})\.(SH|SZ|BJ)$/);
  if (suffixMatch) return `${suffixMatch[2]}${suffixMatch[1]}`;
  if (/^\d{6}$/.test(s)) {
    if (s.startsWith('6') || s.startsWith('68') || s.startsWith('90')) return `SH${s}`;
    if (s.startsWith('4') || s.startsWith('8') || s.startsWith('9')) return `BJ${s}`;
    return `SZ${s}`;
  }
  return s;
};

export const normalizeRoe = (value: unknown): number => {
  let v = safeNum(value, 0);
  if (Math.abs(v) > 200) v = v / 100;
  return v;
};

export const normalizeYiValue = (value: unknown): number => {
  const v = safeNum(value, 0);
  return Math.abs(v) >= 1_000_000 ? v / 100_000_000 : v;
};

export const fmt2 = (value: unknown): string => safeNum(value, 0).toFixed(2);
export const fmtPercent2 = (value: unknown): string => `${safeNum(value, 0).toFixed(2)}%`;

export const fmtSignedPercent2 = (value: unknown): string => {
  const v = safeNum(value, 0);
  return `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`;
};

export const fmtNullableSignedPercent2 = (value: unknown): string =>
  value === null || value === undefined ? '-' : fmtSignedPercent2(value);

export const fmtMainFlowCn = (value: unknown): string => {
  const v = safeNum(value, 0); // 后端口径：百万
  if (Math.abs(v) >= 10000) return `${(v / 10000).toFixed(2)}亿`;
  return `${v.toFixed(2)}百万`;
};
