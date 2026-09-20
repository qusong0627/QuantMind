import { describe, expect, it } from 'vitest';
import {
  describeMarketMismatch,
  filterStrategiesByMarket,
  normalizeStrategyMarket,
  strategyBelongsToMarket,
} from '../strategyMarket';

const hk = { id: '1', name: '港股动量', parameters: { market: 'HK' } };
const cn = { id: '2', name: 'A股动量', parameters: { market: 'CN' } };
const legacy = { id: '3', name: '老策略无市场' };
const us = { id: '4', name: '美股动量', market: 'US' }; // 顶层字段形态（部分接口直接回传）
const garbage = { id: '5', name: '脏数据', parameters: { market: 'not-a-market' } };

describe('strategyMarket', () => {
  it('reads market from parameters first, then top level, and normalizes aliases', () => {
    expect(normalizeStrategyMarket({ parameters: { market: 'hk' } })).toBe('HK');
    expect(normalizeStrategyMarket({ parameters: { market: 'A' } })).toBe('CN');
    expect(normalizeStrategyMarket({ parameters: { market: 'a股' } })).toBe('CN');
    expect(normalizeStrategyMarket({ market: 'us' })).toBe('US');
    expect(normalizeStrategyMarket({ parameters: { market: 'futures' } })).toBe('FUTURES');
    expect(normalizeStrategyMarket({ parameters: { market: 'crypto' } })).toBe('CRYPTO');
  });

  it('returns null for undeclared or unrecognized markets instead of guessing', () => {
    expect(normalizeStrategyMarket(legacy)).toBeNull();
    expect(normalizeStrategyMarket(garbage)).toBeNull();
    expect(normalizeStrategyMarket({ parameters: { market: '  ' } })).toBeNull();
    expect(normalizeStrategyMarket(null)).toBeNull();
    expect(normalizeStrategyMarket(undefined)).toBeNull();
  });

  it('treats undeclared strategies as A-share (backend contract) but never as HK/US', () => {
    // 后端 strategy_storage：`parameters->>'market' IS NULL` 一律计入 A 股视图
    expect(strategyBelongsToMarket(legacy, 'CN')).toBe(true);
    expect(strategyBelongsToMarket(legacy, 'HK')).toBe(false);
    expect(strategyBelongsToMarket(legacy, 'US')).toBe(false);
    // 脏数据市场既不属于 A 股，也不属于任何具体市场
    expect(strategyBelongsToMarket(garbage, 'CN')).toBe(false);
    expect(strategyBelongsToMarket(garbage, 'HK')).toBe(false);
  });

  it('filters the strategy dropdown by market (D4: no HK leakage into the A-share tab)', () => {
    const all = [hk, cn, legacy, us, garbage];

    expect(filterStrategiesByMarket(all, 'CN').map((s) => s.id)).toEqual(['2', '3']);
    expect(filterStrategiesByMarket(all, 'HK').map((s) => s.id)).toEqual(['1']);
    expect(filterStrategiesByMarket(all, 'US').map((s) => s.id)).toEqual(['4']);

    // 未知页签市场 → 原样返回（不因一个前台错误把列表清空）
    expect(filterStrategiesByMarket(all, '')).toHaveLength(5);
    expect(filterStrategiesByMarket(all, 'ZZ')).toHaveLength(5);
    expect(filterStrategiesByMarket(undefined, 'CN')).toEqual([]);
  });

  it('explains a market mismatch with the tag the user must switch to', () => {
    expect(describeMarketMismatch('HK', 'CN')).toContain('HK');
    expect(describeMarketMismatch('HK', 'CN')).toContain('CN');
    // 一致时无话术（不制造噪音）
    expect(describeMarketMismatch('CN', 'CN')).toBe('');
    // 未声明 → 不判定
    expect(describeMarketMismatch(null, 'CN')).toBe('');
  });
});
