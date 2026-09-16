/**
 * 格式化工具函数
 */

/**
 * 格式化成交量
 * @param volume 成交量
 * @returns 格式化后的字符串
 */
export function formatVolume(volume: number): string {
  if (volume >= 100000000) {
    return `${(volume / 100000000).toFixed(2)}亿`;
  }
  if (volume >= 10000) {
    return `${(volume / 10000).toFixed(2)}万`;
  }
  return volume.toString();
}

/**
 * 格式化金额
 * @param amount 金额
 * @returns 格式化后的字符串
 */
export function formatAmount(amount: number): string {
  if (amount >= 100000000) {
    return `${(amount / 100000000).toFixed(2)}亿`;
  }
  if (amount >= 10000) {
    return `${(amount / 10000).toFixed(2)}万`;
  }
  return amount.toFixed(2);
}

/**
 * 格式化价格
 * @param price 价格
 * @param decimals 小数位数
 * @returns 格式化后的字符串
 */
export function formatPrice(price: number, decimals: number = 2): string {
  return price.toFixed(decimals);
}

/**
 * 格式化百分比
 * @param percent 百分比
 * @param showSign 是否显示正负号
 * @returns 格式化后的字符串
 */
export function formatPercent(percent: number, showSign: boolean = true): string {
  const sign = showSign && percent > 0 ? '+' : '';
  return `${sign}${percent.toFixed(2)}%`;
}

/**
 * 格式化时间
 * @param timestamp 时间戳（毫秒）
 * @returns 格式化后的时间字符串
 */
export function formatTime(timestamp: number): string {
  const date = new Date(timestamp);
  const hours = date.getHours().toString().padStart(2, '0');
  const minutes = date.getMinutes().toString().padStart(2, '0');
  const seconds = date.getSeconds().toString().padStart(2, '0');
  return `${hours}:${minutes}:${seconds}`;
}

const SHANGHAI_TIME_ZONE = 'Asia/Shanghai';
const TZ_AWARE_SUFFIX_RE = /(Z|[+-]\d{2}:?\d{2})$/i;
// 小数位最多 9 位：Python isoformat 默认微秒 6 位。只写 {1,3} 时
// `2026-09-14T07:54:25.123456` 匹配失败，落到 `new Date()` 被当成本地时间，
// 仪表盘就会把 UTC 07:54 显示成 07:54 而不是上海 15:54。
const NAIVE_DATE_TIME_RE =
  /^(\d{4})-(\d{2})-(\d{2})(?:[T\s](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,9}))?)?)?$/;

const buildShangHaiFormatter = (options: Intl.DateTimeFormatOptions): Intl.DateTimeFormat => (
  new Intl.DateTimeFormat('zh-CN', {
    timeZone: SHANGHAI_TIME_ZONE,
    ...options,
  })
);

/**
 * 解析后端时间戳
 *
 * 后端很多时间字段会以“无时区 ISO 字符串”返回，这类值在 JS 中会被当成本地时间解析，
 * 导致上海时区页面出现偏移。这里统一将无时区字符串视为 UTC，再转换到上海时间展示。
 */
export function parseBackendTimestamp(value: string | number | Date | null | undefined): Date | null {
  if (value === null || value === undefined) {
    return null;
  }

  if (value instanceof Date) {
    return Number.isNaN(value.getTime()) ? null : value;
  }

  if (typeof value === 'number') {
    const timestamp = value < 1e12 ? value * 1000 : value;
    const parsed = new Date(timestamp);
    return Number.isNaN(parsed.getTime()) ? null : parsed;
  }

  const text = String(value).trim();
  if (!text) {
    return null;
  }

  if (/^\d+$/.test(text)) {
    const numeric = Number(text);
    const timestamp = numeric < 1e12 ? numeric * 1000 : numeric;
    const parsed = new Date(timestamp);
    return Number.isNaN(parsed.getTime()) ? null : parsed;
  }

  if (TZ_AWARE_SUFFIX_RE.test(text)) {
    const parsed = new Date(text);
    return Number.isNaN(parsed.getTime()) ? null : parsed;
  }

  const normalized = text.replace(' ', 'T');
  const match = normalized.match(NAIVE_DATE_TIME_RE);
  if (match) {
    const [
      ,
      year,
      month,
      day,
      hour = '00',
      minute = '00',
      second = '00',
      fraction = '0',
    ] = match;
    const parsed = new Date(Date.UTC(
      Number(year),
      Number(month) - 1,
      Number(day),
      Number(hour),
      Number(minute),
      Number(second),
      Number(fraction.padEnd(3, '0').slice(0, 3)),
    ));
    return Number.isNaN(parsed.getTime()) ? null : parsed;
  }

  const fallback = new Date(text);
  return Number.isNaN(fallback.getTime()) ? null : fallback;
}

/**
 * 按上海时区格式化后端时间
 */
export function formatBackendTime(
  value: string | number | Date | null | undefined,
  options: {
    withSeconds?: boolean;
  } = {},
): string {
  const date = parseBackendTimestamp(value);
  if (!date) {
    return '--';
  }

  return buildShangHaiFormatter({
    hour: '2-digit',
    minute: '2-digit',
    second: options.withSeconds ? '2-digit' : undefined,
    hour12: false,
  }).format(date);
}

/**
 * 按上海时区格式化后端日期时间
 */
export function formatBackendDateTime(value: string | number | Date | null | undefined): string {
  const date = parseBackendTimestamp(value);
  if (!date) {
    return '--';
  }

  return buildShangHaiFormatter({
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(date);
}

/**
 * 格式化日期
 * @param timestamp 时间戳（毫秒）
 * @returns 格式化后的日期字符串
 */
export function formatDate(timestamp: number): string {
  const date = new Date(timestamp);
  const year = date.getFullYear();
  const month = (date.getMonth() + 1).toString().padStart(2, '0');
  const day = date.getDate().toString().padStart(2, '0');
  return `${year}-${month}-${day}`;
}

/**
 * 获取相对时间描述
 * @param timestamp 时间戳（毫秒）
 * @returns 相对时间描述
 */
export function getRelativeTime(timestamp: number): string {
  const now = Date.now();
  const diff = now - timestamp;
  const seconds = Math.floor(diff / 1000);

  if (seconds < 60) {
    return `${seconds}秒前`;
  }

  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) {
    return `${minutes}分钟前`;
  }

  const hours = Math.floor(minutes / 60);
  if (hours < 24) {
    return `${hours}小时前`;
  }

  const days = Math.floor(hours / 24);
  return `${days}天前`;
}
