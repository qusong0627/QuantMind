import { describe, test, expect } from 'vitest';
import {
  extractSymbolFromText,
  filterByMarket,
  inferMarketOfSymbol,
  symbolMatchesMarket,
} from '../marketInfer';

describe('inferMarketOfSymbol', () => {
  test('识别 A 股的后缀式/前缀式/裸数字三种写法', () => {
    expect(inferMarketOfSymbol('600036.SH')).toBe('CN');
    expect(inferMarketOfSymbol('000001.SZ')).toBe('CN');
    expect(inferMarketOfSymbol('SH600036')).toBe('CN');
    expect(inferMarketOfSymbol('000001')).toBe('CN');
  });

  test('识别港股（.HK 后缀式）', () => {
    expect(inferMarketOfSymbol('00700.HK')).toBe('HK');
    expect(inferMarketOfSymbol('0700.HK')).toBe('HK');
  });

  test('裸 4-5 位数字与后端同口径：按 CN 兜底（港股落库前已归一为 .HK）', () => {
    expect(inferMarketOfSymbol('0700')).toBe('CN');
    expect(inferMarketOfSymbol('00700')).toBe('CN');
  });

  test('识别美股 ticker（含 BRK.B 这类带点写法）', () => {
    expect(inferMarketOfSymbol('AAPL')).toBe('US');
    expect(inferMarketOfSymbol('BRK.B')).toBe('US');
  });

  test('识别期货与加密', () => {
    expect(inferMarketOfSymbol('RB0.CN')).toBe('FUTURES');
    expect(inferMarketOfSymbol('CL.FUT')).toBe('FUTURES');
    expect(inferMarketOfSymbol('Au99.99')).toBe('FUTURES');
    expect(inferMarketOfSymbol('BTCUSDT')).toBe('CRYPTO');
  });

  test('判据互斥：SHOP 是美股，不会被当成上交所', () => {
    expect(inferMarketOfSymbol('SHOP')).toBe('US');
  });

  test('空值回退 CN（与后端 infer_market 兜底一致）', () => {
    expect(inferMarketOfSymbol('')).toBe('CN');
    expect(inferMarketOfSymbol(null)).toBe('CN');
    expect(inferMarketOfSymbol(undefined)).toBe('CN');
  });
});

describe('symbolMatchesMarket', () => {
  test('market 为空时全部放行', () => {
    expect(symbolMatchesMarket('600036.SH', null)).toBe(true);
    expect(symbolMatchesMarket('AAPL', '')).toBe(true);
  });

  test('按市场精确匹配，大小写不敏感', () => {
    expect(symbolMatchesMarket('00700.HK', 'hk')).toBe(true);
    expect(symbolMatchesMarket('00700.HK', 'CN')).toBe(false);
    expect(symbolMatchesMarket('600036.SH', 'CN')).toBe(true);
  });
});

describe('filterByMarket', () => {
  test('按 symbol 字段过滤，不修改原数组', () => {
    // Arrange
    const items = [{ symbol: '600036.SH' }, { symbol: '00700.HK' }, { symbol: 'AAPL' }];

    // Act
    const hk = filterByMarket(items, 'HK');
    const all = filterByMarket(items, null);

    // Assert
    expect(hk.map((i) => i.symbol)).toEqual(['00700.HK']);
    expect(all).toHaveLength(3);
    expect(items).toHaveLength(3);
  });
});

describe('extractSymbolFromText', () => {
  test('从通知正文里提取带后缀的代码', () => {
    expect(extractSymbolFromText('600000.SH 订单 f6a0 已超过 30 分钟未收到成交回报'))
      .toBe('600000.SH');
    expect(extractSymbolFromText('00700.HK 触及止损')).toBe('00700.HK');
  });

  test('不把普通英文词当 ticker（REAL-TIME / TOTAL 不是标的）', () => {
    expect(extractSymbolFromText('REAL-TIME UPDATES ENABLED')).toBeNull();
    expect(extractSymbolFromText('CustomStrategy 回测完成，年化 12.04%')).toBeNull();
  });

  test('只认标注了交易所的美股 ticker', () => {
    expect(extractSymbolFromText('NASDAQ: AAPL 收盘涨 2%')).toBe('AAPL');
    expect(extractSymbolFromText('系统维护通知')).toBeNull();
  });
});
