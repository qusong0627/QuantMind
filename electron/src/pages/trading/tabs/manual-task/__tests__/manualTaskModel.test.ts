/**
 * 手动任务第 4/5 步纯函数测试。
 *
 * 盯的是改版前的三个真实毛病：
 *  1. 653 条「当前无可卖持仓」逐条渲染成卡片 → 必须归组成「一行一类原因」；
 *  2. 预案只给「预估剩余 ¥70.20」这种数字，看不出够不够 → 必须有资金裁定；
 *  3. 右侧主按钮在第 4 步算完预案后变成不可点的「预案计算完成」→ 主操作必须唯一且可点。
 */

import { describe, expect, it } from 'vitest';
import {
    CASH_TIGHT_RATIO,
    cashVerdict,
    commonReason,
    formatMoney,
    orderRowView,
    railPrimaryAction,
    sideOf,
    summarizeSkipped,
} from '../manualTaskModel';
import type { ManualExecutionPreviewOrder } from '../../../../../services/realTradingService';

const mkOrder = (overrides: Partial<ManualExecutionPreviewOrder> = {}): ManualExecutionPreviewOrder => ({
    symbol: '600036.SH',
    side: 'BUY',
    quantity: 3200,
    order_type: 'LIMIT',
    price: 12.34,
    reference_price: 12.3,
    estimated_notional: 39488,
    current_volume: 0,
    ...overrides,
});

describe('summarizeSkipped', () => {
    it('同因同向的拦截归成一组：653 条卡片收敛成一行', () => {
        // Arrange：模拟后端真实返回 —— 全仓候选里绝大多数因「无可卖持仓」被过滤
        const items = Array.from({ length: 653 }, (_, i) => ({
            symbol: `600${String(i).padStart(3, '0')}.SH`,
            action: 'SELL',
            reason: '当前无可卖持仓',
        }));

        // Act
        const result = summarizeSkipped(items);

        // Assert
        expect(result.total).toBe(653);
        expect(result.groups).toHaveLength(1);
        expect(result.groups[0].reason).toBe('当前无可卖持仓');
        expect(result.groups[0].count).toBe(653);
        expect(result.groups[0].symbols).toHaveLength(653);
    });

    it('原因相同但方向不同不能合并（买入被拦和卖出被拦是两回事）', () => {
        const result = summarizeSkipped([
            { symbol: 'A.SH', action: 'BUY', reason: '已持仓' },
            { symbol: 'B.SH', action: 'SELL', reason: '已持仓' },
        ]);

        expect(result.groups).toHaveLength(2);
        expect(result.groups.map((g) => g.action).sort()).toEqual(['BUY', 'SELL']);
        expect(result.groups.every((g) => g.count === 1)).toBe(true);
    });

    it('按条数降序：拦截最多的一类永远排第一', () => {
        const result = summarizeSkipped([
            { symbol: 'A.SH', action: 'BUY', reason: '涨停无法买入' },
            { symbol: 'B.SH', action: 'SELL', reason: '当前无可卖持仓' },
            { symbol: 'C.SH', action: 'SELL', reason: '当前无可卖持仓' },
            { symbol: 'D.SH', action: 'SELL', reason: '当前无可卖持仓' },
        ]);

        expect(result.groups[0].reason).toBe('当前无可卖持仓');
        expect(result.groups[0].count).toBe(3);
        expect(result.groups[1].count).toBe(1);
    });

    it('缺 reason / action 的脏数据也要归组，且计入总数（不能静默丢）', () => {
        const result = summarizeSkipped([
            { symbol: 'A.SH', action: '', reason: '' },
            { symbol: '', action: 'BUY', reason: '停牌' },
        ] as any);

        expect(result.total).toBe(2);
        expect(result.groups).toHaveLength(2);
        expect(result.groups.find((g) => g.reason === '未标注原因')?.count).toBe(1);
        // reason 有值但 symbol 为空，也必须计数为 1（count 不能由 symbols 长度推导）
        expect(result.groups.find((g) => g.reason === '停牌')?.count).toBe(1);
    });

    it('空输入 / null 不崩：返回零拦截', () => {
        expect(summarizeSkipped(null)).toEqual({ total: 0, groups: [] });
        expect(summarizeSkipped(undefined)).toEqual({ total: 0, groups: [] });
        expect(summarizeSkipped([])).toEqual({ total: 0, groups: [] });
    });
});

