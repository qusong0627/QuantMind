/**
 * 股票名称索引查询（回测交易明细「股票名称」列）单元测试
 */

import { describe, it, expect } from 'vitest';

import { lookupStockName } from '../marketDataService';

describe('lookupStockName', () => {
  it('前缀式/后缀式/裸代码均按数字键命中', () => {
    const index = new Map<string, string>([['600036', '招商银行']]);
    expect(lookupStockName(index, 'SH600036')).toBe('招商银行');
    expect(lookupStockName(index, '600036.SH')).toBe('招商银行');
    expect(lookupStockName(index, '600036')).toBe('招商银行');
  });

  it('非数字代码回退大写原串', () => {
    const index = new Map<string, string>([['AAPL', '苹果']]);
    expect(lookupStockName(index, 'aapl')).toBe('苹果');
  });

  it('索引未就绪或未命中返回空串', () => {
    expect(lookupStockName(null, 'SH600036')).toBe('');
    expect(lookupStockName(new Map(), 'SH600036')).toBe('');
    expect(lookupStockName(new Map([['600036', '招商银行']]), '')).toBe('');
  });
});
