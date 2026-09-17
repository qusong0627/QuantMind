/** 因子报告（Alphalens 式）类型定义 —— 与 backend/services/engine/factor_report 对齐 */

export interface FactorSummary {
  name: string;
  /** 因子库：alpha158 / alpha101 / gtja191 / L1 / L2 */
  library: string;
  /** 中文名（来自平台因子字典，可能为空） */
  display_name?: string | null;
  /** 中文分类（如「动量」「换手与流动性」「信息不对称与毒性」） */
  category_name?: string | null;
  ic_mean: number;
  icir: number;
  t_value: number;
  win_rate: number;
  /** 10 个分位的平均前瞻收益（升序：Q1 = 因子值最小） */
  quantiles: number[];
  /** 多空价差（Q10 − Q1） */
  ls_mean: number;
  /** 分位收益单调性（分位序号与收益的秩相关，±1 表示完美单调） */
  monotonicity: number | null;
  /** 单边换手率（0~1） */
  turnover: number;
  /** 各前瞻期的 IC（T+1/2/5/10/20）→ IC 衰减曲线；旧快照可能没有 */
  ic_by_horizon?: Record<string, number | null>;
  /** 各前瞻期的多空价差 */
  ls_by_horizon?: Record<string, number | null>;
}

/** 报告页可选的数据集及其快照状态 */
export interface FactorDatasetInfo {
  dataset: string;
  label: string;
  available: boolean;
  horizon?: string | null;
  n_factors?: number | null;
  start?: string | null;
  end?: string | null;
  generated_at?: string | null;
}

export interface FactorDatasetList {
  default: string;
  items: FactorDatasetInfo[];
}

export interface FactorReportMeta {
  generated_at: string;
  dataset?: string;
  horizon: string;
  label_mode?: string;
  start: string;
  end: string;
  n_dates: number;
  n_factors: number;
  universe: string;
  step?: number;
  elapsed_sec?: number;
}

export interface FactorSummaryResponse {
  available: boolean;
  reason?: string;
  dataset?: string;
  meta?: FactorReportMeta;
  total?: number;
  factors: FactorSummary[];
}

export interface FactorDetail {
  dataset?: string;
  factor: string;
  horizon: string;
  empty: boolean;
  reason?: string;
  source?: 'series_snapshot' | 'partition_scan';
  dates: string[];
  quantile_mean: number[];
  quantile_curves: number[][];
  ls_curve: number[];
  ic_series: (number | null)[];
  ic_rolling: (number | null)[];
  ic_mean: number | null;
  ic_std: number | null;
  turnover_dates: string[];
  turnover_series: number[];
  turnover_mean: number | null;
  coverage_mean: number | null;
  n_dates: number;
  start: string;
  end: string;
}

export interface FactorCorrelation {
  available: boolean;
  reason?: string;
  factors: string[];
  matrix: number[][];
}

export interface FactorRelated {
  factor: string;
  related: Array<{ name: string; corr: number }>;
}

/** 去重簇成员 */
export interface FactorClusterMember {
  name: string;
  display_name?: string | null;
  library?: string | null;
  ic_mean?: number | null;
  icir?: number | null;
  turnover?: number | null;
  /** 与簇代表的相关性（反向同源为负） */
  corr_to_rep: number;
  is_rep: boolean;
}

/** 同源因子簇 */
export interface FactorCluster {
  size: number;
  representative: string;
  representative_display?: string | null;
  representative_icir?: number | null;
  representative_ic_mean?: number | null;
  members: FactorClusterMember[];
}

export interface FactorClusterSummary {
  n_total: number;
  n_clusters: number;
  n_duplicates: number;
  n_keep: number;
  largest_cluster: number;
}

export interface FactorClusterResponse {
  available: boolean;
  dataset?: string;
  reason?: string;
  threshold?: number;
  keep?: string;
  summary?: FactorClusterSummary;
  clusters?: FactorCluster[];
}

/** 推荐因子组合（训练勾选的数据来源） */
export interface PortfolioFactor {
  name: string;
  display_name?: string | null;
  library?: string | null;
  weight: number;
  direction: number;
  ic_mean?: number | null;
  icir?: number | null;
  turnover?: number | null;
  coverage?: number | null;
  net_daily?: number | null;
}

export interface FactorPortfolioResponse {
  available: boolean;
  dataset?: string;
  reason?: string;
  generated_at?: string;
  rule?: Record<string, number | boolean | string>;
  summary?: {
    n_universe: number;
    n_passed: number;
    n_selected: number;
    n_rejected: number;
    composite_ic: number | null;
    composite_icir: number | null;
    single_icir_avg: number;
  };
  factors?: PortfolioFactor[];
  rejected?: Array<{ name: string; reason: string }>;
}