describe('cashVerdict', () => {
    it('剩余为负 → 资金缺口，并给出缺口金额', () => {
        const verdict = cashVerdict(-1234.5, 517537);

        expect(verdict.tone).toBe('short');
        expect(verdict.label).toBe('资金缺口');
        expect(verdict.hint).toContain('¥1,234.50');
    });

    it('剩余不足买入额 5% → 资金偏紧（实测 ¥70.20 / ¥517,537 就是这种极端贴线）', () => {
        const verdict = cashVerdict(70.2, 517537);

        expect(verdict.tone).toBe('tight');
        expect(verdict.hint).toContain('5%');
    });

    it('剩余恰好等于买入额 5% 不算偏紧（边界取严）', () => {
        const verdict = cashVerdict(100000 * CASH_TIGHT_RATIO, 100000);

        expect(verdict.tone).toBe('ok');
    });

    it('无买入或买入为 0 时不做紧张判定，零剩余也不算偏紧', () => {
        expect(cashVerdict(0, 0).tone).toBe('ok');
        expect(cashVerdict(5000, undefined).tone).toBe('ok');
    });

    it('预案没给资金测算 → 原样透出，不假装充足', () => {
        const verdict = cashVerdict(undefined, 517537);

        expect(verdict.label).toBe('--');
        expect(verdict.hint).toContain('未提供');
    });
});

describe('railPrimaryAction', () => {
    it('第 4 步未算预案 → 主操作是「立即计算调仓预案」', () => {
        const action = railPrimaryAction('preview', {
            hasPreview: false,
            previewLoading: false,
            submitting: false,
            hasTask: false,
        });

        expect(action).toEqual({ kind: 'generate', label: '立即计算调仓预案', disabled: false });
    });

    it('第 4 步算完预案 → 主操作换成「下一步：确认提交」（旧版这里是不可点的死按钮）', () => {
        const action = railPrimaryAction('preview', {
            hasPreview: true,
            previewLoading: false,
            submitting: false,
            hasTask: false,
        });

        expect(action?.kind).toBe('advance');
        expect(action?.disabled).toBe(false);
    });

    it('计算中 → 按钮禁用，避免重复请求', () => {
        const action = railPrimaryAction('preview', {
            hasPreview: false,
            previewLoading: true,
            submitting: false,
            hasTask: false,
        });

        expect(action?.disabled).toBe(true);
    });

    it('第 5 步未提交 → 主操作是「推送执行」；没有预案时必须禁用', () => {
        const withPreview = railPrimaryAction('submit', {
            hasPreview: true,
            previewLoading: false,
            submitting: false,
            hasTask: false,
        });
        const withoutPreview = railPrimaryAction('submit', {
            hasPreview: false,
            previewLoading: false,
            submitting: false,
            hasTask: false,
        });

        expect(withPreview).toEqual({ kind: 'submit', label: '推送执行', disabled: false });
        expect(withoutPreview?.disabled).toBe(true);
    });

    it('已在推送中 → 禁用（幂等键也拦不住手抖双击）', () => {
        const action = railPrimaryAction('submit', {
            hasPreview: true,
            previewLoading: false,
            submitting: true,
            hasTask: false,
        });

        expect(action?.disabled).toBe(true);
    });

    it('任务已入队（hasTask）→ 没有主操作，按钮区应改为执行状态', () => {
        expect(
            railPrimaryAction('submit', {
                hasPreview: true,
                previewLoading: false,
                submitting: false,
                hasTask: true,
            }),
        ).toBeNull();
    });
});

describe('orderRowView', () => {
    it('有价委托：数量/价格/金额/持仓/Ref 全部落到单行字段', () => {
        const row = orderRowView(mkOrder());

        expect(row.side).toBe('buy');
        expect(row.sideLabel).toBe('买');
        expect(row.quantityText).toBe('3,200');
        expect(row.priceText).toBe('¥12.34');
        expect(row.amountText).toBe('¥39,488.00');
        expect(row.positionText).toBe('当前无持仓');
        expect(row.referenceText).toBe('Ref ¥12.30');
    });

    it('price=0（行情未取到）→ 明确写「未获取」，金额不给假 0', () => {
        const row = orderRowView(mkOrder({ price: 0, estimated_notional: 0 }));

        expect(row.hasPrice).toBe(false);
        expect(row.priceText).toBe('未获取');
        expect(row.amountText).toBe('--');
    });

    it('卖出委托归到 sell（决定行内用红买绿卖哪一套配色）', () => {
        const row = orderRowView(mkOrder({ side: 'SELL', current_volume: 5000 }));

        expect(row.side).toBe('sell');
        expect(row.sideLabel).toBe('卖');
        expect(row.positionText).toBe('持仓 5,000');
    });

    it('持仓文案分买卖：卖单无可卖是真问题，买单无持仓只是常态', () => {
        const sell = orderRowView(mkOrder({ side: 'SELL', current_volume: 0 }));
        const buy = orderRowView(mkOrder({ side: 'BUY', current_volume: 0 }));

        expect(sell.positionText).toBe('无可卖持仓');
        expect(buy.positionText).toBe('当前无持仓');
    });

    it('缺字段不崩：数量/持仓缺失时给占位符而不是 NaN', () => {
        const row = orderRowView({ symbol: 'A.SH', side: 'BUY' } as ManualExecutionPreviewOrder);

        expect(row.quantityText).toBe('--');
        expect(row.orderTypeText).toBe('MARKET');
        expect(row.positionText).toBe('当前无持仓');
    });
});

