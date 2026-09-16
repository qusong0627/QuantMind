/**
 * 调仓计划卡：分散提示渲染（T-FE-18）+ 人工改量交互（T-FE-05 v2）。
 */

import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { PlanBlock, PlanOrder } from '../../types';

vi.mock('../../../shared/useUiMode', () => ({
  useUiMode: () => ({
    mode: 'simple',
    isSimple: true,
    isProfessional: false,
    setMode: vi.fn(),
    toggle: vi.fn(),
  }),
}));
vi.mock('../../services/deskService', () => ({
  executePlan: vi.fn(),
}));

import { PlanCard } from '../PlanCard';

function buyOrder(symbol: string, amount: number): PlanOrder {
  return {
    symbol,
    side: 'BUY',
    quantity: 100,
    price: amount / 100,
    estimated_amount: amount,
    reason: '测试',
    kind: 'rebalance',
    is_limit_up: false,
    is_limit_down: false,
    is_suspended: false,
  };
}

function makePlan(orders: PlanOrder[]): PlanBlock {
  return {
    available: true,
    source: 'engine:test',
    dry_run: true,
    order_count: orders.length,
    orders,
  };
}

describe('PlanCard 分散提示（T-FE-18）', () => {
  it('单票超 15% 时显示琥珀警告（含标的与占比）', () => {
    render(
      <PlanCard
        plan={makePlan([
          buyOrder('600000', 40000),
          buyOrder('600001', 30000),
          buyOrder('600002', 30000),
        ])}
      />
    );
    expect(screen.getByText(/分散提示/)).toBeTruthy();
    expect(screen.getByText(/40\.0%/)).toBeTruthy();
    expect(screen.getAllByText(/600000/).length).toBeGreaterThan(0);
  });

  it('分布合理时显示通过态（单票最大占比与只数）', () => {
    render(
      <PlanCard
        plan={makePlan(
          Array.from({ length: 8 }, (_, i) => buyOrder(`60000${i}`, 10000))
        )}
      />
    );
    expect(screen.getByText(/分散检查 ✓/)).toBeTruthy();
    expect(screen.getByText(/12\.5%/)).toBeTruthy();
  });

  it('只有卖出（无买单）时不显示分散行', () => {
    render(
      <PlanCard
        plan={makePlan([
          {
            ...buyOrder('600000', 100000),
            side: 'SELL',
          },
        ])}
      />
    );
    expect(screen.queryByText(/分散检查|分散提示/)).toBeNull();
  });
});

describe('PlanCard 人工改量（T-FE-05 v2）', () => {
  it('改量后执行载荷带 quantity_overrides；退出单无数量输入', async () => {
    const user = userEvent.setup();
    const { executePlan } = await import('../../services/deskService');
    const mocked = vi.mocked(executePlan);
    mocked.mockReset();
    mocked.mockResolvedValue({
      success: true,
      data: {
        strategy_id: '1',
        mode: 'SIMULATION',
        excluded: [],
        quantity_overrides: [{ symbol: '600000', side: 'BUY', quantity: 1200 }],
        report: { order_count: 2, filled_count: 2, rejected_count: 0 },
        source: 'test',
      },
    });

    render(
      <PlanCard
        plan={makePlan([
          buyOrder('600000', 40000),
          { ...buyOrder('600519', 30000), side: 'SELL', kind: 'exit', reason: '止损' },
        ])}
      />
    );
    await user.click(screen.getByRole('button', { name: /一键执行/ }));

    // 改量：600000 由 100 → 1200
    const input = screen.getByLabelText('600000 数量');
    await user.clear(input);
    await user.type(input, '1200');
    expect(screen.getByText(/改量 1 笔/)).toBeTruthy();

    // 退出单行没有数量输入（风控不绕过）
    expect(screen.queryByLabelText('600519 数量')).toBeNull();
    expect(screen.getByText(/退出规则 · 不可排除\/改量/)).toBeTruthy();

    await user.click(screen.getByRole('button', { name: '确认执行' }));
    await vi.waitFor(() => expect(mocked).toHaveBeenCalledTimes(1));
    expect(mocked).toHaveBeenCalledWith([], [{ symbol: '600000', side: 'BUY', quantity: 1200 }]);
  });

  it('非法改量（0）拦截确认按钮并给出提示', async () => {
    const user = userEvent.setup();
    const { executePlan } = await import('../../services/deskService');
    vi.mocked(executePlan).mockClear();

    render(<PlanCard plan={makePlan([buyOrder('600000', 40000)])} />);
    await user.click(screen.getByRole('button', { name: /一键执行/ }));

    const input = screen.getByLabelText('600000 数量');
    await user.clear(input);
    await user.type(input, '0');
    expect(screen.getByText(/存在不合法的改量输入/)).toBeTruthy();
    const okBtn = screen.getByRole('button', { name: '确认执行' });
    expect(okBtn).toBeDisabled();

    await user.click(okBtn);
    expect(vi.mocked(executePlan)).not.toHaveBeenCalled();
  });
});
