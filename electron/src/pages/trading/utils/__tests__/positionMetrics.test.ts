/**
 * 「缺价」在持仓口径里不是 0，更不是亏光。
 *
 * 事故背景（2026-09-22 实盘持仓监控显示浮动盈亏 −84,778.90）：TDX 桥返回的持仓行
 * 只有股数与成本价，现价/市值由后端用 QuantDB 最近收盘价补。补价那天晚上失败，
 * 落库的是 `price: 0, market_value: 0`。本模块把 0 当成了真值 →
 * 每行盈亏 = 0 − 成本 = −100%，十行加起来就是那串吓人的数字，而账户当日实际
 * 是小幅盈利（86090 市值 vs 84778.90 成本）。
 *
 * 同一条口径在本仓已有明文：研究评分「缺失一律 `—`，绝不显示成 0」。
 * 这里补上持仓侧：拿不到价就标 `priceMissing`，不参与任何加减。
 */

import { describe, it, expect } from 'vitest';

import { buildNormalizedHoldings, getPositionSummary } from '../positionMetrics';
import type { AccountInfo } from '../../../../services/realTradingService';

/** TDX 桥的原始行：有股数、有成本价，没有现价/市值（补价失败时后端写 0） */
const tdxMissingPrice = (over: Record<string, unknown> = {}) => ({
    symbol: '000028.SZ',
    volume: 200,
    cost_price: 19.886,
    price: 0,
    market_value: 0,
    ...over,
});

const account = (positions: unknown[], over: Record<string, unknown> = {}) =>
    ({
        total_asset: 920498.86,
        cash: 834408.86,
        market_value: 86090,
        positions,
        ...over,
    }) as unknown as AccountInfo;

describe('buildNormalizedHoldings 的缺价语义', () => {
    it('现价与市值都是 0 但有股数 → 标成缺价', () => {
        const [h] = buildNormalizedHoldings(account([tdxMissingPrice()]));

        expect(h.priceMissing).toBe(true);
    });

    it('缺价行不得报出 −100% 的盈亏', () => {
        const [h] = buildNormalizedHoldings(account([tdxMissingPrice()]));

        // 旧行为：profit = 0 − 200*19.886 = −3977.2，profitPercent = −100
        expect(h.profit).toBe(0);
        expect(h.profitPercent).toBe(0);
        // 成本价仍然要显示出来 —— 缺的是价，不是成本
        expect(h.cost).toBeCloseTo(19.886, 6);
        expect(h.shares).toBe(200);
    });

    it('拿得到价的行照常算盈亏，不受影响', () => {
        const [h] = buildNormalizedHoldings(
            account([
                {
                    symbol: '000028.SZ',
                    volume: 200,
                    cost_price: 19.886,
                    price: 20.03,
                    market_value: 4006,
                },
            ]),
        );

        expect(h.priceMissing).toBe(false);
        expect(h.profit).toBeCloseTo(28.8, 2); // (20.03 − 19.886) * 200
        expect(h.profitPercent).toBeCloseTo(0.724, 2);
    });

    it('零股数的空行仍被剔除，不会混进缺价统计', () => {
        const rows = buildNormalizedHoldings(account([tdxMissingPrice({ volume: 0 })]));

        expect(rows).toHaveLength(0);
    });
});

describe('getPositionSummary 在缺价时回落账户口径', () => {
    it('持仓行合计为 0 而账户有市值 → 用账户口径，不显示成 0', () => {
        const holdings = buildNormalizedHoldings(account([tdxMissingPrice()]));
        const summary = getPositionSummary(account([tdxMissingPrice()]), holdings);

        // 旧行为：Σ h.value = 0 → 总市值 KPI 显示 0.00
        expect(summary.positionValue).toBe(86090);
    });

    it('有正常持仓时仍以持仓合计为准（不被账户旧值覆盖）', () => {
        const info = account(
            [
                { symbol: '000028.SZ', volume: 200, cost_price: 19.886, price: 20.03, market_value: 4006 },
            ],
            { market_value: 999999 },
        );
        const summary = getPositionSummary(info, buildNormalizedHoldings(info));

        expect(summary.positionValue).toBe(4006);
    });
});