describe('commonReason', () => {
    it('整列同因 → 返回该原因（由列头统一说明，行内不再重复 50 遍）', () => {
        const orders = [
            mkOrder({ symbol: 'A.SH', reason: '按预估可用资金等额分配买入预算' }),
            mkOrder({ symbol: 'B.SH', reason: '按预估可用资金等额分配买入预算' }),
        ];

        expect(commonReason(orders)).toBe('按预估可用资金等额分配买入预算');
    });

    it('原因不一致 / 有空原因 → 返回 null，必须逐行标注', () => {
        expect(commonReason([mkOrder({ reason: 'A' }), mkOrder({ reason: 'B' })])).toBeNull();
        expect(commonReason([mkOrder({ reason: 'A' }), mkOrder({ reason: '' })])).toBeNull();
        expect(commonReason([mkOrder({ reason: '' })])).toBeNull();
    });

    it('空列表不崩', () => {
        expect(commonReason([])).toBeNull();
        expect(commonReason(null)).toBeNull();
        expect(commonReason(undefined)).toBeNull();
    });
});

describe('sideOf / formatMoney', () => {
    it('方向归一：大小写与脏值都按买入兜底', () => {
        expect(sideOf('sell')).toBe('sell');
        expect(sideOf('SELL')).toBe('sell');
        expect(sideOf('BUY')).toBe('buy');
        expect(sideOf(undefined)).toBe('buy');
        expect(sideOf('')).toBe('buy');
    });

    it('金额格式化：非法值给 --，不吐 NaN', () => {
        expect(formatMoney(undefined)).toBe('--');
        expect(formatMoney(NaN)).toBe('--');
        expect(formatMoney(0)).toBe('¥0.00');
        expect(formatMoney(517537)).toBe('¥517,537.00');
    });
});

describe('台账展示字段（名称 / 行业 / 板块）', () => {
    it('风控拦截 chip 带出股票名，且与 symbols 一一对应', () => {
        // Arrange：后端已补名（_enrich_preview_display_fields），前端不能再丢
        const items = [
            { symbol: '600036.SH', name: '招商银行', action: 'BUY', reason: '涨停无法买入' },
            { symbol: '300750.SZ', name: '宁德时代', action: 'BUY', reason: '涨停无法买入' },
            { symbol: '600036.SH', name: '', action: 'BUY', reason: '涨停无法买入' },
        ];

        // Act
        const group = summarizeSkipped(items).groups[0];

        // Assert
        expect(group.count).toBe(3);
        expect(group.symbols).toHaveLength(3);
        expect(group.labels.map((l) => l.name)).toEqual(['招商银行', '宁德时代', '']);
        expect(group.labels.map((l) => l.symbol)).toEqual(group.symbols);
    });

    it('无 symbol 的脏行不进 labels，但照样计入 count（不能静默丢拦截）', () => {
        const group = summarizeSkipped([
            { symbol: '', name: '幽灵', action: 'SELL', reason: '当前无可卖持仓' },
            { symbol: '600036.SH', name: '招商银行', action: 'SELL', reason: '当前无可卖持仓' },
        ]).groups[0];

        expect(group.count).toBe(2);
        expect(group.labels).toHaveLength(1);
    });

    it('委托行透出行业与板块（机构口径：光有代码看不出这是什么票）', () => {
        const row = orderRowView(
            mkOrder({ name: '招商银行', industry: '银行', board: '沪主板' }),
        );

        expect(row.name).toBe('招商银行');
        expect(row.industry).toBe('银行');
        expect(row.board).toBe('沪主板');
    });

    it('未补到名称/行业时给空串而不是 undefined，渲染层才能安全判断', () => {
        const row = orderRowView({ symbol: 'A.SH', side: 'BUY' } as ManualExecutionPreviewOrder);

        expect(row.name).toBe('');
        expect(row.industry).toBe('');
        expect(row.board).toBe('');
    });
});
