/** 港股市场分析 · 类型定义（与 backend market_analysis_hk 响应对齐） */

/** 恒生四大指数快照 */
export interface HkIndexItem {
  symbol: string;
  name: string;
  price: number;
  change: number;
  pct_change: number;
  turnover_yi: number;
  trend: number[];
  trade_date: string;
}

/** 市场温度计（港股无涨跌停，±5% 为快涨快跌口径） */
export interface HkBreadthData {
  trade_date: string;
  total_stocks: number;
  advance_count: number;
  decline_count: number;
  flat_count: number;
  big_up_count: number;   // 涨幅 >= +5%
  big_down_count: number; // 跌幅 <= -5%
  total_turnover_yi: number;
  profit_effect: number;  // 上涨占比 %
  sentiment_score: number;
}

/** 恒生行业热力图条目（与 A 股 ShenwanHeatmapChart 数据形状一致） */
export interface HkSectorHeatItem {
  name: string;
  value: number;
  pct_change: number;
  leader: string;
  leader_pct: number;
  stock_count: number;
}

/** 南向资金总览 */
export interface HkSouthOverview {
  trade_date: string;
  covered_stocks: number;
  total_hold_value_yi: number;
  up_days_change: number;
  down_days_change: number;
  south_stock_count: number;
}

/** 南向个股增减持条目 */
export interface HkSouthFlowItem {
  symbol: string;
  name: string;
  pct_change_abs: number; // 持股占比变动（百分点）
  holding_pct: number;    // 当前持股占比 %
  price?: number;
  turnover_yi?: number;
}

export interface HkSouthFlow {
  trade_date: string;
  period: number;
  increase: HkSouthFlowItem[];
  decrease: HkSouthFlowItem[];
}

/** 南向板块配置 */
export interface HkSouthSectorItem {
  name: string;
  hold_value_yi: number;
  pct_avg: number;
  stock_count: number;
}

/** CCASS 全市场集中度 */
export interface HkCcassRankItem {
  symbol: string;
  name: string;
  top10_pct: number;
  south_pct: number;
  cust_pct: number;
  hhi: number;
  top10_d1: number;
}

export interface HkCcassRankings {
  trade_date: string;
  items: HkCcassRankItem[];
}

/** 个股 CCASS 席位明细 */
export interface HkCcassHoldingItem {
  participant_id: string;
  participant_name: string;
  holding_quantity: number;
  holding_pct: number;
}

export interface HkCcassHolding {
  trade_date: string;
  symbol: string;
  name: string;
  items: HkCcassHoldingItem[];
}

/** CCASS 席位异动 */
export interface HkCcassMoverItem {
  symbol: string;
  name: string;
  count: number;
  top10_d1: number;
}

export interface HkCcassMovers {
  trade_date: string;
  new_entrants: HkCcassMoverItem[];
  exits: HkCcassMoverItem[];
}

/** AH 对应股（含溢价） */
export interface HkAhPairItem {
  h_symbol: string;
  h_name: string;
  h_pct_change: number;
  h_close: number;
  a_symbol: string;
  cn_name: string;
  premium_pct?: number | null; // >0 = A 贵（H 折价）；<0 = 倒挂
}

/** 估值主题 */
export interface HkValuationItem {
  symbol: string;
  name: string;
  value: number;
  total_market_cap_yi?: number;
  pe?: number;
  pb?: number;
}

export interface HkValuationRanking {
  kind: string;
  titlename: string;
  published_at: string;
  items: HkValuationItem[];
}

/** 行业轮动 */
export interface HkRotationItem {
  name: string;
  ret_1d: number;
  ret_5d: number;
  ret_20d: number | null;
  turnover_yi: number;
}

export interface HkSectorRotation {
  trade_date: string;
  items: HkRotationItem[];
}

/** 个股综合赚钱效应 */
export interface HkProfitLeaderItem {
  symbol: string;
  name: string;
  pct_change: number;
  turnover_yi: number;
  score: number;
}

export interface HkProfitLeaders {
  trade_date: string;
  items: HkProfitLeaderItem[];
}

/** AH 溢价 */
export interface HkAhPremiumItem {
  h_symbol: string;
  h_name: string;
  a_symbol: string;
  a_close: number;
  h_close: number;
  fx_hkd_cny: number;
  premium_pct: number;
}

