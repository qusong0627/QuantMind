import type { MarketIndex } from '../../services/marketService';
import { normalizeSymbol } from '../../utils/portfolioUtils';

/** 只使用当日有效的腾讯指数行情；历史日线基准优先。 */
export function getLiveBenchmarkReturn(
  quote: MarketIndex | null,
  symbol: string,
  targetDate: string,
  valueDate: string,
  start: number | null | undefined,
  now = new Date(),
): number | null {
  if (!quote || !targetDate || targetDate > valueDate || normalizeSymbol(quote.symbol) !== symbol || quote.tradeDate !== valueDate || !quote.timestamp) return null;
  const match = /^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2}):(\d{2})$/.exec(quote.timestamp);
  if (!match || match[1] !== valueDate) return null;
  const observed = new Date(`${quote.timestamp}+08:00`).getTime();
  // 保留最后一笔行情直到有更新；日期必须与组合数据点一致。
  if (!Number.isFinite(observed) || observed > now.getTime()) return null;
  const base = targetDate === valueDate ? quote.open : start;
  if (base == null || !Number.isFinite(base) || base <= 0 || !Number.isFinite(quote.price) || quote.price <= 0) return null;
  return (quote.price / base - 1) * 100;
}
