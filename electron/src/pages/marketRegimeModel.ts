/**
 * 大盘分析（模型管理 → 大盘分析）图表口径：均分 × 指数叠加。
 *
 * 指数与均分量纲不同（指数 4000 点、均分 0.0x），直接同轴会画成一条贴地线；
 * 统一按**窗口首个有效日 = 100** 归一，只比走势，不改均分口径。
 */

export interface RegimeIndexPoint {
  trade_date: string;
  /** 指数实际取数日（信号日 T + offset；与 trade_date 不同时用于 tooltip 说明） */
  index_date?: string;
  close: number;
  /** 该指数范围（成分股/交易所）当日的均分；后端按 scope 计算 */
  avg_score?: number | null;
  median_score?: number | null;
  count?: number | null;
}

export interface RegimeIndexSeries {
  code: string;
  name: string;
  /** constituents=成分股 / exchange=沪市·深市前缀 / all_market=无范围回落 */
  scope?: 'constituents' | 'exchange' | 'all_market';
  constituent_count?: number | null;
  points: RegimeIndexPoint[];
}

export interface RegimeScoreRow {
  trade_date: string;
  avg_score: number | null;
  median_score: number | null;
  count: number;
}

/**
 * 均分口径选择：**单选一个有范围的指数** → 用该指数范围的均分；否则全市场均分。
 * 用户口径（2026-10-24）：单选不同市场/指数用对应市场的分数；全部叠加用全市场。
 */
export function resolveScoreRows(
  allMarket: RegimeScoreRow[],
  single: RegimeIndexSeries | null,
  dates: string[],
): { rows: RegimeScoreRow[]; scoped: boolean } {
  const usable =
    single != null &&
    single.scope != null &&
    single.scope !== 'all_market' &&
    single.points.some((p) => p.avg_score != null);
  if (!usable || !single) return { rows: allMarket, scoped: false };
  const byDate = new Map(single.points.map((p) => [String(p.trade_date).slice(0, 10), p]));
  return {
    scoped: true,
    rows: dates.map((d) => {
      const p = byDate.get(d);
      return {
        trade_date: d,
        avg_score: p?.avg_score ?? null,
        median_score: p?.median_score ?? null,
        count: Number(p?.count ?? 0),
      };
    }),
  };
}

export interface RegimeIndexOption {
  code: string;
  name: string;
}

/** A 股常用指数色（与 K 线/图表语义区分：指数线不用红绿，红绿留给涨跌） */
export const INDEX_COLORS: Record<string, string> = {
  '000300.SH': '#f59e0b',
  '000905.SH': '#8b5cf6',
  '000852.SH': '#ec4899',
  '000001.SH': '#10b981',
  '399001.SZ': '#06b6d4',
  // HK
  'HSI.HK': '#f59e0b',
  'HSCEI.HK': '#8b5cf6',
  'HSTECH.HK': '#ec4899',
  'HSCCI.HK': '#06b6d4',
  // US
  'SPX.US': '#f59e0b',
  'NDX.US': '#8b5cf6',
  'DJI.US': '#ec4899',
  'IXIC.US': '#06b6d4',
  // 期货（中证商品期货指数）
  '100001.CCI': '#10b981',
  '000001.CCI': '#06b6d4',
};

export const FALLBACK_INDEX_OPTIONS: RegimeIndexOption[] = [
  { code: '000300.SH', name: '沪深300' },
  { code: '000905.SH', name: '中证500（沪深500）' },
  { code: '000852.SH', name: '中证1000（沪深1000）' },
  { code: '000001.SH', name: '上证综指（沪市）' },
  { code: '399001.SZ', name: '深证成指（深市）' },
];

/** 各市场默认指数候选（后端 index_options 缺失时的兜底；绝不跨市场拿 A 股指数）。 */
export const REGIME_INDEX_OPTIONS_BY_MARKET: Record<string, RegimeIndexOption[]> = {
  CN: FALLBACK_INDEX_OPTIONS,
  HK: [
    { code: 'HSI.HK', name: '恒生指数' },
    { code: 'HSCEI.HK', name: '恒生国企' },
    { code: 'HSTECH.HK', name: '恒生科技' },
    { code: 'HSCCI.HK', name: '恒生红筹' },
  ],
  US: [
    { code: 'SPX.US', name: '标普500' },
    { code: 'NDX.US', name: '纳斯达克100' },
    { code: 'DJI.US', name: '道琼斯工业' },
    { code: 'IXIC.US', name: '纳斯达克综指' },
  ],
  FUTURES: [
    { code: '100001.CCI', name: '中证商品期货指数' },
    { code: '000001.CCI', name: '中证商品期货价格指数' },
  ],
  CRYPTO: [],
};

export function regimeIndexOptions(market?: string): RegimeIndexOption[] {
  const key = String(market || 'CN').toUpperCase();
  return REGIME_INDEX_OPTIONS_BY_MARKET[key] ?? [];
}

/**
 * 指数收盘 → 以窗口首个有效日为 100 的归一序列；缺失日保持 null（connectNulls）。
 */
export function rebaseIndexPoints(
  points: RegimeIndexPoint[],
  dates: string[],
): Array<number | null> {
  const byDate = new Map(points.map((p) => [String(p.trade_date).slice(0, 10), Number(p.close)]));
  let base: number | null = null;
  for (const d of dates) {
    const v = byDate.get(d);
    if (v != null && Number.isFinite(v) && v > 0) {
      base = v;
      break;
    }
  }
  return dates.map((d) => {
    const v = byDate.get(d);
    if (base == null || v == null || !Number.isFinite(v) || v <= 0) return null;
    return Number(((v / base) * 100).toFixed(2));
  });
}
