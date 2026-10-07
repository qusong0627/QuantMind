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

/**
 * ROE 统一口径：百分数（如 11.76 表示 11.76%）。
 *
 * 与后端 `research_service._format_candidate_record` 的契约完全一致：
 * 小数（|v| <= 1.5，如 0.1192）视为「小数」→ ×100 转百分数；
 * 其余（已是百分数，如 QuantDB `fun_roe`）原样返回。全局只有这一条规则。
 */
export const normalizeRoe = (value: unknown): number => {
  const v = safeNum(value, 0);
  return Math.abs(v) <= 1.5 ? v * 100 : v;
};

export const fmt2 = (value: unknown): string => safeNum(value, 0).toFixed(2);
export const fmtPercent2 = (value: unknown): string => `${safeNum(value, 0).toFixed(2)}%`;

export const fmtSignedPercent2 = (value: unknown): string => {
  const v = safeNum(value, 0);
  return `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`;
};

export const fmtNullableSignedPercent2 = (value: unknown): string =>
  value === null || value === undefined ? '-' : fmtSignedPercent2(value);

/**
 * 用于 PE / ROE / RSI / 均线 / 市值这类“0 在现实中不可能”的指标。
 *
 * PG `stock_daily_latest` 在近期交易日未回填这些列，序列化后 NULL 变成了 0，
 * 于是详情页出现 “PE 0.0 / ROE 0.0% / RSI 0.0” 这种看起来像真实数据的假值。
 * 这里把 0 一并显示为 “-”，避免把缺失当成极端估值误导判断。
 */
export const fmtPositiveOrDash = (value: unknown, decimals = 2, suffix = ''): string => {
  if (value === null || value === undefined) return '-';
  const v = safeNum(value, 0);
  if (!Number.isFinite(v) || v === 0) return '-';
  return `${v.toFixed(decimals)}${suffix}`;
};