export interface HkAhPremium {
  trade_date: string;
  premium: HkAhPremiumItem[]; // A 贵 H 便宜（H 折价）
  discount: HkAhPremiumItem[]; // 倒挂（H 贵 A 便宜）
}

/** 派息日历 */
export interface HkDividendItem {
  symbol: string;
  name: string;
  ex_date: string;
  pay_date: string;
  plan: string;
  dividend: number | null;
}

export interface HkDividendCalendar {
  trade_date: string;
  items: HkDividendItem[];
}

/** 行业估值温度计 */
export interface HkSectorValuationItem {
  name: string;
  pe_median: number | null;
  dividend_yield: number | null;
  stock_count: number;
}

/** 数据状态 */
export interface HkFeedStatus {
  available: boolean;
  data_dir: string;
  latest_kline_date: string | null;
  latest_ccass_date: string | null;
  industry_count: number;
  south_files: number;
}

/** 热力图 L2 下钻：行业成分股（成交额降序，父子同源快照） */
export interface HkDrillItem {
  symbol: string;
  name: string;
  pct_change: number;
  amount_yi: number;
  /** 南向 5 日持股变化（百分点）；非港股通标的为 null（显示 —，不冒充 0） */
  sb_chg_d5_pp: number | null;
}

export interface HkHeatmapDrill {
  name: string;
  trade_date: string;
  items: HkDrillItem[];
  summary: { stock_count: number; total_amount_yi: number; truncated: boolean };
}

// ---- 机会雷达（六柱因子复合评分，研究评分非模型预测） ----

export type HkPillarId = 'mom' | 'trend' | 'south' | 'ccass' | 'liq' | 'risk';

/** 柱分 0–100；null = 该柱无数据（显示 —，绝不显示成 0） */
export type HkPillarScores = Record<HkPillarId, number | null>;

export interface HkOpportunityStock {
  symbol: string;
  name: string;
  sector: string | null;
  opp_score: number;
  pct_change: number;
  amount_yi: number;
  pillars: HkPillarScores;
  pillars_covered: number;
  tags: string[];
  sb_chg_d5_pp: number | null;
  rank_in_sector: number | null;
  sector_size: number | null;
}

export interface HkOpportunityStocks {
  trade_date: string | null;
  items: HkOpportunityStock[];
  universe: { eligible: number; scored: number };
}

export interface HkOpportunitySectorStock {
  symbol: string;
  name: string;
  opp_score: number;
  pct_change: number;
}

export interface HkOpportunitySector {
  name: string;
  opp_score_median: number;
  /** 机会宽度：成分机会分 ≥ 70 占比 ∈ [0,1] */
  opportunity_breadth: number;
  stock_count: number;
  south_chg_d5_median_pp: number | null;
  top_stocks: HkOpportunitySectorStock[];
}

export interface HkOpportunitySectors {
  trade_date: string | null;
  items: HkOpportunitySector[];
}

export interface HkRiskWatchItem {
  symbol: string;
  name: string;
  sector: string | null;
  risk_score: number;
  pct_change: number;
  amount_yi: number;
  reasons: string[];
}

export interface HkRiskWatch {
  trade_date: string | null;
  items: HkRiskWatchItem[];
}

export interface HkOpportunitySummary {
  available: boolean;
  trade_date: string | null;
  pct_base: string;
  thresholds: { min_price_hkd: number; min_amount_hkd: number; min_pillars: number };
  pillar_weights: Record<HkPillarId, number>;
  pillar_coverage: Record<string, number>;
  listed_total: number;
  eligible_total: number;
  factors: { l1: string[]; south: string[]; ccass: string[] };
  excluded_factors: Record<string, string>;
  data_as_of: Record<string, string>;
  disclaimer: string;
}
/** 机构持仓分析 —— 资金属性分类（内资 / 港资 / 外资·欧美 / 外资·亚太） */

export type InstitutionalCategoryId =
  | 'cn_broker'
  | 'southbound'
  | 'hk'
  | 'us_eu'
  | 'apac'
  | 'other';

export type InstitutionalKind = 'settlement' | 'broker' | 'custodian' | 'other';

