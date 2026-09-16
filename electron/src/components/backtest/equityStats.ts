/** 资金曲线区间统计（T-FE-09）：纯函数（可单测，无副作用）。口径与体检九项同族，rf=0。 */

export interface RangeStats {
  points: number;
  totalReturn: number | null;
  annualized: number | null;
  maxDrawdown: number | null;
  volatility: number | null;
  sharpe: number | null;
  benchmarkReturn: number | null;
  excess: number | null;
}

const TRADING_DAYS = 252;

function returnsFromValues(values: number[]): number[] {
  const rets: number[] = [];
  for (let i = 1; i < values.length; i += 1) {
    const prev = values[i - 1];
    const cur = values[i];
    if (prev > 0 && Number.isFinite(prev) && Number.isFinite(cur)) {
      rets.push(cur / prev - 1);
    }
  }
  return rets;
}

/** 回撤序列（水下曲线，负数小数）：value/历史峰值 − 1 */
export function computeDrawdownSeries(values: number[]): number[] {
  let peak = -Infinity;
  return values.map((v) => {
    if (!Number.isFinite(v)) return 0;
    peak = Math.max(peak, v);
    return peak > 0 ? v / peak - 1 : 0;
  });
}

/**
 * 区间统计（全区间口径；样本 <2 或走势退化 → 对应项 null，不硬算）。
 * 波动/夏普基于逐点收益（ddof=1，年化 √252）；夏普按 rf=0 口径并如实标注。
 */
export function computeRangeStats(
  values: number[],
  benchmarkValues?: number[] | null
): RangeStats {
  const clean = values.filter((v) => Number.isFinite(v));
  const stats: RangeStats = {
    points: clean.length,
    totalReturn: null,
    annualized: null,
    maxDrawdown: null,
    volatility: null,
    sharpe: null,
    benchmarkReturn: null,
    excess: null,
  };
  if (clean.length < 2 || clean[0] <= 0) return stats;

  const total = clean[clean.length - 1] / clean[0] - 1;
  stats.totalReturn = total;
  // 年化跨度按**收益期数**（点数-1）计，避免同口径下的 off-by-one 低估
  stats.annualized = Math.pow(1 + total, TRADING_DAYS / (clean.length - 1)) - 1;

  const rets = returnsFromValues(clean);
  if (rets.length >= 2) {
    const mean = rets.reduce((a, b) => a + b, 0) / rets.length;
    const variance =
      rets.reduce((acc, r) => acc + (r - mean) * (r - mean), 0) / (rets.length - 1);
    const std = Math.sqrt(variance);
    // 数值噪声守卫：恒定收益序列的 std 会落在 1e-16 量级——按 0 处理，
    // 否则夏普会被噪声放大成天文数字（如 1.8e15），属"算不出"而非"很牛"。
    const effectiveStd = std > 1e-12 ? std : 0;
    stats.volatility = effectiveStd * Math.sqrt(TRADING_DAYS);
    stats.sharpe = effectiveStd > 0 ? (mean / effectiveStd) * Math.sqrt(TRADING_DAYS) : null;
  }

  stats.maxDrawdown = Math.min(...computeDrawdownSeries(clean));

  const bench = (benchmarkValues || []).filter((v) => Number.isFinite(v));
  if (bench.length >= 2 && bench[0] > 0) {
    stats.benchmarkReturn = bench[bench.length - 1] / bench[0] - 1;
    stats.excess = total - stats.benchmarkReturn;
  }
  return stats;
}

export function formatPercent(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—';
  return `${(value * 100).toFixed(digits)}%`;
}
