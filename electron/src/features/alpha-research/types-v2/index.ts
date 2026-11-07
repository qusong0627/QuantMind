// Task status
export type TaskStatus = 'queued' | 'idle' | 'running' | 'stopping' | 'completed' | 'failed' | 'cancelled';

// Execution phase
export type ExecutionPhase =
  | 'parsing'      // Parsing requirements
  | 'planning'     // Planning direction
  | 'evolving'     // Evolving
  | 'backtesting'  // Backtesting
  | 'analyzing'    // Analyzing results
  | 'completed';   // Completed

// Factor quality level
export type FactorQuality = 'high' | 'medium' | 'low';

// 内置指数池 + 全局自定义股票池 code
export type BuiltinUniverseId =
  | 'csi300'
  | 'csi500'
  | 'csi1000'
  | 'sse50'
  | 'gem'
  | 'star'
  | 'csi800'
  | 'all_a';

export type UniverseId = BuiltinUniverseId | (string & Record<never, never>);

// Stock universe metadata from /universes API
export interface UniverseInfo {
  id: UniverseId;
  name: string;
  indexSymbol: string | null;
  stockCount: number;
  isSystem?: boolean;
}

// L1 factor category from /factor-categories API
export interface FactorCategory {
  id: string;
  name: string;
  featureCount: number;
  sampleFeatures: string[];
}

// QuantDB data availability summary from /data-summary API
export interface DataSummary {
  available: boolean;
  dateRange?: {
    start: string;
    end: string;
    tradingDays: number;
  };
  universes?: Record<string, { count: number; indexSymbol: string | null }>;
  stockCount?: number;
  /** 真实可代理挖掘的数据集数量（注册表逐个探测 parquet 可读性） */
  datasetCount?: number;
  datasets?: Record<
    string,
    { columns: number; categories?: string[]; categoryCount?: number }
  >;
  error?: string;
}

/** Statistical runner parameters, using the server's parameter names. */
export interface StatisticalMiningOptions {
  horizons?: number[];
  top_k?: number;
  min_names_per_day?: number;
  min_valid_days?: number;
  quantiles?: number;
  fdr_q?: number;
  require_fdr?: boolean;
  oos_min_ratio?: number;
  oos_min_days?: number;
  neutralize?: 'none' | 'industry' | 'industry_size';
  stability_folds?: number;
  min_fold_sign_consistency?: number;
  min_fold_days?: number;
  require_stability?: boolean;
  dedup_corr_reject?: number;
  dedup_corr_warn?: number;
  require_dedup?: boolean;
  composite_enabled?: boolean;
  composite_z_window?: number;
  net_ic_cost_rate?: number | null;
  tradability?: {
    exclude_suspended?: boolean;
    st_snapshot_symbols?: string[];
    min_list_days?: number;
    exclude_limit_locked?: boolean;
    limit_tolerance?: number;
    min_amount_quantile?: number;
    min_amount_window?: number;
  };
}

// Task configuration
export interface TaskConfig {
  // Basic configuration
  userInput: string;
  algorithm?: 'rdagent' | 'statistical';
  statisticalConfig?: StatisticalMiningOptions;
  autoMaterialize?: boolean;
  miningStart?: string;
  miningEnd?: string;
  validStart?: string;
  validEnd?: string;
  /** When true, use options in "Settings -> Mining Direction" (selected/random), ignoring input box content */
  useCustomMiningDirection?: boolean;
  maxRounds?: number;

  // LLM configuration
  apiKey?: string;
  apiUrl?: string;
  modelName?: string;

  // Mining market (multi-market support)
  miningMarket?: 'a_share' | 'crypto' | 'hong_kong' | 'us_stock' | 'futures';

  // Stock universe for mining and backtesting
  universe?: UniverseId;

  // Data source selection
  dataSource?: 'qlib_bin' | 'parquet';
  /** Selected server-registered QuantDB datasets for RD-Agent mining. */
  datasets?: string[];
  /** Mining frequency. The current supported value is A-share daily. */
  freq?: 'day';

  // Backtest configuration
  market?: 'csi300' | 'csi500' | 'sp500';
  startDate?: string;
  endDate?: string;

}

// Real-time metrics
export interface RealtimeMetrics {
  // IC metrics
  ic: number | null;
  icir: number | null;
  rankIc: number | null;
  rankIcir: number | null;
  
  // Optional factor name if available (e.g. best factor)
  factorName?: string;
  