export interface InstitutionalCategoryStat {
  category: InstitutionalCategoryId;
  label: string;
  kind: InstitutionalKind;
  holding_qty: number;
  value_yi: number; // 估算持仓市值（亿）
  pct_of_disclosed: number; // 占全部披露席位市值 %
  d1_qty: number; // 1 日持仓量变化
  d1_yi: number; // 1 日市值变化（亿）
}

export interface InstitutionalOverview {
  trade_date: string;
  south_date: string;
  stock_count: number;
  disclosed_value_yi: number;
  categories: InstitutionalCategoryStat[];
  change_stats: { window: number; increased: number; decreased: number };
  hkscc_nominees: { value_yi: number; noted: boolean };
}

export interface InstitutionalMoverItem {
  symbol: string;
  name: string;
  price: number | null;
  hold_yi: number;
  hold_pct: number;
  delta_qty: number;
  delta_yi: number;
  delta_pct_abs: number;
  first_seen: boolean; // 基期不在前 50 席位（新进）
}

export interface InstitutionalMovers {
  trade_date: string;
  base_date: string;
  window: number;
  category: string;
  direction: 'increase' | 'decrease';
  items: InstitutionalMoverItem[];
}

export interface InstitutionalDelta {
  window: number;
  delta_qty: number;
  delta_yi: number;
  delta_pct_abs: number;
}

export interface InstitutionalStockCategory {
  category: InstitutionalCategoryId;
  label: string;
  kind: InstitutionalKind;
  holding_qty: number;
  value_yi: number;
  pct_of_total: number;
  deltas: InstitutionalDelta[];
}

export interface InstitutionalParticipant {
  participant_id: string | null;
  participant_name: string;
  category: string;
  kind: string;
  holding_quantity: number;
  holding_pct: number | null;
  delta_5d_qty: number;
  delta_20d_qty: number;
  delta_60d_qty: number;
}

export interface InstitutionalTrendSeries {
  category: string;
  label: string;
  values: number[];
}

export interface InstitutionalStockDetail {
  symbol: string;
  name: string;
  trade_date: string;
  south_pct: number | null;
  price: number | null;
  disclosed_pct: number;
  categories: InstitutionalStockCategory[];
  participants: InstitutionalParticipant[];
  trend: { dates: string[]; series: InstitutionalTrendSeries[] };
}

export interface InstitutionalParticipantItem {
  participant_id: string;
  participant_name: string;
  category: string;
  kind: string;
  hold_yi: number;
  stocks: number;
}

export interface InstitutionalParticipants {
  trade_date: string;
  total: number;
  items: InstitutionalParticipantItem[];
}

export interface InstitutionalSuggestItem {
  symbol: string;
  name: string;
}

/** 宽基指数 · 模型排序分（/indices/model-scores；只描述位置，不是涨跌预测） */
export interface HkIndexModelConstituent {
  symbol: string;
  name: string;
  score: number;
  weight: number | null;
}

export interface HkIndexModelMetrics {
  ic: number | null;
  rank_ic: number | null;
  ic_t: number | null;
  ic_win: number | null;
  top_ann: number | null;
  bottom_ann: number | null;
  long_short: number | null;
  long_short_sharpe: number | null;
}

export interface HkIndexModelInfo {
  model_id: string;
  display_name: string;
  model_type: string;
  status: string;
  feature_count: number | null;
  horizon_days: number | null;
  metrics: HkIndexModelMetrics;
  score_date?: string;
  universe_size?: number;
}

export interface HkIndexModelItem {
  code: string;
  name: string;
  index_symbol: string;
  available: boolean;
  reason: string | null;
  n_members: number;
  n_scored: number;
  coverage: number | null;
  mean_score: number | null;
  median_score: number | null;
  weighted_score: number | null;
  breadth: number | null;
  top_quintile_share: number | null;
  bottom_quintile_share: number | null;
  position: '靠前' | '居中' | '靠后' | null;
  top_constituents: HkIndexModelConstituent[];
  bottom_constituents: HkIndexModelConstituent[];
}

export interface HkIndexModelResponse {
  available: boolean;
  reason: string | null;
  model: HkIndexModelInfo | null;
  membership_date?: string | null;
  universe?: {
    n_scored: number;
    mean_score: number;
    median_score: number;
    top_quintile_cut: number | null;
    bottom_quintile_cut: number | null;
  };
  indices: HkIndexModelItem[];
  wording?: string;
}
