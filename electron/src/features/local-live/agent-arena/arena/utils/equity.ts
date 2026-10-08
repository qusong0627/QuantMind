/** 分账净值序列统计（通达信桥实盘口径）。
 *
 *  数据源 /api/live/equity 的 per-agent 采样：约分钟级、跨交易日（8/31 起 2000+ 点）。
 *  复盘口径与后端 summary 对齐：
 *  - 区间收益 = 末值 / 首值 − 1
 *  - 最大回撤取正数（与 /performance 的 max_drawdown 一致，展示为跌幅大小）
 *  - 夏普用「每日最后一个采样」当收盘价算日频收益，年化 ×√252
 */

export interface EquityPoint {
  date?: string;
  ts: string;
  value: number;
}

export interface DayClose {
  date: string;
  value: number;
}

export interface SeriesStats {
  /** 采样点数（非交易日数） */
  points: number;
  from: string;
  to: string;
  first: number;
  last: number;
  /** 区间收益，缺样本时为 null */
  ret: number | null;
  /** 最大回撤（正数 = 跌幅），如 0.083 表示 −8.3% */
  maxDrawdown: number | null;
  /** 年化夏普（日频），日收益样本不足 3 个或波动为 0 时 null */
  sharpe: number | null;
}

const TRADING_DAYS = 252;
const MIN_RETURNS = 3;
/** 日波动低于 0.0001% 视为无波动（浮点噪声会让夏普炸到 1e16） */
const MIN_STD = 1e-6;

const dayOf = (p: EquityPoint): string => p.date ?? String(p.ts ?? '').slice(0, 10);

/** 每日收盘：同一天取最后一个采样点（序列按时间升序） */
export function dailyCloses(points: EquityPoint[]): DayClose[] {
  const out: DayClose[] = [];
  for (const p of points ?? []) {
    const d = dayOf(p);
    if (!d || !Number.isFinite(p.value)) continue;
    const last = out[out.length - 1];
    if (last && last.date === d) last.value = p.value;
    else out.push({ date: d, value: p.value });
  }
  return out;
}

/** 时间戳 → 序列中就近采样点下标（二分；空序列返回 -1）。
 *  成交标记吸附用：成交时刻未必落在采样点上，取时间最近的点画标记。 */
export function nearestIdxOfTime(pts: { t: number }[], t: number): number {
  const n = pts?.length ?? 0;
  if (!n) return -1;
  let lo = 0;
  let hi = n - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (pts[mid].t < t) lo = mid + 1;
    else hi = mid;
  }
  if (lo > 0 && Math.abs(pts[lo - 1].t - t) <= Math.abs(pts[lo].t - t)) return lo - 1;
  return lo;
}

/** 时间戳两侧的相邻采样点下标：pre = 最后一个 < t 的点，post = 第一个 ≥ t 的点
 *  （二分；任一侧不存在返回 -1）。
 *  用途：量「对账台阶」——对账时刻前后两点的净值差，就是这笔校正在本线图上
 *  跳了多少（2026-09-08 实录：pro 线 13:25 一步 -¥14,026，是归位不是暴跌）。 */
export function stepAroundTime(pts: { t: number }[], t: number): { pre: number; post: number } {
  const n = pts?.length ?? 0;
  if (!n) return { pre: -1, post: -1 };
  let lo = 0;
  let hi = n; // 下界：第一个 ≥ t 的下标
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (pts[mid].t < t) lo = mid + 1;
    else hi = mid;
  }
  return { pre: lo - 1, post: lo < n ? lo : -1 };
}

/** 序列回撤（正数）：从峰值回落的百分比 */
function maxDrawdownOf(values: number[]): number | null {
  if (values.length < 2) return null;
  let peak = -Infinity;
  let worst = 0;
  for (const v of values) {
    if (v > peak) peak = v;
    if (peak > 0) worst = Math.min(worst, v / peak - 1);
  }
  return Math.abs(worst);
}

/** 年化夏普：日频简单收益的均值 / 样本标准差 × √252 */
function sharpeOf(closes: DayClose[]): number | null {
  const rets: number[] = [];
  for (let i = 1; i < closes.length; i += 1) {
    const prev = closes[i - 1].value;
    if (prev > 0) rets.push(closes[i].value / prev - 1);
  }
  if (rets.length < MIN_RETURNS) return null;
  const mean = rets.reduce((a, b) => a + b, 0) / rets.length;
  const variance = rets.reduce((a, b) => a + (b - mean) ** 2, 0) / (rets.length - 1);
  const sd = Math.sqrt(variance);
  if (!(sd > MIN_STD)) return null;
  return (mean / sd) * Math.sqrt(TRADING_DAYS);
}

/** 汇总净值序列：首末/区间收益/最大回撤/夏普。有效点不足 2 个返回 null。 */
export function seriesStats(points: EquityPoint[]): SeriesStats | null {
  const valid = (points ?? []).filter((p) => Number.isFinite(p?.value) && dayOf(p));
  if (valid.length < 2) return null;
  const values = valid.map((p) => p.value);
  const first = values[0];
  const last = values[values.length - 1];
  return {
    points: valid.length,
    from: dayOf(valid[0]),
    to: dayOf(valid[valid.length - 1]),
    first,
    last,
    ret: first > 0 ? last / first - 1 : null,
    maxDrawdown: maxDrawdownOf(values),
    sharpe: sharpeOf(dailyCloses(valid)),
  };
}
