/**
 * 截面选股 · 单模型 TopK 历史统计（测试集）契约。
 *
 * 口径（与后端 `model_selection_stats_service.py` / `selection_stats.py` 一致）：
 * - 只读 `pred.parquet` 的 `split='test'` 分片，按日取分数最高的 top_k 只；
 * - 信号日 T → 之后第 `execution_lag_days` 个交易日收盘入选 → 第 H 个交易日收盘退出；
 * - 每期把全部权益等权投入，期收益 = 有效标的等权平均，权益逐期复利；
 * - 收益字段一律是**百分数**（12.3 = +12.3%），金额字段是元；
 * - 缺失一律 `null`，展示层渲染成 `—`，绝不当作 0。
 */

export interface SelectionStatsRequested {
  start: string | null;
  end: string | null;
  top_k: number;
  /** 后端最终采用的持有交易日数（未传时为模型 target_horizon_days） */
  hold_days: number;
  capital: number;
}

export interface SelectionStatsEffective {
  start: string;
  end: string;
  first_signal_day: string;
  last_signal_day: string;
  first_entry_day: string;
  last_exit_day: string;
  trading_days: number;
  signal_days: number;
}

export interface SelectionStatsParams {
  /** 模型训练周期（metadata.target_horizon_days） */
  horizon_days: number;
  execution_lag_days: number;
  entry_convention: string;
  exit_convention: string;
  price_adjust: string;
  fee_convention: string;
  tradability: string;
  benchmark: string;
  /** daily = 每日滚动 T+1；period = 按模型周期换股 */
  cohort_mode: 'daily' | 'period';
}

export type SelectionStatsDataQuality = 'complete' | 'partial' | 'unavailable';

export interface SelectionStatsSummary {
  total_return_pct: number | null;
  max_drawdown_pct: number | null;
  benchmark_return_pct: number | null;
  excess_return_pct: number | null;
  cohorts: number;
  closed_cohorts: number;
  open_cohorts: number;
  win_cohorts: number;
  loss_cohorts: number;
  flat_cohorts: number;
  cohort_win_rate_pct: number | null;
  avg_cohort_return_pct: number | null;
  median_cohort_return_pct: number | null;
  picks: number;
  priced_picks: number;
  pick_win_rate_pct: number | null;
  avg_pick_return_pct: number | null;
  price_coverage_pct: number | null;
  data_quality: SelectionStatsDataQuality;
  initial_capital: number;
  final_equity: number | null;
  profit_amount: number | null;
}

export interface SelectionStatsMonth {
  month: string;
  start_date: string;
  end_date: string;
  start_equity: number | null;
  end_equity: number | null;
  return_pct: number | null;
  profit_amount: number | null;
  cohorts: number;
  win_rate_pct: number | null;
  benchmark_return_pct: number | null;
  excess_return_pct: number | null;
  /** 首尾月通常不完整（区间不是整月） */
  partial: boolean;
}

export interface SelectionStatsCohort {
  index: number;
  signal_date: string;
  entry_date: string;
  /** 未到期期为 null，用 `mark_date` 估值 */
  exit_date: string | null;
  mark_date: string;
  exit_kind: 'closed' | 'open';
  held: number;
  priced: number;
  missing_entry: number;
  missing_exit: number;
  stale_exit: number;
  return_pct: number | null;
  benchmark_return_pct: number | null;
  excess_return_pct: number | null;
  equity: number | null;
  partial: boolean;
  skipped: boolean;
}

export interface SelectionStatsPoint {
  date: string;
  equity: number;
}

export interface SelectionStatsQuality {
  missing_entry_price: number;
  missing_exit_price: number;
  stale_exit: number;
  underfill_signals: number;
  partial_cohorts: number;
  skipped_cohorts: number;
  open_cohorts: number;
  signals: number;
  requested_picks: number;
  priced_picks: number;
}

export interface SelectionStatsResponse {
  model_id: string;
  model_name: string;
  market: string;
  split: 'test';
  requested: SelectionStatsRequested;
  effective: SelectionStatsEffective;
  params: SelectionStatsParams;
  summary: SelectionStatsSummary;
  months: SelectionStatsMonth[];
  cohorts: SelectionStatsCohort[];
  points: SelectionStatsPoint[];
  warnings: string[];
  quality: SelectionStatsQuality;
  /** 推理中心「排除黑名单」口径（未启用时不带或 applied=false） */
  blacklist?: { applied: boolean; excluded?: number; asof?: string | null };
}

/** 查询参数：全部可省，后端按模型元数据与测试集全区间补齐 */
export interface SelectionStatsQuery {
  start?: string;
  end?: string;
  top_k?: number;
  hold_days?: number;
  capital?: number;
  exclude_blacklist?: boolean;
}
