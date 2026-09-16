export type DeployMode = 'REAL' | 'SHADOW' | 'SIMULATION';

export type ScheduleType = 'interval' | 'weekly';
export type TradeWeekday = 'MON' | 'TUE' | 'WED' | 'THU' | 'FRI';
export type TradingSession = 'AM' | 'PM' | 'AFTER_HOURS' | 'NIGHT';
export type LiveOrderType = 'LIMIT' | 'MARKET';

export interface ExecutionConfig {
  max_buy_drop?: number;
  stop_loss?: number;
}

export interface LiveTradeConfig {
  /** 策略市场（T-P3-07：时段按市场本地钟点解释；缺省 CN） */
  market?: 'CN' | 'US' | 'HK' | 'CRYPTO' | 'FUTURES';
  rebalance_days?: 1 | 3 | 5 | 10 | 20;
  schedule_type: ScheduleType;
  trade_weekdays?: TradeWeekday[];
  enabled_sessions: TradingSession[];
  sell_time: string;
  buy_time: string;
  sell_first: boolean;
  order_type: LiveOrderType;
  max_price_deviation?: number;
  max_orders_per_cycle: number;
  /** 全局股票池 ref（如 pool:csi1000），实盘信号裁剪用 */
  pool_id?: string | null;
  /** 仅前端展示用，后端忽略 */
  pool_name?: string | null;
}

export interface StrategyLiveDefaults {
  execution_defaults?: ExecutionConfig;
  live_defaults?: Partial<LiveTradeConfig>;
  live_config_tips?: string[];
}

