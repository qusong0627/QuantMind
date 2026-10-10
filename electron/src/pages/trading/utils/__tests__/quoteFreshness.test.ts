import { describe, expect, it } from 'vitest';
import {
  DAILY_FALLBACK_SOURCE,
  parseQuoteTs,
  classifyQuote,
  quoteAgeText,
  type QuoteMeta,
} from '../quoteFreshness';

const at = (hhMm: string, base = '2026-10-12') => Date.parse(`${base}T${hhMm}:00Z`);
const now = at('02:00'); // 北京 10:00 盘中（周一）

const meta = (over: Partial<QuoteMeta> = {}): QuoteMeta => ({
  price: 10,
  isStale: false,
  source: 'tdx_bridge',
  tsMs: now - 5_000,
  ...over,
});

describe('quoteFreshness', () => {
  it('parseQuoteTs：epoch 秒 / 毫秒 / ISO 自适应；缺失与坏值 → null', () => {
    expect(parseQuoteTs(1_789_002_005)).toBe(1_789_002_005_000); // 秒 → 毫秒
    expect(parseQuoteTs(1_789_002_005_000)).toBe(1_789_002_005_000);
    expect(parseQuoteTs('2026-10-10T01:30:05Z')).toBe(Date.parse('2026-10-10T01:30:05Z'));
    expect(parseQuoteTs(null)).toBeNull();
    expect(parseQuoteTs(undefined)).toBeNull();
    expect(parseQuoteTs('')).toBeNull();
    expect(parseQuoteTs('garbage')).toBeNull();
    expect(parseQuoteTs(0)).toBeNull();
    expect(parseQuoteTs(-1)).toBeNull();
  });

  it('classifyQuote：quantdb 日线兜底独立分级，不按秒计时', () => {
    const f = classifyQuote(meta({ source: DAILY_FALLBACK_SOURCE, tsMs: null }), now, true);
    expect(f.kind).toBe('daily');
    expect(f.ageSec).toBeNull();
    // 即便服务端标 stale，日线兜底仍是 daily（时间戳语义不同）
    const f2 = classifyQuote(meta({ source: DAILY_FALLBACK_SOURCE, isStale: true }), now, true);
    expect(f2.kind).toBe('daily');
  });

  it('classifyQuote：时段内超过 60s 或服务端判 stale → stale；60s 内 → fresh', () => {
    expect(classifyQuote(meta({ tsMs: now - 120_000 }), now, true).kind).toBe('stale');
    expect(classifyQuote(meta({ tsMs: now - 30_000 }), now, true).kind).toBe('fresh');
    expect(classifyQuote(meta({ tsMs: now - 120_000 }), now, true).ageSec).toBe(120);
    // 恰在 60s 线上不算陈旧（与服务端 fresh 档一致）
    expect(classifyQuote(meta({ tsMs: now - 60_000 }), now, true).kind).toBe('fresh');
    // 服务端 stale 标记时段内直接生效（推送判重只比价格，安静标的需要服务端兜底）
    expect(classifyQuote(meta({ isStale: true, tsMs: now - 5_000 }), now, true).kind).toBe('stale');
  });

  it('classifyQuote：非交易时段不按秒计时——安静标的没推送 ≠ 断流，收盘后不误报', () => {
    expect(classifyQuote(meta({ tsMs: now - 3_600_000 }), now, false).kind).toBe('fresh');
    // 收盘后连服务端 is_stale 也不翻红（那是推送时刻的判断，不是当前断流）
    expect(classifyQuote(meta({ isStale: true, tsMs: now - 3_600_000 }), now, false).kind).toBe('fresh');
    // 但 quantdb 日线兜底不受时段影响
    expect(classifyQuote(meta({ source: DAILY_FALLBACK_SOURCE }), now, false).kind).toBe('daily');
  });

  it('classifyQuote：时间戳缺失不吓人（服务端没说 stale 就按 fresh），未来戳钳到 0 秒', () => {
    const noTs = classifyQuote(meta({ tsMs: null }), now, true);
    expect(noTs.kind).toBe('fresh');
    expect(noTs.ageSec).toBeNull();
    const future = classifyQuote(meta({ tsMs: now + 5_000 }), now, true);
    expect(future.ageSec).toBe(0);
    expect(future.kind).toBe('fresh');
  });

  it('quoteAgeText：秒/分/小时三档', () => {
    expect(quoteAgeText(0)).toBe('0s');
    expect(quoteAgeText(89)).toBe('89s');
    expect(quoteAgeText(90)).toBe('1min');
    expect(quoteAgeText(3599)).toBe('59min');
    expect(quoteAgeText(3600)).toBe('1.0h');
    expect(quoteAgeText(5400)).toBe('1.5h');
  });
});
