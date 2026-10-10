/**
 * 北京时区（UTC+8，无夏令时）展示与交易时段的**唯一口径**（T6-3 审计 H7c）。
 *
 * 三条规则，先看字符串形态再决定怎么显示：
 * 1) **带时区**（`…Z` / `±HH:MM` / `±HHMM` / PG 文本形态 `…HH:MM:SS±HH`）→ 解析后换算成
 *    北京墙钟展示，设备时区无关；
 * 2) **无时区（naive）→ 原样展示**，不做任何换算——naive 多为写入侧已按北京墙钟落的值
 *    （TDX 桥 / 券商回报 / 日志原文），擅自 +8 会把本来对的时间改错；
 * 3) 解析失败 → 原字符串兜底，不吞不猜。
 *
 * 背景：此前多处 `slice(0,19)` / `slice(11,19)` / `replace('T',' ')` 裸截断，对 1) 类值
 * 是实锤错误面（UTC 当北京展示，差 8 小时）；统一收口到这里。
 * 台账侧（TradingHistory）本来就走 `utils/format.ts` 的上海格式化器，口径一致，不再重复。
 */

const AWARE_RE = /(?:Z|[+-]\d{2}:?\d{2})$/i;
/** PG `timestamptz::text` 形态的 2 位偏移（`2026-09-17 16:00:00.123+08`，必须跟在时刻后面）。 */
const AWARE_HH_RE = /[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?[+-]\d{2}$/;
const NAIVE_RE = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?/;
/** 1973 年（1e12 ms）前的 epoch 一定是秒——毫秒/秒时间戳自适应阈值，与 utils/format.ts 同口径。 */
const MS_THRESHOLD = 1e12;

const two = (n: number): string => String(n).padStart(2, '0');

export interface BeijingParts {
  y: number;
  m: number;
  d: number;
  hh: number;
  mm: number;
  ss: number;
}

/** 字符串是否带时区标记且可解析（naive 字符串返回 false）。 */
export function isAwareTimestamp(value: string): boolean {
  const s = value.trim();
  return (AWARE_RE.test(s) || AWARE_HH_RE.test(s)) && !Number.isNaN(Date.parse(s));
}

/**
 * naive 字符串的**墙钟字段**（原样口径，不做时区换算）；非 naive 形态 → null。
 * 供需要拿 naive 值做比较/格式化的调用方复用（如副驾驶 `eventTimeLabel` 的跨日判断）。
 */
export function naivePartsOf(value: string | null | undefined): BeijingParts | null {
  if (value === null || value === undefined) return null;
  const m = String(value).trim().match(NAIVE_RE);
  if (!m) return null;
  return { y: +m[1], m: +m[2], d: +m[3], hh: +m[4], mm: +m[5], ss: m[6] ? +m[6] : 0 };
}

/**
 * aware 时间戳（ISO 字符串 / epoch 秒 / epoch 毫秒 / Date）→ 北京墙钟 parts。
 * 用 UTC getter 读 `+8h` 平移后的 Date，结果与设备时区无关。
 * naive 字符串、不可解析值 → null（调用方走原样展示分支）。
 */
export function beijingPartsOf(value: string | number | Date | null | undefined): BeijingParts | null {
  if (value === null || value === undefined) return null;
  let ms: number;
  if (value instanceof Date) {
    ms = value.getTime();
  } else if (typeof value === 'number') {
    ms = value < MS_THRESHOLD ? value * 1000 : value;
  } else {
    const s = String(value).trim();
    if (!s) return null;
    if (/^\d+$/.test(s)) {
      const n = Number(s);
      ms = n < MS_THRESHOLD ? n * 1000 : n;
    } else if (isAwareTimestamp(s)) {
      ms = Date.parse(s);
    } else {
      return null;
    }
  }
  if (!Number.isFinite(ms)) return null;
  const bj = new Date(ms + 8 * 3600_000);
  return {
    y: bj.getUTCFullYear(),
    m: bj.getUTCMonth() + 1,
    d: bj.getUTCDate(),
    hh: bj.getUTCHours(),
    mm: bj.getUTCMinutes(),
    ss: bj.getUTCSeconds(),
  };
}

/**
 * 时间戳 → 「YYYY-MM-DD HH:mm[:ss]」：aware 换算北京、naive 原样截取、不可解析原样。
 * `withSeconds: false` 输出到分钟（替代旧 `slice(0,16).replace('T',' ')` 口径）。
 */
export function fmtBeijingDateTime(
  value: string | number | Date | null | undefined,
  opts: { withSeconds?: boolean } = {},
): string {
  if (value === null || value === undefined) return '—';
  const withSeconds = opts.withSeconds !== false;
  const p = beijingPartsOf(value);
  if (p) {
    const clock = withSeconds ? `${two(p.hh)}:${two(p.mm)}:${two(p.ss)}` : `${two(p.hh)}:${two(p.mm)}`;
    return `${p.y}-${two(p.m)}-${two(p.d)} ${clock}`;
  }
  const s = String(value).trim();
  const np = naivePartsOf(s);
  if (np) {
    const clock = withSeconds ? `${two(np.hh)}:${two(np.mm)}:${two(np.ss)}` : `${two(np.hh)}:${two(np.mm)}`;
    return `${np.y}-${two(np.m)}-${two(np.d)} ${clock}`;
  }
  return s || '—';
}

/** 只要时钟「HH:mm[:ss]」：aware 换算北京、naive 原样（替代旧 `slice(11,19)` 口径）。 */
export function fmtBeijingClock(
  value: string | number | Date | null | undefined,
  opts: { withSeconds?: boolean } = {},
): string {
  const full = fmtBeijingDateTime(value, opts);
  const idx = full.indexOf(' ');
  return idx >= 0 ? full.slice(idx + 1) : full;
}

/**
 * A 股交易时段（含集合竞价尾段与尾盘），按**北京墙钟**判定（设备时区无关）。
 * 用法：行情新鲜度的「已陈旧」徽标只在时段内计时——推送判重只比价格
 * （`quote_pusher._has_quote_changed`），安静标的没推送 ≠ 断流。
 */
export function isCnTradingHours(d: Date = new Date()): boolean {
  const bj = new Date(d.getTime() + 8 * 3600_000);
  const day = bj.getUTCDay();
  if (day === 0 || day === 6) return false;
  const m = bj.getUTCHours() * 60 + bj.getUTCMinutes();
  return (m >= 9 * 60 + 25 && m <= 11 * 60 + 35) || (m >= 12 * 60 + 55 && m <= 15 * 60 + 5);
}
