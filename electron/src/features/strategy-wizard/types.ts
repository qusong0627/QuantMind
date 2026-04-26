export type Operator = '>' | '>=' | '<' | '<=' | '==' | '!=';

export interface NumericCondition {
  type: 'numeric';
  factor: string;
  operator: Operator;
  threshold: number;
}

export interface TrendCondition {
  type: 'trend';
  factor: string;
  window: number;
  direction: 'up' | 'down';
}

export interface CompositeCondition {
  type: 'composite';
  op: 'AND' | 'OR';
  children: Condition[];
}

export type Condition = NumericCondition | TrendCondition | CompositeCondition;

export interface BuyRule {
  kind: 'indicator' | 'fundamental';
  name: string;
  params?: Record<string, any>;
  priority?: number;
  weight?: number;
}

export interface SellRule {
  kind: 'stop' | 'indicator' | 'time';
  name: string;
  params?: Record<string, any>;
}

export interface RiskConfig {
  maxDrawdown?: number;
  maxPositionSize?: number;
  maxPositions?: number;
  stopLoss?: number;
  takeProfit?: number;
  riskFreeRate?: number;
  rebalanceFrequency?: 'daily' | 'weekly' | 'monthly';
  effectiveFrom?: string;
  effectiveTo?: string;
  // 交易费率配置
  commission?: number; // 佣金费率 (如 0.0003)
  stampDuty?: number;  // 印花税 (如 0.001)
  transferFee?: number;// 过户费 (如 0.00002)
  slippage?: number;   // 滑点 (如 0.002)
}

// Qlib 策略参数（取代旧的 positionConfig + strategyStyle + risk）
export interface QlibParams {
  strategy_type: 'TopkDropout' | 'TopkWeight';
  topk: number;
  n_drop?: number;
  rebalance_days: 1 | 3 | 5;
  // 兼容历史缓存与旧接口字段
  rebalance_period?: 'daily' | 'weekly' | 'monthly';
}

// 仓位管理配置（保留兼容，不再在向导中使用）
export interface PositionConfig {
  // 动态仓位开关
  enableDynamicPosition: boolean;

  // 市场低迷时仓位 (0.3 - 0.5)
  bearMarketPosition: number;

  // 市场震荡时仓位 (0.5 - 0.7)
  normalMarketPosition: number;

  // 市场活跃时仓位 (0.7 - 1.0)
  bullMarketPosition: number;

  // 策略总仓位占比 (0.1 - 1.0)
  strategyTotalPosition: number;

  // 检测指标配置
  marketIndexSymbol: string;    // 默认 "000300.SH"
  detectionWindow: number;      // 默认 20
  volumeThreshold: number;      // 默认 0.2 (20%)
}

// 策略风格
export type StyleType = 'conservative' | 'aggressive' | 'dynamic';

export interface StrategyStyle {
  type: StyleType;
  customParams?: Partial<RiskConfig>;
}

// Qlib验证结果
export interface ValidationCheck {
  type: 'syntax' | 'import' | 'config' | 'strategy' | 'sandbox' | 'error';
  passed: boolean;
  message: string;
  details?: string;
}


export interface ValidationResult {
  valid: boolean;
  checks: ValidationCheck[];
  warnings: string[];
  executionPreview?: {
    start_date: string;
    end_date: string;
    universe_size: number;
  };
}

// 保存状态
export interface SaveStatus {
  savedToCloud: boolean;
  cloudUrl?: string;
  strategyId?: string;
  downloadedLocally: boolean;
}

export interface PoolFile {
  fileUrl: string;
  fileKey?: string;
  format: 'json' | 'txt' | 'csv';
  relativePath?: string;
  fileSize?: number;
  codeHash?: string;
}

export interface WizardState {
  conditions: Condition | null;
  pool: {
    items: Array<{ symbol: string; name?: string; metrics?: Record<string, number> }>;
    summary?: Record<string, any>;
    charts?: Record<string, any>;
  } | null;
  buyRules: BuyRule[];
  sellRules: SellRule[];
  risk: RiskConfig;
  generated?: {
    code?: string;
    doc?: string;
    hints?: Record<string, any>;
  };
  customPool?: Array<{ symbol: string; name: string; price?: number }>;
  selectedSymbols?: string[];

  // Qlib 策略参数（合并了原仓位管理+风格选择）
  qlibParams: QlibParams;
  // 已废弃字段（保留兼容）
  positionConfig?: PositionConfig;
  strategyStyle?: StrategyStyle;
  validationResult: ValidationResult | null;
  saveStatus: SaveStatus;
  poolFile?: PoolFile;
}
