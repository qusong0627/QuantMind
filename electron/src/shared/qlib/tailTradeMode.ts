/**
 * 尾盘交易模式共享工具
 *
 * 统一两种执行口径：
 * - ON:  当日预测 + 收盘成交 (deal_price=close, signal_lag_days=0)
 * - OFF: T+1 预测生效 + 开盘成交 (deal_price=open, signal_lag_days=1)
 *
 * 所有回测入口共享同一个 localStorage 键，保证口径一致。
 */

export const TAIL_TRADE_MODE_STORAGE_KEY = 'backtest_tail_trade_mode';

export const getStoredTailTradeMode = (): boolean => {
  if (typeof window === 'undefined') return false;
  return window.localStorage.getItem(TAIL_TRADE_MODE_STORAGE_KEY) === '1';
};

export const setStoredTailTradeMode = (enabled: boolean): void => {
  if (typeof window === 'undefined') return;
  window.localStorage.setItem(TAIL_TRADE_MODE_STORAGE_KEY, enabled ? '1' : '0');
};

export const getTailTradeDealPrice = (enabled: boolean): 'open' | 'close' =>
  enabled ? 'close' : 'open';

export const getTailTradeSignalLagDays = (enabled: boolean): number =>
  enabled ? 0 : 1;

/**
 * 是否强制禁止信号缺失时的 feature 降级回退。
 * 文档口径要求始终为 false，避免静默降级污染口径。
 */
export const ALLOW_FEATURE_SIGNAL_FALLBACK = false;
