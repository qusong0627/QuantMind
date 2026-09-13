import { describe, test, expect } from 'vitest';
import { splitNotificationsByMarket } from '../notificationScope';

const mk = (title: string, content = '') => ({ title, content });

describe('splitNotificationsByMarket', () => {
  test('带本市场代码的通知归本市场，无代码的归全局', () => {
    // Arrange
    const items = [
      mk('订单超时过期', '600000.SH 订单已超过 30 分钟未成交'),
      mk('回测已完成', 'CustomStrategy 年化 12.04%'),
      mk('触及止损', '00700.HK 跌破止损价'),
    ];

    // Act
    const { market, global } = splitNotificationsByMarket(items, 'HK');

    // Assert
    expect(market.map((n) => n.title)).toEqual(['触及止损']);
    expect(global.map((n) => n.title)).toEqual(['回测已完成']);
  });

  test('其它市场的通知两边都不进（不会顶替显示）', () => {
    // Arrange
    const items = [
      mk('A股订单过期', '600000.SH 订单过期'),
      mk('港股成交', '00700.HK 成交 100 股'),
    ];

    // Act
    const bucket = splitNotificationsByMarket(items, 'US');

    // Assert
    expect(bucket.market).toHaveLength(0);
    expect(bucket.global).toHaveLength(0);
  });

  test('market 为空时全部按全局返回（不做分流）', () => {
    const items = [mk('A股订单过期', '600000.SH 订单过期')];

    const bucket = splitNotificationsByMarket(items, null);

    expect(bucket.market).toHaveLength(0);
    expect(bucket.global).toHaveLength(1);
  });

  test('输入非数组时安全返回空桶（不抛错）', () => {
    const bucket = splitNotificationsByMarket(undefined as never, 'CN');

    expect(bucket.market).toEqual([]);
    expect(bucket.global).toEqual([]);
  });
});
