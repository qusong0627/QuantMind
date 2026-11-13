/**
 * 模型分数 ↔ K 线对齐的唯一前端实现。
 *
 * 口径（与后端 `backend/shared/score_window.py` 一致，改口径先改金样）：
 * - 分数日 D = 模型产出分数的数据日；
 * - 执行日 E = D 的下一交易日（T+1）；
 * - 目标日 X = D+1+N（T+20 → D+21）；后端已按交易日历算好 `target_date`；
 * - 预测窗口 = [E, X]，共 N 个交易日收益区间；
 * - 落点模式：
 *   - `prediction`（默认）：分数画在 D；
 *   - `resolution`：分数画在 X（后端给的 target_date），用于检验兑现。
 *
 * 本文件只做纯函数：不取数、不读日历、不生成假日期。目标日缺失（日历越界/
 * 非交易日）的点在 `resolution` 模式下直接丢弃，绝不用自然日推算。
 */

export type ScoreAnchorMode = 'prediction' | 'resolution';

export interface ScorePointLike {
  trade_date: string;
  /** 后端按交易日历算好的目标日；缺失 = 该点无法兑现落点 */
  target_date?: string | null;
  fusion_score: number | null;
  signal_side?: string | null;
}

export interface ScoreWindowLike {
  data_date: string;
  entry_date: string;
  target_date: string;
  horizon: number;
  execution_lag_days: number;
  window_dates: string[];
}

export interface AlignedScore {
  /** 图上的日期（prediction = trade_date；resolution = target_date） */
  date: string;
  value: number;
  /** 分数真实产生日（tooltip 用） */
  scoreDate: string;
  side: string | null;
}

/** ISO 日期字符串（YYYY-MM-DD）可直接字典序比较。 */
function isIsoDate(value: unknown): value is string {
  return typeof value === 'string' && /^\d{4}-\d{2}-\d{2}/.test(value);
}

/**
 * 把分数点按落点模式映射到图上日期。
 *
 * - 分数为 null / 日期非法 → 丢弃；
 * - `resolution` 模式没有 target_date → 丢弃；
 * - 同一天多个点（例如多次推理）→ 保留 scoreDate 最新的一条（后写覆盖）。
 */
export function buildScoreAlignment(
  points: ScorePointLike[],
  mode: ScoreAnchorMode,
): AlignedScore[] {
  const byDate = new Map<string, AlignedScore>();
  for (const p of points) {
    if (p == null || p.fusion_score == null || !Number.isFinite(Number(p.fusion_score))) continue;
    const scoreDate = isIsoDate(p.trade_date) ? p.trade_date.slice(0, 10) : '';
    if (!scoreDate) continue;
    const date = mode === 'resolution'
      ? (isIsoDate(p.target_date) ? p.target_date.slice(0, 10) : '')
      : scoreDate;
    if (!date) continue;
    const prev = byDate.get(date);
    if (prev && prev.scoreDate > scoreDate) continue;
    byDate.set(date, {
      date,
      value: Number(p.fusion_score),
      scoreDate,
      side: p.signal_side ? String(p.signal_side) : null,
    });
  }
  return [...byDate.values()].sort((a, b) => a.date.localeCompare(b.date));
}

/**
 * 把 K 线日期与预测窗口日期合并成一条时间轴（去重、升序）。
 * 只做字符串并集，不生成任何日期。
 */
export function extendTimeline(historyDates: string[], windowDates: string[]): string[] {
  const set = new Set<string>();
  for (const d of historyDates) if (isIsoDate(d)) set.add(d.slice(0, 10));
  for (const d of windowDates) if (isIsoDate(d)) set.add(d.slice(0, 10));
  return [...set].sort((a, b) => a.localeCompare(b));
}

/** 窗口里晚于最后一根 K 线的日期（用于延长 x 轴）。 */
export function futureDatesAfter(historyDates: string[], windowDates: string[]): string[] {
  const last = historyDates.length ? historyDates[historyDates.length - 1] : '';
  const set = new Set<string>();
  for (const d of windowDates) {
    if (isIsoDate(d)) {
      const day = d.slice(0, 10);
      if (!last || day > last) set.add(day);
    }
  }
  return [...set].sort((a, b) => a.localeCompare(b));
}

/**
 * 按 K 线周期选未来日期：
 * - 日 K：使用完整 window_dates（执行日 → 目标日）；
 * - 周/月：只取执行日/目标日两个真实交易日单点标注。客户端重采样无法把
 *   未来交易日可靠映射到周/月桶，宁可少画，也不伪造一根周/月 K 线。
 */
export function futureDatesForPeriod(
  period: 'daily' | 'weekly' | 'monthly',
  windowDates: string[],
  barDates: string[],
  entryDate?: string | null,
  targetDate?: string | null,
): string[] {
  const bars = new Set(barDates.filter(isIsoDate).map((d) => d.slice(0, 10)));
  const lastBar = [...bars].sort((a, b) => a.localeCompare(b)).pop() ?? '';
  const source = period === 'daily' ? windowDates : [entryDate, targetDate];
  const out = new Set<string>();
  for (const d of source) {
    if (!isIsoDate(d)) continue;
    const day = d.slice(0, 10);
    if (bars.has(day)) continue;
    // 周/月：过去的目标日不延长（预测已兑现或分数过期），否则原始日期会
    // 插进已有周/月桶之间，形成没有 K 线的空洞。日 K 保留重叠窗口用于对照。
    if (period !== 'daily' && lastBar && day <= lastBar) continue;
    out.add(day);
  }
  return [...out].sort((a, b) => a.localeCompare(b));
}

/** 最后一条有效分数（按 scoreDate 排序后取末条）。 */
export function latestScore(points: ScorePointLike[]): { value: number; date: string } | null {
  let out: { value: number; date: string } | null = null;
  for (const p of points) {
    if (p?.fusion_score == null || !Number.isFinite(Number(p.fusion_score))) continue;
    if (!isIsoDate(p.trade_date)) continue;
    const date = p.trade_date.slice(0, 10);
    if (!out || date >= out.date) out = { value: Number(p.fusion_score), date };
  }
  return out;
}

/**
 * 分数 → 分数隐含目标价。
 *
 * 只有 `target_mode === 'return'` 才允许换算；rank/score 等口径的分数不是
 * 收益率，换算出来的价格是编造的。缺失 lastClose 也返回 null。
 */
export function impliedTargetPrice(
  score: number | null | undefined,
  lastClose: number | null | undefined,
  targetMode?: string | null,
): number | null {
  if (score == null || !Number.isFinite(Number(score))) return null;
  if (lastClose == null || !Number.isFinite(Number(lastClose)) || Number(lastClose) <= 0) return null;
  if (String(targetMode || '').toLowerCase() !== 'return') return null;
  return Number(lastClose) * (1 + Number(score));
}

/** 目标日是否已在 K 线历史中兑现（用于 tooltip 显示实际收益）。 */
export function isWindowResolved(targetDate: string | null | undefined, lastBarDate: string): boolean {
  if (!isIsoDate(targetDate) || !isIsoDate(lastBarDate)) return false;
  return targetDate.slice(0, 10) <= lastBarDate.slice(0, 10);
}

/** 预测窗口是否全部落在最后一根 K 线之后（尚未兑现）。 */
export function isWindowPending(entryDate: string | null | undefined, lastBarDate: string): boolean {
  if (!isIsoDate(entryDate) || !isIsoDate(lastBarDate)) return false;
  return entryDate.slice(0, 10) > lastBarDate.slice(0, 10);
}
