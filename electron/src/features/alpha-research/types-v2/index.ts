// Task status
export type TaskStatus = 'idle' | 'running' | 'completed' | 'failed';

// Execution phase
export type ExecutionPhase =
  | 'parsing'      // Parsing requirements
  | 'planning'     // Planning direction
  | 'evolving'     // Evolving
  | 'backtesting'  // Backtesting
  | 'analyzing'    // Analyzing results
  | 'completed';   // Completed

// Factor quality level（unknown = IC 缺失，质量无从分级；旧实现把 null 判成 low）
export type FactorQuality = 'high' | 'medium' | 'low' | 'unknown';

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

export type UniverseId = BuiltinUniverseId | (string & {});

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
  datasets?: Record<
    string,
    { columns: number; categories?: string[]; categoryCount?: number }
  >;
  error?: string;
}

// Task configuration
export interface TaskConfig {
  // Basic configuration
  userInput: string;
  /** When true, use options in "Settings -> Mining Direction" (selected/random), ignoring input box content */
  useCustomMiningDirection?: boolean;
  numDirections?: number;
  maxRounds?: number;
  librarySuffix?: string;

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

  // Backtest configuration
  market?: 'csi300' | 'csi500' | 'sp500';
  startDate?: string;
  endDate?: string;

  // Advanced configuration
  parallelExecution?: boolean;
  qualityGateEnabled?: boolean;
  backtestTimeout?: number;
}

/**
 * 「挖掘历史 → 重跑」的回填草稿（AppRoot 交给 HomePage → ChatInput）。
 * `key` 是代次不是内容：同方向连点两次「重跑」也要重新应用，
 * 只比对象内容会被 React 判成无变化而跳过。
 */
export interface MiningRetryDraft {
  key: number;
  userInput: string;
  miningMarket?: TaskConfig['miningMarket'];
  universe?: UniverseId;
  dataSource?: TaskConfig['dataSource'];
}

// Real-time metrics
export interface RealtimeMetrics {
  // IC metrics —— 一律可选：后端没算过 → undefined → 界面显「—」，禁止补 0
  // （0 的语义是「算出来就是 0」，与「没算过」是两回事）
  ic?: number;
  icir?: number;
  rankIc?: number;
  rankIcir?: number;

  // Optional factor name if available (e.g. best factor)
  factorName?: string;

  // 本轮挖掘产出的全部结构化因子（挖到多少显示多少；旧实现截 Top10）
  factors?: Factor[];

  // Return metrics（同上：可选）
  annualReturn?: number;
  sharpeRatio?: number;
  maxDrawdown?: number;

  // Factor statistics（计数必有——统计面板直接渲染）
  totalFactors: number;
  highQualityFactors: number;
  mediumQualityFactors: number;
  lowQualityFactors: number;

  // —— 机构级指标（mining_plugins 评估器链，metadata_json 透传）——
  // 一律可选：后端没算过/旧回测 → undefined → 界面显「—」，禁止补 0。
  /** RRE 排序可靠度（robustness，越大越稳） */
  rre?: number;
  /** PFS 扰动保真度（截面加噪排序保持率） */
  pfs?: number;
  pfsGauss?: number;
  pfsT?: number;
  /** 有效天数（参与 IC 计算的交易日数） */
  nObs?: number;
  /** 多头组合（rank 前 30%）日均换手 */
  turnoverDaily?: number;
  /** 年化换手 = 日均 × 252 */
  annTurnover?: number;
  /** 扣费年化收益（研究口径双边 0.2%） */
  annReturnNet?: number;
  /** 扣费夏普 */
  sharpeNet?: number;
  /** 扣费最大回撤 */
  maxDrawdownNet?: number;
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

// 物化状态（后端 metadata.materialization / manifest 条目，snake→camel 归一后）
export type FactorMaterializationStatus =
  | 'materialized'
  | 'rejected_duplicate'
  | 'rejected_gate'
  | 'error'
  | 'none';

export interface FactorMaterialization {
  status: FactorMaterializationStatus;
  /** 训练库列名（column） */
  column?: string;
  name?: string;
  values?: number;
  /** 与库内最高相关因子的 |ρ|（rejected_duplicate / 物化时的查重值） */
  corr?: number | null;
  /** 最高相关对照列 */
  corrAgainst?: string | null;
  at?: string;
  /** 门禁裁决明细（rejected_gate / materialized 都带） */
  gates?: any;
  error?: string;
}

// Factor information
export interface Factor {
  factorId: string;
  factorName: string;
  factorExpression: string;
  factorDescription: string;
  quality: FactorQuality;
  market?: string;  // a_share, crypto, hong_kong, us_stock
  universe?: string;  // csi300, csi500, csi1000, sse50, gem, star, csi800, all_a

  // Backtest metrics —— 一律可选：后端没算过保持 undefined（界面显「—」），
  // 旧实现硬编码 0，把「没算过」伪造成「算出来是 0」
  ic?: number;
  icir?: number;
  rankIc?: number;
  rankIcir?: number;
  sharpeRatio?: number;
  annualReturn?: number;
  maxDrawdown?: number;

  // —— 物化（训练库入库状态）——
  /** 未物化的因子没有这段（undefined） */
  materialization?: FactorMaterialization;
  /** 历史因子（user_id IS NULL）：只读，物化/回测入口禁用 */
  ownerless?: boolean;

  // —— 机构级指标（metadata_json；缺失保持 undefined，界面显「—」）——
  rre?: number;
  /** PFS 族（后端 metadata.quality 子对象） */
  pfsQuality?: {
    pfs?: number;
    pfsGauss?: number;
    pfsT?: number;
    nDays?: number;
  };
  turnoverDaily?: number;
  annTurnover?: number;
  annReturnNet?: number;
  sharpeNet?: number;
  maxDrawdownNet?: number;
  nObs?: number;

  // Metadata
  round: number;
  direction: string;
  createdAt: string;

  /** 因子工厂批量产出：只读展示，不提供回测/训练操作 */
  readOnly?: boolean;
  source?: string;
  coverage?: number;
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
