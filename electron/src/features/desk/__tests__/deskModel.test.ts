import { describe, expect, it } from 'vitest';
import type { PipelineStep, PlanBlock, PlanOrder } from '../../types';
import {
  executionSummary,
  formatMoney,
  formatPct,
  healthItemViews,
  pipelineSummary,
  planKindLabel,
  planSummary,
  pnlSummary,
  statusStyle,
} from '../deskModel';

const order = (over: Partial<PlanOrder>): PlanOrder => ({
  symbol: '600036.SH',
  side: 'BUY',
  quantity: 100,
  price: 10,
  estimated_amount: 1000,
  reason: '调仓买入: 当前0 → 目标100',
  kind: 'rebalance',
  is_limit_up: false,
  is_limit_down: false,
  is_suspended: false,
  ...over,
});

describe('statusStyle / pipelineSummary', () => {
  it('四态样式与最差状态优先级（fail > warn > unknown > ok）', () => {
    expect(statusStyle('fail').dot).toContain('rose');
    expect(statusStyle(undefined).label).toBe('未运行');

    const steps = [
      { status: 'ok' }, { status: 'ok' }, { status: 'warn' },
    ] as PipelineStep[];
    const summary = pipelineSummary(steps);
    expect(summary).toMatchObject({ ok: 2, warn: 1, fail: 0, unknown: 0, worst: 'warn' });

    const worstFail = pipelineSummary([{ status: 'fail' }, { status: 'warn' }] as PipelineStep[]);
    expect(worstFail.worst).toBe('fail');
    // 全 ok → worst=ok；空 → unknown
    expect(pipelineSummary([{ status: 'ok' }] as PipelineStep[]).worst).toBe('ok');
    expect(pipelineSummary([]).worst).toBe('unknown');
    // 未知状态计入 unknown（不吞）
    expect(pipelineSummary([{ status: 'weird' }] as PipelineStep[]).unknown).toBe(1);
  });
});

describe('planSummary / planKindLabel', () => {
  const plan: PlanBlock = {
    available: true,
    source: 'x',
    dry_run: true,
    orders: [
      order({ side: 'BUY', estimated_amount: 1000 }),
      order({ side: 'SELL', estimated_amount: 2000, kind: 'rebalance' }),
      order({ side: 'SELL', estimated_amount: 300.5, kind: 'exit' }),
    ],
  };

  it('买/卖分组与金额合计；退出规则单数单独可见', () => {
    const summary = planSummary(plan);
    expect(summary.buys).toHaveLength(1);
    expect(summary.sells).toHaveLength(2);
    expect(summary.exits).toBe(1);
    expect(summary.buyAmount).toBe(1000);
    expect(summary.sellAmount).toBe(2300.5);
  });

  it('不可用/空计划不抛错', () => {
    expect(planSummary(undefined).buys).toEqual([]);
    expect(planSummary({ available: false, source: 'x' }).sellAmount).toBe(0);
  });

  it('触发类别标签', () => {
    expect(planKindLabel('exit')).toBe('退出规则');
    expect(planKindLabel('rebalance')).toBe('定期调仓');
    expect(planKindLabel(undefined)).toBe('—');
  });
});

describe('pnlSummary', () => {
  it('可用且有本金 → 累计收益率；无本金 → null（不硬算）', () => {
    const ok = pnlSummary({
      available: true, source: 'x', total_pnl: 10000, today_pnl: 500, initial_capital: 100000,
    });
    expect(ok.returnPct).toBeCloseTo(0.1);
    const noInitial = pnlSummary({ available: true, source: 'x', total_pnl: 1 });
    expect(noInitial.returnPct).toBeNull();
    const unavailable = pnlSummary({ available: false, source: 'x' });
    expect(unavailable.available).toBe(false);
    expect(pnlSummary(undefined).totalPnl).toBe(0);
  });
});

describe('executionSummary / healthItemViews / 格式化', () => {
  it('执行汇总缺字段回退 0', () => {
    expect(executionSummary(undefined)).toEqual({ simCount: 0, realCount: 0, filled: 0, rejected: 0 });
    expect(
      executionSummary({ source: 's', sim_count: 2, real_count: 1, filled: 1, rejected: 1 })
    ).toEqual({ simCount: 2, realCount: 1, filled: 1, rejected: 1 });
  });

  it('健康项 → 视图（样式映射 + 下钻 detail/suggestion 保留）', () => {
    const views = healthItemViews({
      ok: 1, warn: 1, fail: 0, source: 's',
      items: [
        { id: 'C08', name: '数据同步', level: 'ok', detail: '已同步', suggestion: '' },
        { id: 'C05', name: '台账', level: 'warn', detail: '空', suggestion: '检查台账写入' },
      ],
    });
    expect(views).toHaveLength(2);
    expect(views[0].style.dot).toContain('red');
    expect(views[1].style.dot).toContain('amber');
    expect(views[1].suggestion).toContain('台账');
  });

  it('金额/百分比格式化容错', () => {
    expect(formatMoney(null)).toBe('—');
    expect(formatMoney(1234.5)).toContain('1,234.50');
    expect(formatPct(0.1234)).toBe('12.34%');
    expect(formatPct(null)).toBe('—');
  });
});
