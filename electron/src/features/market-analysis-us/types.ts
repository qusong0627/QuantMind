/** 美股市场分析 · 响应类型（与后端 `market_analysis_us` 严格对齐）
 *
 * 后端每个端点的返回形状见 `backend/services/api/market_analysis_us/feed/`。
 * 口径提醒（前端需如实呈现）：
 * - 标的池为标普500 + 纳指补充共约 517 只，**不是全市场**
 * - 日线是未复权原始价，成交额是美元原始值
 * - **无 VIX、无 ETF**；指数 `amount` 恒为 0（故指数卡不显示成交额）
 * - 指数分区通常滞后个股若干交易日 → 各面板独立标注数据日期
 */

// ---- 诊断 ----

export interface UsFeedStatus {
  available: boolean;
  data_dir: string;
  kline_latest: string | null;
  index_latest: string | null;
  valuation_latest: string | null;
  universe_size: number;
  sector_covered: number;
  indices: Array<{ symbol: string; name: string }>;
  notes: Record<string, string>;
}

// ---- Tab1 大盘脉搏 ----

export interface UsIndexItem {
  symbol: string;
  name: string;
  price: number;
  change: number;
  pct_change: number;
  /** 恒为 null：index_daily 的 amount 列恒为 0 */
  turnover_yi: number | null;
  /** 成交量（股数）；SOX.US 为 null */
  volume: number | null;
  /** 量比（当日量 / 前 20 日均量）；无基准时为 null */
  rvol: number | null;
  trend: number[];
  trade_date: string;
}

// ---- 今日热门（大盘脉搏核心） ----

/** 个股热门行：成交额 / 量比 / 涨跌 + 距 52 周高点位置 */
export interface UsHotStockRow {
  symbol: string;
  name: string;
  sector: string;
  close: number;
  pct_change: number;
  /** 成交额（亿美元） */
  amount_yi: number;
  /** 量比 = 当日量 / 前 20 日均量；基准不足 20 日为 null */
  rvol: number | null;
  /** 距 52 周高点百分比：0=正处高点，负值=低于高点 */
  drawdown_pct: number | null;
}

export type UsHotKind = 'amount' | 'rvol' | 'gainers' | 'losers';

export interface UsHotStocks {
  trade_date: string;
  kind: string;
  items: UsHotStockRow[];
}

export interface UsUnusualVolume extends UsHotStocks {
  min_rvol: number;
}

export interface UsDistributionBucket {
  label: string;
  count: number;
}

export interface UsMarketDistribution {
  trade_date: string;
  total: number;
  buckets: UsDistributionBucket[];
  quantiles: {
    p10?: number;
    p25?: number;
    median?: number;
    p75?: number;
    p90?: number;
  };
}

export interface UsMarketStats {
  trade_date: string;
  total_amount_yi: number;
  rvol_median: number | null;
  /** 放量标的占比（量比 ≥1.5 的家数 / 全池，%） */
  active_ratio: number;
  high_rvol_count: number;
  up_5pct: number;
  down_5pct: number;
}

export interface UsSectorFundFlowRow {
  name: string;
  sector: string;
  /** 成交额（亿美元） */
  amount_yi: number;
  /** 占全市场成交额比例（%） */
  share: number;
  /** 前 20 日平均占比（%） */
  base_share: number | null;
  /** 占比变化（百分点）—— 资金迁移方向 */
  share_change_pp: number | null;
}

export interface UsSectorFundFlow {
  trade_date: string;
  total_amount_yi: number;
  base_days: number;
  sectors: UsSectorFundFlowRow[];
}

export interface UsIndexSpreadPair {
  label: string;
  left_symbol: string;
  left_name: string;
  right_symbol: string;
  right_name: string;
  left_return: number;
  right_return: number;
  spread: number;
  window: number;
}

export interface UsIndexSpread {
  trade_date: string;
  pairs: UsIndexSpreadPair[];
  window: number;
}

