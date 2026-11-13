import { describe, expect, it } from 'vitest';
import type { StrategyFile } from '../../../../types/backtest/strategy';
import {
  isStandardTopkStrategy,
  sortTradingStrategies,
  tradingStrategySortRank,
} from '../sortTradingStrategies';

function strategy(
  id: string,
  name: string,
  extra: Partial<StrategyFile> = {},
): StrategyFile {
  return {
    id,
    name,
    source: 'personal',
    code: '',
    ...extra,
  };
}

describe('sortTradingStrategies', () => {
  it('pins standard Top-K to sort rank 1 and then orders by numeric id', () => {
    const topk = strategy('42', '默认 Top-K 选股策略', {
      parameters: { strategy_type: 'standard_topk' },
    });
    const later = strategy('8', '自定义量价策略', {
      parameters: { strategy_type: 'custom_price_volume' },
    });
    const earlier = strategy('3', '动量策略', {
      parameters: { strategy_type: 'momentum' },
    });

    expect(tradingStrategySortRank(topk)).toBe(1);
    expect(isStandardTopkStrategy(topk)).toBe(true);

    const sorted = sortTradingStrategies([later, topk, earlier]);
    expect(sorted.map((item) => item.id)).toEqual(['42', '3', '8']);
  });

  it('does not treat other topk templates as the default Top-K', () => {
    const longShort = strategy('2', '多空 Top-K', {
      parameters: { strategy_type: 'long_short_topk' },
    });
    const standard = strategy('9', '默认 Top-K 选股策略', {
      tags: ['template:standard_topk'],
    });
    const sorted = sortTradingStrategies([longShort, standard]);
    expect(sorted.map((item) => item.id)).toEqual(['9', '2']);
  });

  it('pins favorited strategies above everything else', () => {
    const topk = strategy('42', '默认 Top-K 选股策略', {
      parameters: { strategy_type: 'standard_topk' },
    });
    const fav = strategy('8', '我的收藏', { is_favorite: true });
    const plain = strategy('3', '动量策略', {});
    const sorted = sortTradingStrategies([topk, plain, fav]);
    expect(sorted.map((item) => item.id)).toEqual(['8', '42', '3']);
  });
});
