/** 测试集实盘力（滚动 3 个月）API 载荷类型。 */

export interface SignalEvalICStats {
  n: number;
  mean: number | null;
  std: number | null;
  icir: number | null;
  win_rate: number | null;
  t: number | null;
  hac_t: number | null;
}

export interface SignalEvalDecayRow {
  horizon: number;
  n_days: number;
  mean_ic: number | null;
  t: number | null;
}

export interface SignalEvalQuantiles {
  buckets: Array<number | null>;
  monotonicity: number | null;
  spread_mean: number | null;
  spread_t: number | null;
  spread_pos_rate: number | null;
}

export interface SignalEvalStability {
  rank_autocorr_daily: number | null;
  rank_autocorr_n: number;
  top_overlap: Record<string, number | null>;
}

export interface SignalEvalTopN {
  n: number;
  total_return: number | null;
  annualized: number | null;
  mdd: number | null;
  sharpe: number | null;
  excess: number | null;
  excess_t: number | null;
  market_return: number | null;
  excess_hs300?: number | null;
  excess_sse?: number | null;
  excess_szse?: number | null;
  excess_zz1000?: number | null;
  excess_zz500?: number | null;
  hs300_return?: number | null;
  sse_return?: number | null;
  szse_return?: number | null;
  zz1000_return?: number | null;
  zz500_return?: number | null;
  avg_names: number | null;
  capacity_p5: number | null;
  capacity_p10: number | null;
  cohorts: number | null;
}

export interface SignalEvalCoverage {
  avg: number | null;
  min: number | null;
  days: number;
  days_below_50: number;
  low_days: string[];
}

export type SignalEvalGrade = 'green' | 'yellow' | 'red' | 'unknown';

export interface SignalEvalAdmission {
  grade: SignalEvalGrade;
  checks: Record<string, boolean>;
  reasons: string[];
  window_end: string | null;
  checked_at: string | null;
  stale: boolean;
  eval_version: number;
}

export interface SignalEvalHistoryRow {
  computed_at: string;
  window_start: string;
  window_end: string;
  signal_days: number;
  rank_ic: number | null;
  neutral_ic: number | null;
  icir: number | null;
  spread: number | null;
  monotonicity: number | null;
  top20_return: number | null;
  top20_excess: number | null;
  top50_excess: number | null;
  stability: number | null;
  grade: SignalEvalGrade | null;
  long_pass: boolean | null;
}

export interface SignalEvalVerdict {
  ranking_pass: boolean;
  neutral_pass: boolean;
  decile_pass: boolean;
  long_pass: boolean;
  stability_pass: boolean;
  summary: string;
}

export interface SignalEvalReport {
  status: 'ready';
  model_id: string;
  eval_version: number;
  computed_at: string;
  window: {
    label: string;
    start: string;
    end: string;
    signal_days: number;
    pred_end: string;
    pred_lag_days?: number;
    horizon: number;
    independent_periods?: number;
  };
  sample: { rows: number; symbols: number; avg_names: number };
  coverage?: SignalEvalCoverage;
  metrics: {
    rank_ic: SignalEvalICStats;
    neutral_ic: SignalEvalICStats;
    decay: SignalEvalDecayRow[];
    quantiles: SignalEvalQuantiles;
    stability: SignalEvalStability;
  };
  series: {
    weekly_ic: Array<{ week: string; ic: number | null; ic_neutral: number | null }>;
    benchmarks?: Record<string, Array<{ date: string; nav: number }>>;
  };
  topn: SignalEvalTopN[];
  execution?: {
    entry: string;
    exit: string;
    cost_bps_per_side: number;
    cost_note: string;
    limit_up_entry_excluded: boolean;
    limit_down_exit_extended: boolean;
    st_pit_excluded: boolean;
    adv_floor_yuan: number;
    missing_price_skipped: boolean;
    benchmark_basis: string;
  };
  verdict: SignalEvalVerdict;
  admission?: SignalEvalAdmission;
}

export type SignalEvalState =
  | {
      status: 'ready';
      report: SignalEvalReport;
      admission: SignalEvalAdmission;
      history: SignalEvalHistoryRow[];
      stale: false;
    }
  | {
      status: 'stale';
      report: SignalEvalReport | null;
      admission: SignalEvalAdmission;
      history: SignalEvalHistoryRow[];
      stale: true;
      reason?: string;
    }
  | {
      status: 'missing';
      admission: SignalEvalAdmission;
      history: SignalEvalHistoryRow[];
      stale: true;
      reason?: string;
    }
  | {
      status: 'running';
      progress: number;
      step: string;
      message: string;
      cached_report?: SignalEvalReport | null;
      admission: SignalEvalAdmission;
      history: SignalEvalHistoryRow[];
      stale?: boolean;
    }
  | { status: 'unavailable'; reason: string };
