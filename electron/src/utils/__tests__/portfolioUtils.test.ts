import { describe, it, expect } from 'vitest';
import { calculatePositionsWinRate, normalizeStockCode, toSuffixCode } from '../portfolioUtils';
import { AccountInfo } from '../../services/realTradingService';

describe('portfolioUtils - 代码口径（中枢唯一入口）', () => {
    it('normalizeStockCode 前缀/后缀/裸码统一收敛为前缀式', () => {
        expect(normalizeStockCode('600519.SH')).toBe('SH600519');
        expect(normalizeStockCode('sh600519')).toBe('SH600519');
        expect(normalizeStockCode('600519')).toBe('SH600519');
        expect(normalizeStockCode('000001')).toBe('SZ000001');
        expect(normalizeStockCode('300750')).toBe('SZ300750');
        expect(normalizeStockCode('430047')).toBe('BJ430047');
        expect(normalizeStockCode('920819')).toBe('BJ920819');
        expect(normalizeStockCode('900901')).toBe('SH900901'); // 沪B
    });

    it('toSuffixCode 裸码按号段补后缀（旧实现把 600519 标成 600519.SZ）', () => {
        expect(toSuffixCode('600519')).toBe('600519.SH');
        expect(toSuffixCode('688981')).toBe('688981.SH');
        expect(toSuffixCode('000001')).toBe('000001.SZ');
        expect(toSuffixCode('300750')).toBe('300750.SZ');
        expect(toSuffixCode('430047')).toBe('430047.BJ');
        expect(toSuffixCode('920819')).toBe('920819.BJ');
    });

    it('toSuffixCode 前缀式入参可转换（旧正则 ^[S[HZB]J]\\d{6}$ 匹配不到任何前缀码）', () => {
        expect(toSuffixCode('SH600519')).toBe('600519.SH');
        expect(toSuffixCode('SZ000001')).toBe('000001.SZ');
        expect(toSuffixCode('BJ430047')).toBe('430047.BJ');
    });

    it('toSuffixCode 后缀式原样返回，非标准码原样返回', () => {
        expect(toSuffixCode('600519.SH')).toBe('600519.SH');
        expect(toSuffixCode('BTCUSDT')).toBe('BTCUSDT');
        expect(toSuffixCode('')).toBe('');
    });
});

describe('portfolioUtils - calculatePositionsWinRate', () => {
    it('should return 0 when accountInfo is null', () => {
        const result = calculatePositionsWinRate(null);
        expect(result.winRate).toBe(0);
        expect(result.total).toBe(0);
    });

    it('should calculate win rate correctly for array of positions', () => {
        const mockAccount: Partial<AccountInfo> = {
            positions: [
                { symbol: '600519', volume: 100, price: 1800, cost_price: 1700 }, // Win
                { symbol: '300750', volume: 200, price: 400, cost_price: 450 },   // Loss
                { symbol: '000001', volume: 0, price: 10, cost_price: 8 },      // Zero volume ignored
                { symbol: '600036', volume: 100, price: 35, cost_price: 30 }    // Win
            ] as any
        };

        const result = calculatePositionsWinRate(mockAccount as AccountInfo);
        expect(result.total).toBe(3); // 600519, 300750, 600036
        expect(result.winning).toBe(2); // 600519, 600036
        expect(result.winRate).toBeCloseTo(66.666, 1);
    });

    it('should calculate win rate correctly for object of positions', () => {
        const mockAccount: Partial<AccountInfo> = {
            positions: {
                '600519': { volume: 100, price: 1800, cost_price: 1700 }, // Win
                '300750': { volume: 200, price: 400, cost_price: 450 }   // Loss
            } as any
        };

        const result = calculatePositionsWinRate(mockAccount as AccountInfo);
        expect(result.total).toBe(2);
        expect(result.winning).toBe(1);
        expect(result.winRate).toBe(50);
    });

    it('should handle missing cost or price safely', () => {
        const mockAccount: Partial<AccountInfo> = {
            positions: [
                { symbol: '600519', volume: 100, price: 1800 }, // No cost, not a win
                { symbol: '300750', volume: 200, cost_price: 450 } // No price, not a win
            ] as any
        };

        const result = calculatePositionsWinRate(mockAccount as AccountInfo);
        expect(result.winning).toBe(0);
        expect(result.winRate).toBe(0);
    });
});
