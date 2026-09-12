/**
 * 美股个股终端类型 —— 与后端 `backend/services/api/stock_terminal_us/feed/detail.py` 的
 * `/api/v1/stock-terminal-us/detail` 响应严格对齐。
 *
 * 约定：缺数据的字段一律为 null 或空数组（后端不省略键）；前端渲染前必须做 null 防御
 * （历史事故：null.toFixed 导致整页白屏）。
 */

/** 财务表的一行：key 是 QuantUS parquet 原始英文列名，label 是后端映射好的中文 */
export interface UsFinRow {
  key: string;
  label: string;
  /** 与 financials.periods 等长、同序 */
  values: (number | null)[];
}

export interface UsOverview {
  cn_name: string | null;
  en_name: string | null;
  sector: string | null;
  industry: string | null;
  close: number | null;
  pct_change: number | null;
  market_cap: number | null;
  cap_display: string | null;
  week52_high: number | null;
  week52_low: number | null;
  avg_volume: number | null;
  trade_date: string | null;
}

export interface UsValuation {
  pe_ratio: number | null;
  pb_ratio: number | null;
  dividend_yield: number | null;
  market_cap: number | null;
  week52_high: number | null;
  week52_low: number | null;
  source: string | null;
  asof: string | null;
}

export interface UsFinancials {
  periods: string[];
  income: UsFinRow[];
  balance: UsFinRow[];
  cashflow: UsFinRow[];
}

export interface UsAnalystTarget {
  current: number | null;
  high: number | null;
  low: number | null;
  mean: number | null;
  median: number | null;
}

export interface UsRatingRow {
  period: string;
  strongBuy: number | null;
  buy: number | null;
  hold: number | null;
  sell: number | null;
  strongSell: number | null;
}

export interface UsUpgradeRow {
  date: string;
  firm: string | null;
  to_grade: string | null;
  from_grade: string | null;
  /** up | down | init | reiterated | other */
  action: string;
  current_target: number | null;
  prior_target: number | null;
}

export interface UsAnalysts {
  target: UsAnalystTarget | null;
  ratings: UsRatingRow[];
  upgrades: UsUpgradeRow[];
}

export interface UsEarningsHistoryRow {
  quarter: string;
  actual: number | null;
  estimate: number | null;
  surprise_pct: number | null;
}

export interface UsEarningsUpcomingRow {
  date: string;
  eps_estimate: number | null;
  revenue_estimate: number | null;
}

export interface UsEarnings {
  history: UsEarningsHistoryRow[];
  upcoming: UsEarningsUpcomingRow[];
}

export interface UsInsiderRow {
  date: string;
  insider: string | null;
  position: string | null;
  /** buy | sell | other */
  type: string;
  shares: number | null;
  value: number | null;
}

export interface UsInsiderNet {
  buy_value: number | null;
  sell_value: number | null;
  net_value: number | null;
  buy_count: number | null;
  sell_count: number | null;
}

export interface UsHoldingsFundRow {
  holder: string | null;
  pct_held: number | null;
  shares: number | null;
  value: number | null;
  pct_change: number | null;
  date_reported: string | null;
}

export interface UsHoldings {
  insiders_pct: number | null;
  institutions_pct: number | null;
  institutions_float_pct: number | null;
  institutions_count: number | null;
  funds: UsHoldingsFundRow[];
  reported_date: string | null;
}

export interface UsCorporateActions {
  dividends: { date: string; amount: number | null }[];
  splits: { date: string; ratio: number | null }[];
}

export interface UsStockDetail {
  symbol: string;
  name: string;
  trade_date: string | null;
  overview: UsOverview;
  valuation: UsValuation;
  financials: UsFinancials;
  analysts: UsAnalysts;
  earnings: UsEarnings;
  insiders: { items: UsInsiderRow[]; net: UsInsiderNet };
  holdings: UsHoldings;
  corporate_actions: UsCorporateActions;
}

/** `/kline` 响应：日线 + 历史拆股事件（美股日线是未复权原始价，拆股标记用于解释跳变） */
export interface UsKlineResponse {
  symbol: string;
  adjust: string;
  items: { date: string; open: number; high: number; low: number; close: number; volume: number | null; amount: number | null }[];
  splits: { date: string; ratio: number | null }[];
}
