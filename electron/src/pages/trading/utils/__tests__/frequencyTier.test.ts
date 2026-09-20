import { describe, expect, it } from 'vitest';
import {
  describeRhythm,
  deriveFrequencyTier,
  FREQUENCY_TIERS,
  INTRADAY_TIER,
} from '../frequencyTier';

describe('frequencyTier', () => {
  it('maps rebalance_days to the four tiers the platform actually offers', () => {
    expect(deriveFrequencyTier({ rebalance_days: 20 }).key).toBe('low');
    expect(deriveFrequencyTier({ rebalance_days: 10 }).key).toBe('low');
    expect(deriveFrequencyTier({ rebalance_days: 5 }).key).toBe('medium');
    expect(deriveFrequencyTier({ rebalance_days: 3 }).key).toBe('medium');
    expect(deriveFrequencyTier({ rebalance_days: 1 }).key).toBe('daily');
  });

  it('never claims intraday-high-frequency support (platform has no tick matching)', () => {
    const tier = deriveFrequencyTier({ rebalance_days: 1, enabled_sessions: ['AM', 'PM'] });
    expect(tier.key).toBe('daily');
    expect(tier.key).not.toBe('intraday');
    // 档位表里必须有一项明确标注「未开放」，而不是假装支持
    expect(INTRADAY_TIER.supported).toBe(false);
    expect(FREQUENCY_TIERS.every((t) => t.supported)).toBe(true);
    expect(INTRADAY_TIER.note.length).toBeGreaterThan(0);
  });

  it('falls back to an explicit unknown tier rather than guessing', () => {
    // 非法的 rebalance_days（7 不在 1/3/5/10/20 白名单内）
    const odd = deriveFrequencyTier({ rebalance_days: 7 });
    expect(odd.supported).toBe(false);
    expect(odd.key).toBe('unknown');

    const missing = deriveFrequencyTier({});
    expect(missing.key).toBe('unknown');
    expect(deriveFrequencyTier(null).key).toBe('unknown');
  });

  it('weekly schedules are low frequency regardless of rebalance_days', () => {
    const weekly = deriveFrequencyTier({ schedule_type: 'weekly', trade_weekdays: ['MON'], rebalance_days: 1 });
    expect(weekly.key).toBe('low');
    expect(weekly.basis).toContain('weekly');
  });

  it('describes the rhythm in the operator vocabulary (sessions, order, times, caps)', () => {
    const text = describeRhythm({
      rebalance_days: 3,
      sell_time: '14:45',
      buy_time: '14:50',
      sell_first: true,
      enabled_sessions: ['PM'],
      max_orders_per_cycle: 20,
    });
    expect(text).toContain('14:45');
    expect(text).toContain('14:50');
    expect(text).toContain('先卖后买');
    expect(text).toContain('下午盘');
    expect(text).toContain('20');

    const buyFirst = describeRhythm({ sell_first: false, enabled_sessions: ['AM'] });
    expect(buyFirst).toContain('先买后卖');
    expect(buyFirst).toContain('上午盘');

    // 空配置不炸，也不编造时段
    expect(describeRhythm(null)).toBe('');
  });
});
