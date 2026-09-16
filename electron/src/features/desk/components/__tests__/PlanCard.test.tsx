/**
 * 调仓计划卡：强制分散提示渲染（T-FE-18）——阈值判定用夹具体现，UI 文案与使用同源。
 */

import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
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
