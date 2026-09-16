import { describe, expect, it } from 'vitest';
import type { PipelineStep, PlanBlock, PlanOrder } from '../../types';
import {
  executionSummary,
  formatMoney,
  formatPct,
  healthItemViews,
  pipelineSummary,
  planDrillEntries,
  planKindLabel,
  planSummary,
  pnlDrillEntries,
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

describe('下钻条目（T-FE-03）', () => {
  it('盈亏块 → 条目：字段分解 + source；不可用 → 状态条目（含 detail）', () => {
    const entries = pnlDrillEntries({
      available: true,
      source: 'db:simulation_fund_snapshots',
      total_asset: 2010176.58,
      initial_capital: 2000000,
      total_pnl: 10176.58,
      today_pnl: 1797,
      market_value: 521211,
      snapshot_date: '2026-09-16',
    });
    const labels = entries.map((e) => e.label);
    expect(labels).toContain('总资产');
    expect(labels).toContain('累计收益');
    expect(entries.every((e) => e.source === 'db:simulation_fund_snapshots')).toBe(true);
    const pnlEntry = entries.find((e) => e.label === '累计收益');
    expect(pnlEntry?.hint).toContain('0.51%'); // 收益率推算写入 hint

    const empty = pnlDrillEntries({ available: false, source: 'x', detail: '无资金快照' });
    expect(empty).toHaveLength(1);
    expect(empty[0].value).toBe('无资金快照');
  });

  it('计划块 → 摘要条目 + 前 N 笔明细（退出规则标注在 source 列）', () => {
    const plan = {
      available: true,
      source: 'simulation engine dry-run',
      strategy_name: '测试策略',
      mode: 'SIMULATION',
      signal_count: 1000,
      order_count: 3,
      error: null,
      orders: [
        order({ side: 'BUY', symbol: '600036.SH', estimated_amount: 1000 }),
        order({ side: 'SELL', symbol: '002552.SZ', estimated_amount: 2000 }),
        order({ side: 'SELL', symbol: '688596.SH', estimated_amount: 300, kind: 'exit', reason: '止损触发' }),
      ],
    };
    const entries = planDrillEntries(plan, 2); // topN=2 截断明细
    expect(entries.find((e) => e.label === '信号数')?.value).toBe('1000');
    expect(entries.find((e) => e.label === '计划笔数')?.value).toContain('卖 2');
    const orderEntries = entries.filter((e) => /^(买|卖) /.test(e.label));
    expect(orderEntries).toHaveLength(2);
    expect(orderEntries[0].source).toBe('定期调仓');
    expect(orderEntries[0].hint).toContain('调仓买入');

    const unavailable = planDrillEntries({ available: false, source: 'redis', reason: '无活跃策略' });
    expect(unavailable[0].value).toBe('无活跃策略');
  });
});
