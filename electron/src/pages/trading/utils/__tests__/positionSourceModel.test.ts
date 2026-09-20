import { describe, expect, test } from 'vitest';
import {
    extraQuoteWriters,
    formatAge,
    formatMoney,
    freshnessStyle,
    nextPreferredSource,
    quoteSampleSymbols,
    resolveViewedSource,
} from '../positionSourceModel';

describe('nextPreferredSource', () => {
    test('点击未选中的源 → 只看它', () => {
        // Arrange / Act / Assert
        expect(nextPreferredSource(null, 'qmt_exec')).toBe('qmt_exec');
        expect(nextPreferredSource('tdx_bridge', 'qmt_exec')).toBe('qmt_exec');
    });

    test('再点当前正在看的源 → 取消偏好，回到跟随交易券商', () => {
        expect(nextPreferredSource('qmt_exec', 'qmt_exec')).toBeNull();
    });
});

describe('resolveViewedSource', () => {
    test('有显式偏好时以偏好为准（哪怕它跟交易券商不是同一个源）', () => {
        expect(resolveViewedSource('tdx_bridge', 'qmt_exec')).toBe('tdx_bridge');
    });

    test('无偏好时跟随交易券商源', () => {
        expect(resolveViewedSource(null, 'qmt_exec')).toBe('qmt_exec');
    });

    test('都拿不到时返回 null（页面据此不标任何 chip 为选中）', () => {
        expect(resolveViewedSource(null, null)).toBeNull();
        expect(resolveViewedSource(null, undefined)).toBeNull();
    });
});

describe('extraQuoteWriters', () => {
    const quote = {
        dominant: 'tdx_bridge',
        sources: [
            { source: 'tdx_bridge', label: '通达信桥', count: 48, newest_age_sec: 3, level: 'fresh' },
            { source: 'qmt_big', label: 'QMT 备源', count: 2, newest_age_sec: 1, level: 'fresh' },
        ],
    };

    test('列出主供数方之外的写源（同一批键被两席轮流写要暴露出来）', () => {
        expect(extraQuoteWriters(quote).map(s => s.source)).toEqual(['qmt_big']);
    });

    test('采样失败（error）时不显示并写源，避免把「读不到」说成「有并写」', () => {
        expect(extraQuoteWriters({ ...quote, error: 'quote_redis_unavailable: x' })).toEqual([]);
    });

    test('无数据时返回空数组', () => {
        expect(extraQuoteWriters(null)).toEqual([]);
        expect(extraQuoteWriters({})).toEqual([]);
    });
});

describe('freshnessStyle', () => {
    test('已知分级给出中文标签', () => {
        expect(freshnessStyle('fresh').label).toBe('实时');
        expect(freshnessStyle('stale').label).toBe('滞后');
        expect(freshnessStyle('unavailable').label).toBe('停更');
    });

    test('未知/缺失分级退化为停更（不冒充实时）', () => {
        expect(freshnessStyle(undefined).label).toBe('停更');
        expect(freshnessStyle('weird').label).toBe('停更');
    });
});

describe('formatAge', () => {
    test('秒/分/时三档', () => {
        expect(formatAge(3)).toBe('3s 前');
        expect(formatAge(90)).toBe('1m 前');
        expect(formatAge(7200)).toBe('2h 前');
    });

    test('缺失/非数 → 破折号（不写成 0s 前冒充新鲜）', () => {
        expect(formatAge(null)).toBe('—');
        expect(formatAge(undefined)).toBe('—');
        expect(formatAge(Number.NaN)).toBe('—');
    });
});

describe('quoteSampleSymbols', () => {
    test('去重、去空白后逗号拼接（后端按逗号切分）', () => {
        expect(quoteSampleSymbols(['SH600036', 'SZ002709', 'SH600036'])).toBe('SH600036,SZ002709');
        expect(quoteSampleSymbols([' SH600036 ', ''])).toBe('SH600036');
    });

    test('空/缺失 → null（让后端回落到馈送名单，而不是发 symbols= 空串）', () => {
        expect(quoteSampleSymbols([])).toBeNull();
        expect(quoteSampleSymbols(null)).toBeNull();
        expect(quoteSampleSymbols(undefined)).toBeNull();
        expect(quoteSampleSymbols(['', '   '])).toBeNull();
    });

    test('超上限截断（URL 别超长）', () => {
        const many = Array.from({ length: 120 }, (_, i) => `SZ${String(i).padStart(6, '0')}`);
        expect(quoteSampleSymbols(many, 100)?.split(',').length).toBe(100);
    });
});

describe('formatMoney', () => {
    test('亿/万简写', () => {
        expect(formatMoney(238_879_107.6)).toBe('2.39 亿');
        expect(formatMoney(919_185.63)).toBe('91.9 万');
    });

    test('0 与非数 → 破折号（0 是占位，不是真实资产）', () => {
        expect(formatMoney(0)).toBe('—');
        expect(formatMoney(null)).toBe('—');
        expect(formatMoney(undefined)).toBe('—');
    });
});
