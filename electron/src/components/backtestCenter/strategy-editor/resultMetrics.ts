/**
 * 策略编辑工作台的纯计算口径（不依赖 React，便于单测）。
 *
 * 颜色口径：A 股「红涨绿跌」——正收益红、负收益绿；评分是质量分不是涨跌，
 * 用中性靛蓝，绝不套红绿（与全站 signalVocabulary / heatmapColor 一致）。
 */
import type { StrategyLabEquityPoint, StrategyLabRunResult } from '../../../features/strategy-lab/types';
import type { SavedBacktestRun } from '../../../utils/strategyEditorSavedRuns';

export const SCORE_NEUTRAL = '#6366f1';

export function formatPct(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return `${(value * 100).toFixed(digits)}%`;
}

export function formatSignedPct(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  const pct = value * 100;
  return `${pct > 0 ? '+' : ''}${pct.toFixed(digits)}%`;
}

export function formatNumber(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  const fixed = value.toFixed(digits);
  // 避免 -0.00：负零不是有效读数，显示成 0.00 才不误导。
  return /^-0(?:\.0+)?$/.test(fixed) ? fixed.slice(1) : fixed;
}

/**
 * 回撤口径归一化：minibt 返回正数（31.05 表示 -31.05%），qlib 返回负数。
 * 展示与评分统一按「负数跌幅」处理，避免正数被当成「回撤 < 10%」。
 */
export function normalizeDrawdown(value: number | null | undefined): number | null {
  if (value === null || value === undefined || Number.isNaN(value)) return null;
  return -Math.abs(value);
}

export function formatMoney(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return value.toLocaleString('zh-CN', { maximumFractionDigits: 0 });
}

/** 正负号着色：仅用于收益/超额/PnL 这类「方向」数值。 */
export function signedToneClass(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value) || value === 0) {
    return 'text-slate-800';
  }
  return value > 0 ? 'text-red-500' : 'text-emerald-600';
}

export interface StrategyScore {
  score: number;
  grade: '优' | '良' | '可' | '弱' | '高风险';
  notes: string[];
}

/**
 * 快速体检分：口径与 Strategy Lab 的 ScoreCard 保持一致（夏普 30 + 年化 25 +
 * 回撤 25 + 样本量 20），避免同一份结果在两个入口给出不同分数。
 */
export function computeStrategyScore(result: StrategyLabRunResult | null): StrategyScore | null {
  if (!result) return null;
  const m = result.metrics;
  let score = 0;
  const notes: string[] = [];

  if (m.sharpe >= 1.5) {
    score += 30;
    notes.push('夏普 ≥ 1.5：优');
  } else if (m.sharpe >= 1.0) {
    score += 22;
    notes.push('夏普 ≥ 1.0：良');
  } else if (m.sharpe >= 0.5) {
    score += 14;
    notes.push('夏普 0.5–1.0：可');
  } else if (m.sharpe > 0) {
    score += 6;
    notes.push('夏普 > 0：偏弱');
  } else {
    notes.push('夏普 ≤ 0：警告');
  }

  if (m.annual_return > 0.3) {
    score += 25;
  } else if (m.annual_return > 0.15) {
    score += 18;
  } else if (m.annual_return > 0.08) {
    score += 12;
  } else if (m.annual_return > 0) {
    score += 5;
  } else {
    notes.push('年化收益为负');
  }

  const drawdown = normalizeDrawdown(m.max_drawdown) ?? 0;
  if (drawdown > -0.1) {
    score += 25;
    notes.push('回撤 < 10%：稳');
  } else if (drawdown > -0.2) {
    score += 18;
  } else if (drawdown > -0.3) {
    score += 10;
  } else {
    notes.push('回撤 > 30%：高风险');
  }

  if (m.n_trades >= 50) {
    score += 20;
  } else if (m.n_trades >= 20) {
    score += 12;
  } else if (m.n_trades >= 5) {
    score += 6;
    notes.push('交易次数偏少，结论不稳');
  } else {
    notes.push('交易 < 5 次，统计不显著');
  }

  const finalScore = Math.max(0, Math.min(100, score));
  const grade: StrategyScore['grade'] =
    finalScore >= 75 ? '优' : finalScore >= 55 ? '良' : finalScore >= 35 ? '可' : finalScore >= 15 ? '弱' : '高风险';
  return { score: finalScore, grade, notes };
}

