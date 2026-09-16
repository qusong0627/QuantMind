import type { LiveTradeConfig } from '../../../types/liveTrading';
import type { AppMarket } from '../../../store/slices/uiSlice';
import { getMarketSessions } from '../../../config/marketConfig';

export interface ValidationIssue {
  field: string;
  message: string;
}

/** HH:MM ∈ [start, end]；支持跨午夜时段（期货夜盘 21:00–02:30）。 */
function isTimeInRange(value: string, start: string, end: string) {
  if (!value || !start || !end) return false;
  return start <= end ? value >= start && value <= end : value >= start || value <= end;
}

export function validateLiveTradeConfig(
  config: LiveTradeConfig,
  market?: AppMarket,
): ValidationIssue[] {
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

  const enabledSessions = config.enabled_sessions || [];
  if (enabledSessions.length === 0) {
    issues.push({ field: 'enabled_sessions', message: '至少选择一个执行时段' });
  }

  // T-P3-07：时段表按**策略市场本地钟点**（与后端 shared/market_sessions 同口径）
  const sessionRanges = getMarketSessions(market ?? config.market ?? 'CN').sessions;
  const hasWrappingSession = enabledSessions.some((s) => {
    const r = sessionRanges[s];
    return r ? r[0] > r[1] : false;
  });

  // 跨午夜时段的买卖顺序无法用简单的字符串比较表达（如夜盘 23:00 卖 / 01:00 买）——跳过该规则
  if (
    !hasWrappingSession &&
    config.sell_time &&
    config.buy_time &&
    config.sell_time >= config.buy_time
  ) {
    issues.push({ field: 'buy_time', message: '买入时间必须晚于卖出时间' });
  }

  if (config.sell_time) {
    const sellValid = enabledSessions.some((session) => {
      const r = sessionRanges[session];
      return r ? isTimeInRange(config.sell_time, r[0], r[1]) : false;
    });
    if (!sellValid) {
      issues.push({ field: 'sell_time', message: '卖出时间必须落在已选执行时段内' });
    }
  }

  if (config.buy_time) {
    const buyValid = enabledSessions.some((session) => {
      const r = sessionRanges[session];
      return r ? isTimeInRange(config.buy_time, r[0], r[1]) : false;
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
