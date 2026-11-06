import { describe, expect, it } from 'vitest';
import { getLiveBenchmarkReturn } from './benchmarkQuote';
import type { MarketIndex } from '../../services/marketService';
const quote: MarketIndex = { symbol:'SH000905', name:'中证500', price:110, open:100, change:0, changePercent:0, tradeDate:'2026-11-06', timestamp:'2026-11-06T14:59:30' };
const now = new Date('2026-11-06T08:00:00Z');
describe('live index benchmark', () => {
  it('uses today open when target is today and historical open otherwise', () => {
    expect(getLiveBenchmarkReturn(quote,'000905.SH','2026-11-06','2026-11-06',null,now)).toBeCloseTo(10);
    expect(getLiveBenchmarkReturn(quote,'000905.SH','2026-11-03','2026-11-06',80,now)).toBeCloseTo(37.5);
  });
  it('rejects stale, wrong symbol, wrong day and missing base', () => {
    expect(getLiveBenchmarkReturn({...quote,timestamp:'2026-11-06T14:40:00'},'000905.SH','2026-11-06','2026-11-06',null,now)).toBeNull();
    expect(getLiveBenchmarkReturn(quote,'000300.SH','2026-11-06','2026-11-06',null,now)).toBeNull();
    expect(getLiveBenchmarkReturn(quote,'000905.SH','2026-11-06','2026-11-05',null,now)).toBeNull();
    expect(getLiveBenchmarkReturn(quote,'000905.SH','2026-11-03','2026-11-06',null,now)).toBeNull();
    expect(getLiveBenchmarkReturn(quote,'000905.SH','2026-11-06','2026-11-06',null,new Date('2026-11-07T08:00:00Z'))).toBeNull();
  });
});