export interface EquitySummary {
  startDate: string;
  endDate: string;
  tradingDays: number;
  /** 期末 / 期初，与净值图同口径（无量纲）。 */
  finalNetValue: number;
  /** 序列最后一个原始权益值（minibt 是金额，qlib 可能是归一值，仅作参考）。 */
  finalEquity: number;
  benchmarkReturn: number | null;
  alpha: number | null;
  monthlyWinRate: number | null;
  maxDrawdownDays: number;
}

/** 净值序列的派生读数；全部为「缺失返回 null」，不拿 0 冒充。 */
export function computeEquitySummary(equity: StrategyLabEquityPoint[]): EquitySummary | null {
  if (!equity?.length) return null;
  const first = equity[0];
  const last = equity[equity.length - 1];
  const totalReturn = first.value ? last.value / first.value - 1 : null;

  let benchmarkReturn: number | null = null;
  if (first.benchmark !== null && first.benchmark !== undefined && first.benchmark !== 0 && last.benchmark !== null && last.benchmark !== undefined) {
    benchmarkReturn = last.benchmark / first.benchmark - 1;
  }
  const alpha = benchmarkReturn === null || totalReturn === null ? null : totalReturn - benchmarkReturn;

  // 最长「水下」连续交易日（从峰值未收复）
  let peak = first.value;
  let underwaterDays = 0;
  let maxUnderwaterDays = 0;
  for (const point of equity) {
    if (point.value >= peak) {
      peak = point.value;
      underwaterDays = 0;
      continue;
    }
    underwaterDays += 1;
    if (underwaterDays > maxUnderwaterDays) maxUnderwaterDays = underwaterDays;
  }

  const months = computeMonthlyReturns(equity);
  const finished = months.flatMap((row) => row.months.filter((m) => m.ret !== null));
  const monthlyWinRate = finished.length
    ? finished.filter((m) => (m.ret as number) > 0).length / finished.length
    : null;

  return {
    startDate: first.date,
    endDate: last.date,
    tradingDays: equity.length,
    finalNetValue: first.value ? last.value / first.value : 1,
    finalEquity: last.value,
    benchmarkReturn,
    alpha,
    monthlyWinRate,
    maxDrawdownDays: maxUnderwaterDays,
  };
}

export interface MonthlyCell {
  month: number;
  ret: number | null;
}

export interface MonthlyRow {
  year: number;
  months: MonthlyCell[];
  yearReturn: number | null;
}

/** 月度收益矩阵：每月最后一点 / 上月最后一点 - 1；首月沿用当年首个点。 */
export function computeMonthlyReturns(equity: StrategyLabEquityPoint[]): MonthlyRow[] {
  if (!equity?.length) return [];
  const monthMap = new Map<string, { first: number; last: number }>();
  for (const point of equity) {
    const key = point.date.slice(0, 7);
    if (!key) continue;
    const bucket = monthMap.get(key);
    if (!bucket) monthMap.set(key, { first: point.value, last: point.value });
    else bucket.last = point.value;
  }
  const keys = Array.from(monthMap.keys()).sort();
  const rows = new Map<number, MonthlyRow>();

  keys.forEach((key, index) => {
    const year = Number(key.slice(0, 4));
    const month = Number(key.slice(5, 7));
    const bucket = monthMap.get(key)!;
    const prev = index === 0 ? null : monthMap.get(keys[index - 1])?.last ?? null;
    const base = prev ?? bucket.first;
    const ret = base ? bucket.last / base - 1 : null;
    if (!rows.has(year)) {
      rows.set(year, {
        year,
        months: Array.from({ length: 12 }, (_, i) => ({ month: i + 1, ret: null })),
        yearReturn: null,
      });
    }
    const row = rows.get(year)!;
    row.months[month - 1] = { month, ret };
  });

  // 年度收益按年内首末点计算（与月度口径独立，避免首月基准跨年造成串味）
  const yearBuckets = new Map<number, { first: number; last: number }>();
  for (const point of equity) {
    const year = Number(point.date.slice(0, 4));
    if (!Number.isFinite(year)) continue;
    const bucket = yearBuckets.get(year);
    if (!bucket) yearBuckets.set(year, { first: point.value, last: point.value });
    else bucket.last = point.value;
  }
  return Array.from(rows.values())
    .sort((a, b) => a.year - b.year)
    .map((row) => {
      const bucket = yearBuckets.get(row.year);
      return {
        ...row,
        yearReturn: bucket && bucket.first ? bucket.last / bucket.first - 1 : null,
      };
    });
}

export interface BenchmarkKlinePoint {
  date: string;
  close: number;
}

