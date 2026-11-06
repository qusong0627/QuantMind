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
  const age = now.getTime() - observed;
  if (!Number.isFinite(observed) || age < 0) return null;
  const local = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).formatToParts(now);
  const part = (name: string) => local.find(p => p.type === name)?.value || '';
  const today = `${part('year')}-${part('month')}-${part('day')}`;
  if (today !== valueDate) return null;
  const nowMinutes = Number(part('hour')) * 60 + Number(part('minute'));
  const quoteMinutes = Number(match[2]) * 60 + Number(match[3]);
  if (nowMinutes < 15 * 60 ? age > 300000 : quoteMinutes < 14 * 60 + 55) return null;
  const base = targetDate === valueDate ? quote.open : start;
  if (base == null || !Number.isFinite(base) || base <= 0 || !Number.isFinite(quote.price) || quote.price <= 0) return null;
  return (quote.price / base - 1) * 100;
}
