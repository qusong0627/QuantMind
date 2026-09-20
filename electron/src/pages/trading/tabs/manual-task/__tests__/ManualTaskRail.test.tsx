/**
 * 「风控 · 操作」栏渲染测试。
 *
 * 盯的是一次真实改版事故面：预案算完后主按钮如果又变成不可点的死按钮，
 * 用户就会在顶部「下一步」和右栏之间来回找。这里断言主操作在四个状态下
 * 分别是什么、点下去调的是哪个回调，外加风控裁定确实把 653 条收敛成了一行。
 */

import React from 'react';
import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';
import { ManualTaskRail } from '../ManualTaskRail';
import type { ManualExecutionPreview } from '../../../../../services/realTradingService';

const mkPreview = (overrides: Partial<ManualExecutionPreview> = {}): ManualExecutionPreview => ({
    preview_hash: 'abcdef0123456789abcdef0123456789',
    account_snapshot: { available_cash: 70000, market_value: 120000 } as any,
    strategy_context: { model_id: 'model-x', prediction_trade_date: '2026-09-19' } as any,
    sell_orders: [],
    buy_orders: [],
    skipped_items: [],
    summary: {
        signal_count: 3271,
        buy_order_count: 40,
        sell_order_count: 12,
        skipped_count: 653,
        estimated_sell_proceeds: 28691.11,
        estimated_buy_amount: 517537,
        estimated_remaining_cash: 70.2,
    },
    ...overrides,
} as ManualExecutionPreview);

const mkProps = (overrides: Partial<React.ComponentProps<typeof ManualTaskRail>> = {}) => ({
    step: 'preview' as const,
    preview: null,
    previewLoading: false,
    submitting: false,
    isRealMode: false,
    taskId: '',
    taskCompleted: false,
    onGenerate: vi.fn(),
    onAdvance: vi.fn(),
    onSubmit: vi.fn(),
    onReview: vi.fn(),
    onViewResult: vi.fn(),
    ...overrides,
});

describe('ManualTaskRail', () => {
    it('未生成预案：主按钮是「立即计算调仓预案」，点击触发计算', () => {
        const props = mkProps();
        render(<ManualTaskRail {...props} />);

        const button = screen.getByRole('button', { name: /立即计算调仓预案/ });
        fireEvent.click(button);

        expect(props.onGenerate).toHaveBeenCalledTimes(1);
        expect(props.onAdvance).not.toHaveBeenCalled();
    });

    it('已生成预案：主按钮换成「下一步：确认提交」并可点（不是旧版的死按钮）', () => {
        const props = mkProps({ preview: mkPreview() });
        render(<ManualTaskRail {...props} />);

        const button = screen.getByRole('button', { name: /下一步：确认提交/ });
        expect(button).toBeEnabled();
        fireEvent.click(button);

        expect(props.onAdvance).toHaveBeenCalledTimes(1);
    });

    it('第 5 步未提交：主按钮是「推送执行」，点击触发提交回调', () => {
        const props = mkProps({ step: 'submit', preview: mkPreview() });
        render(<ManualTaskRail {...props} />);

        fireEvent.click(screen.getByRole('button', { name: /推送执行/ }));

        expect(props.onSubmit).toHaveBeenCalledTimes(1);
    });

    it('已提交（taskId 存在）：不再出现「推送执行」，改为执行状态块', () => {
        render(<ManualTaskRail {...mkProps({ step: 'submit', preview: mkPreview(), taskId: 'task-123456' })} />);

        expect(screen.queryByRole('button', { name: /推送执行/ })).toBeNull();
        expect(screen.getByText('任务已进入执行队列')).toBeInTheDocument();
        expect(screen.getByText(/task-123456/)).toBeInTheDocument();
    });

    it('风控裁定：653 条同因拦截收敛成一行，并给出总数', () => {
        const items = Array.from({ length: 653 }, (_, i) => ({
            symbol: `600${String(i).padStart(3, '0')}.SH`,
            action: 'SELL',
            reason: '当前无可卖持仓',
        }));
        render(<ManualTaskRail {...mkProps({ preview: mkPreview({ skipped_items: items } as any) })} />);

        const riskBlock = screen.getByText('风控裁定').closest('section') as HTMLElement;
        // 653 出现两次：大字总数 + 这一组的条数；关键是没有 653 行明细
        expect(within(riskBlock).getAllByText('653')).toHaveLength(2);
        // 同名原因只出现一次
        expect(within(riskBlock).getAllByText('当前无可卖持仓')).toHaveLength(1);
        expect(within(riskBlock).getByText(/笔被拦截 \/ 过滤/)).toBeInTheDocument();
    });

    it('零拦截：写成「全部放行」，不假装有风险', () => {
        render(<ManualTaskRail {...mkProps({ preview: mkPreview() })} />);

        expect(screen.getByText(/全部放行/)).toBeInTheDocument();
    });

    it('资金台账：剩余 ¥70.20 相对买入额被标成「资金偏紧」而不是「充足」', () => {
        render(<ManualTaskRail {...mkProps({ preview: mkPreview() })} />);

        expect(screen.getByText('资金偏紧')).toBeInTheDocument();
        expect(screen.getByText(/不足买入总额 5%/)).toBeInTheDocument();
    });

    it('实盘模式：顶部通道带如实标明实盘 / 真实资金', () => {
        render(<ManualTaskRail {...mkProps({ isRealMode: true, preview: mkPreview() })} />);

        expect(screen.getByText(/实盘通道 · REAL/)).toBeInTheDocument();
        expect(screen.getByText('真实资金')).toBeInTheDocument();
    });

    it('模拟模式：通道带写模拟，且披露里说明不产生真实委托', () => {
        render(<ManualTaskRail {...mkProps({ isRealMode: false, preview: mkPreview() })} />);

        expect(screen.getByText(/模拟通道 · SIMULATION/)).toBeInTheDocument();
        expect(screen.getByText(/本次为模拟通道，不产生真实委托/)).toBeInTheDocument();
    });

    it('重新计算：预案已生成时仍可重算一次（行情变了要能刷）', () => {
        const props = mkProps({ preview: mkPreview() });
        render(<ManualTaskRail {...props} />);

        fireEvent.click(screen.getByRole('button', { name: /重新计算预案/ }));

        expect(props.onGenerate).toHaveBeenCalledTimes(1);
    });

    it('计算中：主按钮禁用，避免重复请求', () => {
        render(<ManualTaskRail {...mkProps({ previewLoading: true })} />);

        expect(screen.getByRole('button', { name: /立即计算调仓预案/ })).toBeDisabled();
    });
});
