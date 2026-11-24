/** 机构择时 v2（影子盘）快照契约：后端 /api/v1/market-analysis/timing-v2。 */

export interface TimingV2Metrics {
  n: number;
  ann: number | null;
  vol: number | null;
  sharpe: number | null;
  mdd: number | null;
  hit: number | null;
  turnover: number | null;
  exposure: number | null;
  t: number | null;
  total: number | null;
}

export interface TimingV2HistoryPoint {
  date: string;
  tft_z: number | null;
  mlp_z: number | null;
  exposure: number | null;
  tft_pred_mean: number | null;
  mlp_pred: number | null;
}

export interface TimingV2Snapshot {
  status: 'ok' | 'stale' | 'missing' | string;
  model_version: string;
  generated_at: string;
  data_as_of: string;
  tft_as_of: string;
  mlp_as_of: string;
  as_of: string;
  stale_trading_days: number;
  market: string;
  tft_model: {
    model_id: string;
    role: string;
    pred_mean: number | null;
    pred_bottom20: number | null;
    z: number | null;
  };
  mlp_model: {
    model: string;
    role: string;
    pred: number | null;
    z: number | null;
  };
  signal: {
    tft_z: number | null;
    mlp_z: number | null;
    exposure: number;
    label: string;
    rule: string;
    execution: string;
    confidence: string;
  };
  structure: {
    breadth: number | null;
    gap_ie_20: number | null;
    gap_hz_20: number | null;
    vol20: number | null;
    vol20_ann: number | null;
    bp_capw_pct: number | null;
    ep_capw_pct: number | null;
    med_pe_pct: number | null;
    amount_ratio20: number | null;
  };
  performance: {
    window: { start: string; end: string; n: number };
    combined: TimingV2Metrics;
    tft_only: TimingV2Metrics;
    mlp_only: TimingV2Metrics;
    buy_hold: TimingV2Metrics;
    placebo_p: number | null;
    mlp_long_history: {
      window: { start: string; end: string | null };
      strategy: TimingV2Metrics;
      buy_hold: TimingV2Metrics;
    };
  };
  history: TimingV2HistoryPoint[];
  roles: Array<{ model: string; role: string; use: string; status: string }>;
  notes: string[];
}