export interface AlignedBuyHold {
  /** 与 equity 日期对齐的归一化持有净值（首日 = 1），只包含有基准数据的日期。 */
  series: Array<{ date: string; value: number }>;
  /** 对齐窗口内的策略收益（首/末有效点）。 */
  strategyReturn: number;
  /** 对齐窗口内的买入持有收益。 */
  benchmarkReturn: number;
  alpha: number;
}

/**
 * 把「买入持有」K 线对齐到策略净值日期：
 * - 只取基准有数据的日期（基准上市/数据起点晚于策略时，窗口自动收窄）；
 * - 停牌/缺日按前值填充（持有者不会因为停牌卖出）；
 * - 归一化以对齐窗口首日收盘价为 1。
 */
export function alignBuyHoldBenchmark(
  equity: StrategyLabEquityPoint[],
  kline: BenchmarkKlinePoint[],
): AlignedBuyHold | null {
  if (!equity?.length || !kline?.length) return null;
  const closeByDate = new Map<string, number>();
  for (const point of kline) {
    const date = String(point.date || '').slice(0, 10);
    if (date && Number.isFinite(point.close) && point.close > 0) closeByDate.set(date, point.close);
  }

  let lastClose: number | null = null;
  const aligned: Array<{ date: string; equityValue: number; close: number }> = [];
  for (const point of equity) {
    const date = String(point.date || '').slice(0, 10);
    const close = closeByDate.get(date);
    if (close !== undefined) lastClose = close;
    if (lastClose === null) continue;
    if (!Number.isFinite(point.value) || point.value <= 0) continue;
    aligned.push({ date, equityValue: point.value, close: lastClose });
  }

  if (aligned.length < 2) return null;
  const first = aligned[0];
  const last = aligned[aligned.length - 1];
  if (!first.close || !first.equityValue) return null;

  const strategyReturn = last.equityValue / first.equityValue - 1;
  const benchmarkReturn = last.close / first.close - 1;
  return {
    series: aligned.map((row) => ({ date: row.date, value: row.close / first.close })),
    strategyReturn,
    benchmarkReturn,
    alpha: strategyReturn - benchmarkReturn,
  };
}

export interface SavedRunMetrics {
  cumReturn: number | null;
  sharpe: number | null;
  maxDrawdown: number | null;
}

/** 存档结果的指标提取：兼容 StrategyLabRunResult 与 qlib BacktestResult 两种形状。 */
export function extractSavedRunMetrics(run: SavedBacktestRun): SavedRunMetrics {
  const asNumber = (value: unknown): number | null =>
    typeof value === 'number' && Number.isFinite(value) ? value : null;
  const result = (run.result || {}) as Record<string, unknown>;
  const rawMetrics = result.metrics;
  if (rawMetrics && typeof rawMetrics === 'object') {
    const metrics = rawMetrics as Record<string, unknown>;
    return {
      cumReturn: asNumber(metrics.cum_return),
      sharpe: asNumber(metrics.sharpe),
      maxDrawdown: asNumber(metrics.max_drawdown),
    };
  }
  return {
    cumReturn: asNumber(result.total_return) ?? asNumber(result.cumulative_return),
    sharpe: asNumber(result.sharpe_ratio),
    maxDrawdown: asNumber(result.max_drawdown),
  };
}

/** 参数面板的校验：只拦真正会产出错误回测的组合。 */
export interface ParamValidation {
  dateInvalid: boolean;
  fastSlowInvalid: boolean;
  symbolMissing: boolean;
  canApply: boolean;
  messages: string[];
}

export function validateBacktestParams(input: {
  symbol: string;
  startDate: string;
  endDate: string;
  fast: number;
  slow: number;
  isMinibt: boolean;
}): ParamValidation {
  const messages: string[] = [];
  // 只有 minibt 会把 SYMBOL 写回代码；截面策略的标的由股票池 + topk 决定。
  const symbolMissing = input.isMinibt && !String(input.symbol || '').trim();
  if (symbolMissing) messages.push('标的代码不能为空');
  const dateInvalid = !input.startDate || !input.endDate || input.startDate >= input.endDate;
  if (dateInvalid) messages.push('结束日期必须晚于开始日期');
  const fastSlowInvalid = input.isMinibt && Number(input.fast) >= Number(input.slow);
  if (fastSlowInvalid) messages.push('快线必须小于慢线（fast < slow）');
  return {
    symbolMissing,
    dateInvalid,
    fastSlowInvalid,
    canApply: messages.length === 0,
    messages,
  };
}
