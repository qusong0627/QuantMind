/** 时间显示工具（复盘口径：秒级一致优先）。
 *
 *  日志时间戳带时区（`2026-09-10T14:45:02.140557+08:00`）——一律按原始
 *  字符串截取展示，不做本地时区换算，避免与日志原文/券商回报对不上；
 *  只有「距今多久 / 星期几」这类相对值才做时间运算（按 UTC+8 日历日）。
 */

const WEEK = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];
const RE_TS = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}:\d{2}(?::\d{2})?)/;

interface Parts {
  y: number;
  m: number;
  d: number;
  clock: string; // HH:mm[:ss]
}

function partsOf(iso: string | null | undefined): Parts | null {
  if (!iso) return null;
  const m = String(iso).match(RE_TS);
  if (!m) return null;
  return { y: +m[1], m: +m[2], d: +m[3], clock: m[4] };
}

/** "2026-09-10T14:45:02.140557+08:00" → "2026-09-10 14:45:02" */
export function fmtDateTime(iso: string | null | undefined, withSeconds = true): string {
  const p = partsOf(iso);
  if (!p) return iso ? String(iso).slice(0, 19) : '—';
  const clock = withSeconds ? withTwoDigits(p.clock) : p.clock.slice(0, 5);
  return `${p.y}-${two(p.m)}-${two(p.d)} ${clock}`;
}

/** "…T14:45:02…" → "14:45:02"（同日时间列用） */
export function fmtClock(iso: string | null | undefined, withSeconds = true): string {
  const p = partsOf(iso);
  if (!p) return '—';
  return withSeconds ? withTwoDigits(p.clock) : p.clock.slice(0, 5);
}

/** 日历日（按 UTC+8 记录的日期部分，不做时区换算） */
export function fmtDay(iso: string | null | undefined): string {
  const p = partsOf(iso);
  if (!p) return iso ? String(iso).slice(0, 10) : '—';
  return `${p.y}-${two(p.m)}-${two(p.d)}`;
}

/** 星期几（按记录的日历日算，UTC 构造避免本地时区漂移） */
export function fmtWeekday(iso: string | null | undefined): string {
  const p = partsOf(iso);
  if (!p) return '';
  return WEEK[new Date(Date.UTC(p.y, p.m - 1, p.d)).getUTCDay()];
}

/** 距今时长（复盘看时效）：秒/分/小时/天。 */
export function fmtAgo(iso: string | null | undefined, nowMs: number = Date.now()): string {
  if (!iso) return '';
  const t = new Date(iso).getTime();
  if (!Number.isFinite(t)) return '';
  const sec = Math.max(0, Math.round((nowMs - t) / 1000));
  if (sec < 60) return `${sec} 秒前`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min} 分钟前`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr} 小时 ${min % 60} 分前`;
  const day = Math.floor(hr / 24);
  return `${day} 天 ${hr % 24} 小时前`;
}

/** 持仓时长（精确到分，替代原 "2D 0H" 粗粒度写法）。 */
export function fmtSpan(days: number | null | undefined): string {
  if (days == null || !Number.isFinite(days)) return '—';
  const mins = Math.round(days * 24 * 60);
  if (mins < 60) return `${mins} 分`;
  const h = Math.floor(mins / 60);
  const m = mins % 60;
  if (h < 24) return m ? `${h} 小时 ${m} 分` : `${h} 小时`;
  const d = Math.floor(h / 24);
  const rh = h % 24;
  return rh ? `${d} 天 ${rh} 小时` : `${d} 天`;
}

const two = (n: number) => String(n).padStart(2, '0');
/** "14:45" → "14:45:00"（补齐秒，列对齐） */
const withTwoDigits = (clock: string) => (clock.length === 5 ? `${clock}:00` : clock);
