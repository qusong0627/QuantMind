/**
 * 策略文件相关类型定义
 */

/** 策略来源类型 */
export type StrategySource = 'upload' | 'personal' | 'template';

/** 策略文件信息 */
export interface StrategyFile {
  id: string;
  name: string;
  source: StrategySource;
  code: string;
  description?: string;
  created_at?: string;
  updated_at?: string;
  is_qlib_format?: boolean;  // 是否为Qlib格式
  language?: 'python' | 'qlib'; // 语言类型
  tags?: string[];
  cos_url?: string;          // COS 预签名 URL（私读，TTL=3600s）
  is_verified?: boolean;     // 是否通过回测验证
  is_system?: boolean;       // 是否为系统内置策略
  parameters?: Record<string, unknown>;
  execution_config?: {
    max_buy_drop?: number;
    stop_loss?: number;
    is_limit_order?: boolean;
    parameters?: Record<string, unknown>;
  };
  live_trade_config?: {
    rebalance_days?: 1 | 3 | 5 | 10 | 20;
    schedule_type?: 'interval' | 'weekly';
    trade_weekdays?: Array<'MON' | 'TUE' | 'WED' | 'THU' | 'FRI'>;
    enabled_sessions?: Array<'AM' | 'PM'>;
    sell_time?: string;
    buy_time?: string;
    sell_first?: boolean;
    order_type?: 'LIMIT' | 'MARKET';
    max_price_deviation?: number;
    max_orders_per_cycle?: number;
  };
  live_config_tips?: string[];
  execution_defaults?: {
    max_buy_drop?: number;
    stop_loss?: number;
  };
  live_defaults?: {
    rebalance_days?: 1 | 3 | 5 | 10 | 20;
    schedule_type?: 'interval' | 'weekly';
    trade_weekdays?: Array<'MON' | 'TUE' | 'WED' | 'THU' | 'FRI'>;
    enabled_sessions?: Array<'AM' | 'PM'>;
    sell_time?: string;
    buy_time?: string;
    sell_first?: boolean;
    order_type?: 'LIMIT' | 'MARKET';
    max_price_deviation?: number;
    max_orders_per_cycle?: number;
  };
}


/** 策略验证结果 */
export interface StrategyValidationResult {
  is_valid: boolean;
  is_qlib_format: boolean;
  /** 运行时引擎：qlib 主引擎 / minibt 脚本运行时（专用 runner 镜像） */
  engine?: 'qlib' | 'minibt';
  errors: StrategyValidationError[];
  warnings: StrategyValidationWarning[];
  suggestions?: string[];
}

/** 策略验证错误 */
export interface StrategyValidationError {
  type: 'syntax' | 'import' | 'structure' | 'compatibility';
  line?: number;
  column?: number;
  message: string;
  severity: 'error' | 'warning';
}

/** 策略验证警告 */
export interface StrategyValidationWarning {
  type: 'performance' | 'deprecated' | 'best_practice';
  line?: number;
  message: string;
}

/** 策略转换请求 */
export interface StrategyConversionRequest {
  source_code: string;
  source_language: 'python' | 'other';
  target_format: 'qlib';
  conversion_options?: {
    preserve_comments?: boolean;
    optimize_logic?: boolean;
    add_type_hints?: boolean;
  };
}

/** 策略转换响应 */
export interface StrategyConversionResponse {
  success: boolean;
  converted_code?: string;
  conversion_notes?: string[];
  errors?: string[];
  warnings?: string[];
}
