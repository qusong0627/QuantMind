/** 因子研究（factor-lib-demo 复刻版）类型定义 —— 与 /api/v1/factor-research 响应一一对应 */

export interface FactorMeta {
  code: string;
  name_cn: string;
  /** 完整名称（私人因子库的英文全名；经典因子为空） */
  display_name?: string;
  l1: string;
  l2: string;
  /** 1=正（越大越好），-1=负 */
  direction: number;
  description: string;
  formula: string;
  wind_source: string;
  /** 静态标签（demo 原始口径；可用因子的实时标签见排行榜/单因子） */
  env_tag: string;
  time_tag: string;
  available: boolean;
  unavailable_reason: string;
}

export interface BenchRef {
  symbol: string;
  name: string;
}

export interface CatalogResponse {
  factors: FactorMeta[];
  l1_order: string[];
  l2_order: Record<string, string[]>;
  benchmarks: BenchRef[];
  meta: Record<string, unknown>;
}

export interface SeriesPoint {
  date: string;
  value: number;
}

export interface RangeMeta {
  start: string | null;
  end: string | null;
  n_months: number;
}

export interface FactorKpi {
  annual_return?: number | null;
  sharpe?: number | null;
  max_drawdown?: number | null;
  win_rate?: number | null;
  calmar?: number | null;
  n_months?: number;
  ic_mean?: number | null;
  ic_std?: number | null;
  ic_ir?: number | null;
  ic_win_rate?: number | null;
}

export interface ExcessKpi {
  '000300.SH'?: number | null;
  '000906.SH'?: number | null;
  '000905.SH'?: number | null;
}

export interface LeaderboardRow extends FactorKpi {
  rank: number;
  code: string;
  name_cn: string;
  l1: string;
  l2: string;
  composite: number;
  eff_z: number;
  perf_z: number;
  excess_300?: number | null;
  excess_800?: number | null;
  excess_500?: number | null;
  /** 最新截面 Top-N 持仓的中位总市值（亿） */
  median_mv_yi?: number | null;
  /** 市值风格：大盘（≥500亿）/ 中盘（100-500亿）/ 小盘（<100亿） */
  mv_style?: string | null;
  /** 最新截面 Top-N 持仓的前三行业（申万二级） */
  top_industries?: Array<{ name: string; count: number }> | null;
  env_tag: string;
  time_tag: string;
  /** 回测月数不足 6（年化等已置空，排在榜尾） */
  insufficient?: boolean;
  /** 疑似未来函数（|IC|>0.3 / |ICIR|>5，超真实因子上限；排在正常因子之后） */
  suspicious?: boolean;
}

export interface LeaderboardResponse {
  leaderboard: LeaderboardRow[];
  meta: Record<string, unknown> & { range?: RangeMeta };
}

/** 左侧目录选中的分类限定（l2 为 null = 整个大类）；null = 全部因子 */
export interface CategoryFilter {
  l1: string;
  l2: string | null;
}

export interface StockRow {
  rank: number;
  symbol: string;
  name?: string | null;
  industry?: string | null;
  total_mv_yi?: number | null;
  pe_ttm?: number | null;
  pb?: number | null;
  avg_amount_yi?: number | null;
  score?: number | null;
  raw?: number | null;
}

export interface FactorVariant {
  n: number;
  kpi: FactorKpi;
  excess: ExcessKpi;
  nav: SeriesPoint[];
}

export interface BenchSeries {
  code: string;
  name: string;
  kpi?: FactorKpi;
  nav: SeriesPoint[];
}

export interface NScanRow {
  n: number;
  final_nav: number;
  annual_return: number;
}

export interface DistRow {
  name: string;
  count: number;
}

export interface FactorDetail {
  code: string;
  name_cn: string;
  display_name?: string;
  l1: string;
  l2: string;
  direction: number;
  description: string;
  formula: string;
  wind_source: string;
  available: boolean;
  env_tag: string;
  time_tag: string;
  range: RangeMeta;
  variants: FactorVariant[];
  benchmarks: BenchSeries[];
  nscan: NScanRow[];
  ic: SeriesPoint[];
  ic_kpi: FactorKpi;
  stocks: StockRow[];
  /** 个股表截面日；该因子整段无数据时为 null */
  stocks_date: string | null;
  /** true = 面板末月无该因子数据，已回退到它自身最近一个有数据的截面 */
  stocks_stale?: boolean;
  industry_dist: DistRow[];
  cap_dist: DistRow[];
}

export interface CorrPair {
  factor_a: string;
  factor_b: string;
  corr: number | null;
}

export interface CompareItemReq {
  code: string;
  n: number;
}

export interface CompareFactor {
  code: string;
  name_cn: string;
  l1: string;
  l2: string;
  n: number;
  kpi: FactorKpi;
  excess: ExcessKpi;
  nav: SeriesPoint[];
  ic: SeriesPoint[];
}

export interface CompareResponse {
  factors: CompareFactor[];
  corr: CorrPair[] | null;
  benchmarks: BenchSeries[];
  range: RangeMeta;
}

export interface ComposeRequest {
  weights: Record<string, number>;
  top_n: number;
  threshold?: number | null;
  thresholds?: Record<string, number> | null;
  start?: string | null;
  end?: string | null;
  dataset?: 'classic' | 'private';
}

export interface ComposeHolding {
  symbol: string;
  name?: string;
  industry?: string;
  score?: number;
}

export interface ComposeResponse {
  kpi: FactorKpi;
  excess: ExcessKpi;
  nav: SeriesPoint[];
  turnover: SeriesPoint[];
  benchmarks: BenchSeries[];
  holdings: ComposeHolding[];
  holdings_date: string | null;
  weights: Record<string, number>;
  thresholds: Record<string, number> | null;
  top_n: number;
  threshold: number | null;
  range: RangeMeta;
}

export interface OptimalWinner {
  weights: Record<string, number>;
  sharpe: number | null;
  annual_return: number;
  max_drawdown: number;
  win_rate: number;
  excess_300: number | null;
}

export interface OptimalResponse {
  objectives: {
    sharpe: OptimalWinner | null;
    annual_return: OptimalWinner | null;
    excess_300: OptimalWinner | null;
  };
  n_combos: number;
  n_months: number;
  codes: string[];
  elapsed_ms: number;
}

// ---------------------------------------------------------------------------
// 因子筛选（质量门槛 + 同源去重，screen_factors.py 产物）
// ---------------------------------------------------------------------------
export interface ScreeningRow {
  name: string;
  display_name: string;
  library: string;
  l1?: string;
  l2?: string;
  ic_mean: number | null;
  icir: number | null;
  turnover: number | null;
}

export interface ScreeningDropDup {
  name: string;
  library: string;
  duplicate_of: string;
  abs_corr: number;
}

export interface ScreeningGateDrop {
  name: string;
  library: string;
  reason: string;
}

export interface ScreeningResponse {
  generated_at: string;
  gates: { min_abs_ic: number; min_abs_icir: number; corr_threshold: number };
  cross_corr: string;
  counts: {
    candidates: number;
    kept: number;
    gated_out: number;
    deduped: number;
    total_considered: number;
  };
  kept: ScreeningRow[];
  dropped_gated: ScreeningGateDrop[];
  dropped_duplicate: ScreeningDropDup[];
  cluster_summary?: Record<string, unknown>;
}
