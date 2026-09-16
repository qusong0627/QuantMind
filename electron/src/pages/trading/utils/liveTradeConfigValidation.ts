import type { LiveTradeConfig, TradingSession } from '../../../types/liveTrading';

export interface ValidationIssue {
  field: string;
  message: string;
}

function isTimeInRange(value: string, start: string, end: string) {
  return value >= start && value <= end;
}

export function validateLiveTradeConfig(config: LiveTradeConfig): ValidationIssue[] {
  const issues: ValidationIssue[] = [];

  if (config.schedule_type === 'interval' && !config.rebalance_days) {
    issues.push({ field: 'rebalance_days', message: '请选择调仓周期' });
  }

  if (config.schedule_type === 'weekly' && (!config.trade_weekdays || config.trade_weekdays.length === 0)) {
    issues.push({ field: 'trade_weekdays', message: '按周执行至少选择一个交易日' });
  }

  if (!config.sell_time) {
    issues.push({ field: 'sell_time', message: '请选择卖出时间' });
  }

  if (!config.buy_time) {
    issues.push({ field: 'buy_time', message: '请选择买入时间' });
  }

  if (config.sell_time && config.buy_time && config.sell_time >= config.buy_time) {
    issues.push({ field: 'buy_time', message: '买入时间必须晚于卖出时间' });
  }

  const enabledSessions = config.enabled_sessions || [];
  if (enabledSessions.length === 0) {
    issues.push({ field: 'enabled_sessions', message: '至少选择一个执行时段' });
  }

  const sessionRanges = {
    AM: ['00:00', '23:59'],
    PM: ['00:00', '23:59'],
    // 盘后固定价格交易（T-P2-07）：15:05–15:30，与后端 market_rules 同口径
    AFTER_HOURS: ['15:05', '15:30'],
  } as const satisfies Record<TradingSession, readonly [string, string]>;

  if (config.sell_time) {
    const sellValid = enabledSessions.some((session) => {
      const [start, end] = sessionRanges[session];
      return isTimeInRange(config.sell_time, start, end);
    });
    if (!sellValid) {
      issues.push({ field: 'sell_time', message: '卖出时间必须落在已选执行时段内' });
    }
  }

  if (config.buy_time) {
    const buyValid = enabledSessions.some((session) => {
      const [start, end] = sessionRanges[session];
      return isTimeInRange(config.buy_time, start, end);
    });
    if (!buyValid) {
      issues.push({ field: 'buy_time', message: '买入时间必须落在已选执行时段内' });
    }
  }

  if (config.order_type === 'LIMIT' && (config.max_price_deviation == null || config.max_price_deviation < 0 || config.max_price_deviation > 0.05)) {
    issues.push({ field: 'max_price_deviation', message: '限价单的价格偏离容忍必须在 0% 到 5% 之间' });
  }

  if (!config.max_orders_per_cycle || config.max_orders_per_cycle < 1 || config.max_orders_per_cycle > 100) {
    issues.push({ field: 'max_orders_per_cycle', message: '单轮最大委托数必须在 1 到 100 之间' });
  }

  return issues;
}