  // Top 10 factors list
  top10Factors?: Array<{
    factorId: string;
    factorName: string;
    factorExpression: string;
    rankIc: number | null;
    rankIcir: number | null;
    ic: number | null;
    icir: number | null;
    annualReturn?: number | null;
    sharpeRatio?: number | null;
    maxDrawdown?: number | null;
    calmarRatio?: number | null;
    market?: string;
    cumulativeCurve?: Array<{date: string, value: number}>;
  }>;

  // Return metrics
  annualReturn: number | null;
  sharpeRatio: number | null;
  maxDrawdown: number | null;

  // Factor statistics
  totalFactors: number;
  highQualityFactors: number;
  mediumQualityFactors: number;
  lowQualityFactors: number;
}

// Execution progress
export interface ExecutionProgress {
  phase: ExecutionPhase;
  currentRound: number;
  totalRounds: number;
  progress: number; // 0-100
  message: string;
  timestamp: string;
}

// Timeline phase (from backend)
export interface TimelinePhase {
  key: string;
  label: string;
  status: 'pending' | 'running' | 'completed';
  start_time: string | null;
  end_time: string | null;
  duration_s: number | null;
  tokens?: { prompt: number; completion: number; calls: number };
  factors?: string[];
}

// Timeline loop entry
export interface TimelineLoop {
  loop: number;
  label: string;
  status: 'running' | 'backtesting' | 'completed';
  phases: TimelinePhase[];
}

// Token usage summary
export interface TokenUsage {
  total_prompt_tokens: number;
  total_completion_tokens: number;
  total_calls: number;
  models: string[];
}

// Log entry
export interface LogEntry {
  id: string;
  timestamp: string;
  level: 'info' | 'warning' | 'error' | 'success';
  message: string;
}

// Factor information
export interface FactorTags {
  data_source: string;
  frequency: string;
  family: string;
  transform: string;
  target: string;
  maturity: string;
}

export interface FactorLifecycle {
  state: 'experiment' | 'observing' | 'active' | 'downweighted';
  maturity: string;
  blind_until: string | null;
  last_eval: Record<string, unknown> | null;
}

export interface Factor {
  factorId: string;
  factorName: string;
  factorExpression: string;
  factorDescription: string;
  quality: FactorQuality;
  market?: string;  // a_share, crypto, hong_kong, us_stock
  universe?: string;  // csi300, csi500, csi1000, sse50, gem, star, csi800, all_a

  // Backtest metrics
  ic: number | null;
  icir: number | null;
  rankIc: number | null;
  rankIcir: number | null;
  sharpeRatio: number | null;
  annualReturn: number | null;
  maxDrawdown: number | null;

  // Metadata
  round: number;
  direction: string;
  createdAt: string;
  archivedAt?: string | null;
  hasCode?: boolean;
  archiveReason?: string | null;

  /** 因子工厂批量产出：只读展示，不提供回测/训练操作 */
  readOnly?: boolean;
  source?: string;
  coverage?: number | null;
  tags?: FactorTags;
  lifecycle?: FactorLifecycle | null;
}

// Backtest result
export interface BacktestResult {
  // Overall metrics
  metrics: RealtimeMetrics;

  // Time series data
  equityCurve: TimeSeriesData[];
  drawdownCurve: TimeSeriesData[];
  icTimeSeries: TimeSeriesData[];

  // Factor list
  factors: Factor[];

  // Quality distribution
  qualityDistribution: {
    high: number;
    medium: number;
    low: number;
  };
}

// Time series data point
export interface TimeSeriesData {
  date: string;
  value: number;
}

// Task information
export interface Task {
  taskId: string;
  queuePosition?: number;
  status: TaskStatus;
  config: TaskConfig;
  progress: ExecutionProgress;
  metrics?: RealtimeMetrics;
  result?: BacktestResult;
  logs: LogEntry[];
  createdAt: string;
  updatedAt: string;
  timeline?: TimelineLoop[];
  tokenUsage?: TokenUsage;
  /** 后端随任务状态返回的已落库因子（结构化，优先于日志解析） */
  factors?: Factor[];
}

// API Response
export interface ApiResponse<T = any> {
  success: boolean;
  data?: T;
  error?: string;
  message?: string;
}

// WebSocket message type
export type WsMessageType =
  | 'progress'
  | 'metrics'
  | 'log'
  | 'result'
  | 'error';

// WebSocket message
export interface WsMessage {
  type: WsMessageType;
  taskId: string;
  data: any;
  timestamp: string;
}