export interface UsBreadthData {
  trade_date: string;
  total_stocks: number;
  advance_count: number;
  decline_count: number;
  flat_count: number;
  big_up_count: number;
  big_down_count: number;
  total_turnover_yi: number;
  profit_effect: number;
  sentiment_score: number;
  median_pct: number;
  big_move_threshold: number;
}

export interface UsSectorHeatItem {
  name: string;
  sector: string;
  value: number;
  pct_change: number;
  leader: string;
  leader_symbol: string;
  leader_pct: number;
  stock_count: number;
  advance_count: number;
}

export interface UsProfitLeader {
  symbol: string;
  name: string;
  close: number;
  pct_change: number;
  amount_yi: number;
  score: number;
}

export interface UsProfitLeaders {
  trade_date: string;
  items: UsProfitLeader[];
}

// ---- Tab2 市场宽度 ----

export interface UsBreadthPoint {
  date: string;
  advancers: number;
  decliners: number;
  pct_above_ma50: number;
  pct_above_ma200: number;
  new_highs: number;
  new_lows: number;
  ad_line: number;
}

export interface UsBreadthHistory {
  trade_date: string;
  points: UsBreadthPoint[];
  summary: {
    pct_above_ma50: number;
    pct_above_ma200: number;
    new_highs: number;
    new_lows: number;
    ad_line: number;
  };
}

export interface UsBreadthHighlightRow {
  symbol: string;
  name: string;
  close: number;
  high_52w: number;
  low_52w: number;
  drawdown_pct: number;
  pct_change: number;
}

export interface UsBreadthHighlights {
  trade_date: string;
  new_highs: UsBreadthHighlightRow[];
  new_lows: UsBreadthHighlightRow[];
  near_high: UsBreadthHighlightRow[];
  far_from_high: UsBreadthHighlightRow[];
  high_low_counts: {
    new_highs?: number;
    new_lows?: number;
    total?: number;
  };
}

// ---- Tab3 板块轮动 ----

export interface UsSectorRotationRow {
  name: string;
  sector: string;
  ret_1d: number | null;
  ret_5d: number | null;
  ret_20d: number | null;
  ret_60d: number | null;
  rs_20d: number | null;
  breadth_20d: number | null;
  stock_count: number;
}

export interface UsSectorRotation {
  trade_date: string;
  index_date: string;
  benchmark_return_20d?: number | null;
  sectors: UsSectorRotationRow[];
}

export interface UsSectorValuationRow {
  name: string;
  sector: string;
  pe_median: number | null;
  pb_median: number | null;
  dividend_yield_median: number | null;
  market_cap_yi: number;
  stock_count: number;
}

// ---- Tab4 财报季 ----

export interface UsEarningsCalendarItem {
  symbol: string;
  name: string;
  earnings_date: string;
  days_until: number;
  eps_avg: number | null;
  eps_low: number | null;
  eps_high: number | null;
  revenue_avg: number | null;
  revenue_low: number | null;
  revenue_high: number | null;
}

export interface UsEarningsCalendar {
  as_of: string;
  days: number;
  total: number;
  items: UsEarningsCalendarItem[];
}

export interface UsEarningsSurpriseRow {
  symbol: string;
  name: string;
  report_date: string;
  reported_eps: number | null;
  estimate_eps: number | null;
  surprise_pct: number;
}

export interface UsEarningsSurprises {
  as_of: string;
  lookback_days: number;
  items: UsEarningsSurpriseRow[];
}

export interface UsEarningsRevisionRow {
  symbol: string;
  name: string;
  eps_avg: number | null;
  eps_growth_pct: number;
  revenue_growth_pct: number | null;
  analyst_count: number;
}

export interface UsEarningsRevisions {
  items: UsEarningsRevisionRow[];
}

// ---- Tab5 分析师 ----

export interface UsAnalystUpgradeRow {
  symbol: string;
  name: string;
  grade_date: string;
  firm: string;
  action: string;
  from_grade: string;
  to_grade: string;
  price_target_action: string;
  current_price_target: number | null;
  prior_price_target: number | null;
  price_target_change_pct: number | null;
  direction: 'up' | 'down' | 'neutral';
}

