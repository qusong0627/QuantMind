/**
 * Qlib专用回测类型定义
 */

/** Qlib策略类型 */
export type QlibStrategyType =
  | 'TopkDropout'
  | 'WeightStrategy'
  | 'CustomStrategy'
  | 'standard_topk'
  | 'alpha_cross_section'
  | 'long_short_topk'
  | 'deep_time_series'
  | 'adaptive_drift'
  | 'score_weighted'
  | 'momentum'
  | 'EnhancedIndex'
  | 'RiskParity'
  | 'StopLoss'
  | 'VolatilityWeighted';

/** Qlib策略参数 */
export interface QlibStrategyParams {
  [key: string]: number | string | boolean | undefined;

  // 基础参数
  topk?: number;
  short_topk?: number;
  n_drop?: number;
  signal?: string;

  // 费率参数
  buy_cost?: number;
  sell_cost?: number;

  // WeightStrategy参数
  min_score?: number;
  max_weight?: number;
  long_exposure?: number;
  short_exposure?: number;
  enable_short_selling?: boolean;

  // 动量策略参数
  momentum_period?: number;

  // 风险模型/指数增强参数
  riskmodel_root?: string;
  market?: string;

  // 行业轮动参数
  topk_sectors?: number;
  lookback_days?: number;

  // 波动率加权参数
  vol_lookback?: number;

  // 止损参数
  stop_loss?: number;
  take_profit?: number;

  // 调仓周期
  rebalance_days?: number;

  // 其它 Mixin 参数
  dynamic_position?: boolean | string;
  market_state_symbol?: string;
}

/** Qlib回测配置 */
export interface QlibBacktestConfig {
  // 基础配置
  symbol: string;
  start_date: string;
  end_date: string;
  initial_capital: number;
  commission?: number;
  slippage?: number;
  user_id: string;
  tenant_id?: string;

  // Qlib特定配置
  qlib_strategy_type: QlibStrategyType;
  qlib_strategy_params: QlibStrategyParams;
  strategy_content?: string;
  qlib_provider_uri?: string;
  qlib_region?: string;
  benchmark_symbol?: string;
  seed?: number;
  use_vectorized?: boolean;
  signal_lag_days?: number;
  allow_feature_signal_fallback?: boolean;
}

/** Qlib回测结果 */
export interface QlibBacktestResult {
  backtest_id: string;
  strategy_name?: string;
  status: 'pending' | 'running' | 'completed' | 'failed';
  created_at: string;
  completed_at?: string;
  progress?: number;
  error_message?: string;
  full_error?: string;
  config?: QlibBacktestConfig;
  portfolio_metrics?: {
    final_value: number;
    account: number;
    position_value: number;
  };
  total_return?: number;
  annual_return?: number;
  max_drawdown?: number;
  volatility?: number;
  sharpe_ratio?: number;
  alpha?: number | null;
  beta?: number;
  information_ratio?: number;
  benchmark_return?: number;
  total_trades?: number;
  execution_time?: number;
  equity_curve?: Array<{ date: string; value: number }>;
  drawdown_curve?: Array<{ date: string; value: number }>;
  trades?: Array<Record<string, any>>;
  
  // 分析归因
  style_attribution?: any;
  factor_metrics?: any;
  stratified_returns?: any[];
  rebalance_suggestions?: any[];
}