export interface UsAnalystUpgrades {
  as_of: string;
  days: number;
  total: number;
  items: UsAnalystUpgradeRow[];
}

export interface UsAnalystTargetRow {
  symbol: string;
  name: string;
  close: number;
  target_mean: number;
  target_high: number | null;
  target_low: number | null;
  upside_pct: number;
}

export interface UsAnalystTargets {
  close_date: string;
  items: UsAnalystTargetRow[];
}

export interface UsAnalystRatingRow {
  symbol: string;
  name: string;
  bull_ratio: number;
  total: number;
  strong_buy: number;
  buy: number;
  hold: number;
  sell: number;
  strong_sell: number;
}

export interface UsAnalystRatings {
  as_of: string;
  total_coverage: number;
  strong_buy: number;
  buy: number;
  hold: number;
  sell: number;
  strong_sell: number;
  bull_ratio: number;
  top_rated: UsAnalystRatingRow[];
  bottom_rated: UsAnalystRatingRow[];
}

// ---- Tab6 资金与筹码 ----

export interface UsInsiderRow {
  symbol: string;
  name: string;
  insider: string;
  position: string;
  value_yi: number;
  shares: number;
  trades: number;
  last_date: string;
}

export interface UsInsiderMovers {
  as_of: string;
  days: number;
  buy_count: number;
  sell_count: number;
  buy_amount_yi: number;
  sell_amount_yi: number;
  top_buys: UsInsiderRow[];
  top_sells: UsInsiderRow[];
}

export interface UsInstitutionalRow {
  symbol: string;
  name: string;
  delta_value_yi: number;
  holders: number;
  increased: number;
  decreased: number;
  /** 明细表内头部机构合计占比（非全机构口径） */
  top_holders_pct: number;
}

export interface UsInstitutionalHolders {
  institutions_pct_median: number | null;
  insiders_pct_median: number | null;
  coverage: number;
  report_date?: string;
  top_increases: UsInstitutionalRow[];
  top_decreases: UsInstitutionalRow[];
}

export interface UsDividendCalendarItem {
  symbol: string;
  name: string;
  ex_dividend_date: string;
  dividend_date: string | null;
  days_until: number;
}

export interface UsDividendCalendar {
  as_of: string;
  days: number;
  total: number;
  items: UsDividendCalendarItem[];
}

export interface UsSplitItem {
  symbol: string;
  name: string;
  split_date: string;
  ratio: number;
}

export interface UsRecentSplits {
  as_of: string;
  days: number;
  items: UsSplitItem[];
}

export interface UsDividendHistoryRow {
  symbol: string;
  name: string;
  payments: number;
  total_per_share: number;
  last_date: string;
}

// ---- Tab7 估值 ----

export interface UsValuationRankRow {
  symbol: string;
  name: string;
  sector: string;
  value: number;
  market_cap_yi: number;
  pe_ratio: number | null;
  pb_ratio: number | null;
  dividend_yield: number | null;
}

export interface UsValuationRankings {
  kind: 'dividend' | 'pe' | 'pb';
  items: UsValuationRankRow[];
}

export interface UsSizeTier {
  key: string;
  label: string;
  count: number;
  market_cap_yi: number;
  pe_median: number | null;
  dividend_yield_median: number | null;
}

export interface UsSizeTiers {
  tiers: UsSizeTier[];
  total_market_cap_yi: number;
  as_of: string;
}

export interface UsValuationOverview {
  coverage: number;
  pe_median: number | null;
  pe_p25: number | null;
  pe_p75: number | null;
  pb_median: number | null;
  dividend_yield_median: number | null;
  dividend_payers: number;
  as_of: string;
}

// ---- 刷新 ----

export interface UsRefreshResult {
  status: string;
  trade_date: string;
  total_stocks: number;
  message: string;
  timestamp: string;
}
