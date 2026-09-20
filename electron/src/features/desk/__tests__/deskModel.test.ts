import { describe, expect, it } from 'vitest';
import type { EvidenceRing, PipelineStep, PlanBlock, PlanOrder } from '../../types';
import {
  buildExecuteSelection,
  evidenceRingDrillEntries,
  evidenceSummary,
  excludedSymbolsFromPlan,
  executionItemDrillEntries,
  executionSummary,
  executionUnavailableReason,
  formatMoney,
  formatPct,
  hasInvalidQuantityEdit,
  healthItemViews,
  pipelineStepDrillEntries,
  pipelineSummary,
  planDrillEntries,
  planKindLabel,
  planOrderDrillEntries,
  planSummary,
  pnlDrillEntries,
  pnlSummary,
  quantityAdjustmentSummary,
  quantityOverridesFromPlan,
  signalItemDrillEntries,
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
    expect(statusStyle('fail').dot).toContain('red');
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

  it('不可归集的委托块识别出原因（不退化成"今天没交易"）', () => {
    // CN / 正常块 → 无原因
    expect(executionUnavailableReason(undefined)).toBeNull();
    expect(executionUnavailableReason({ source: 's' })).toBeNull();
    expect(executionUnavailableReason({ source: 's', available: true, sim_count: 0 })).toBeNull();

    // available:false（非 CN 市场无 market 列）→ 必须回传后端原因
    const reason = executionUnavailableReason({
      source: 'desk:market-scope',
      available: false,
      market: 'HK',
      reason: 'HK 市场委托暂不可按市场归集：委托表 sim_orders 无 market 列',
    });
    expect(reason).toContain('sim_orders');

    // 后端没给 reason 时也要有可读兜底，不能返回空串
    expect(executionUnavailableReason({ source: 's', available: false, market: 'US' })).toContain('US');
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
    expect(views[0].style.dot).toContain('emerald');
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

describe('一键执行选择模型（T-FE-05）', () => {
  const plan: PlanBlock = {
    available: true,
    source: 'x',
    orders: [
      order({ side: 'BUY', symbol: '600036.SH' }),
      order({ side: 'SELL', symbol: '002552.SZ' }),
      order({ side: 'SELL', symbol: '688596.SH', kind: 'exit', reason: '止损触发' }),
    ],
  };

  it('退出规则单锁定进 locked（不可排除），其余可勾选', () => {
    const sel = buildExecuteSelection(plan, new Set());
    expect(sel.locked.map((x) => x.order.symbol)).toEqual(['688596.SH']);
    expect(sel.selectable.map((x) => x.order.symbol)).toEqual(['600036.SH', '002552.SZ']);
    expect(sel.executableCount).toBe(3);
  });

  it('勾除调仓单后执行数下降；退出单即使在下标集合中也不被排除', () => {
    const excluded = new Set([0, 2]); // 勾除 BUY 与退出单（退出单应被忽略）
    const sel = buildExecuteSelection(plan, excluded);
    expect(sel.executableCount).toBe(2); // 3 - 1（退出单不计入排除）
    expect(excludedSymbolsFromPlan(plan, excluded)).toEqual(['600036.SH']);
  });

  it('空计划不抛错', () => {
    expect(buildExecuteSelection(undefined, new Set()).executableCount).toBe(0);
    expect(excludedSymbolsFromPlan(null, new Set<number>())).toEqual([]);
  });
});

describe('证据矩阵（T-FE-16）', () => {
  const ring = (over: Partial<EvidenceRing>): EvidenceRing => ({
    key: 'data', label: '数据', artifact: '数据质检报告', frequency: '每日',
    level: 'ok', summary: '已同步', items: [], ...over,
  });

  it('汇总：四态计数 + 无证据环名单（无证据 ≠ 绿）', () => {
    const summary = evidenceSummary([
      ring({ level: 'ok' }), ring({ level: 'warn' }), ring({ level: 'fail' }),
      ring({ level: 'no_evidence', label: '特征' }), ring({ level: 'no_evidence', label: '模拟' }),
    ]);
    expect(summary).toMatchObject({ ok: 1, warn: 1, fail: 1, noEvidence: 2 });
    expect(summary.gapLabels).toEqual(['特征', '模拟']);
    expect(evidenceSummary(null).noEvidence).toBe(0);
  });

  it('no_evidence 为深黄「无证据」（不是绿、也不是"未运行"）', () => {
    const style = statusStyle('no_evidence');
    expect(style.label).toBe('无证据');
    expect(style.dot).toContain('amber-700');
    expect(style.dot).toContain('dashed');
    // 没验成 ≠ 通过：无证据绝不能与 ok 同色
    expect(style.dot).not.toContain('emerald');
    expect(style.dot).not.toBe(statusStyle('unknown').dot);
  });

  it('健康状态色语义：绿=正常 / 黄=警告 / 红=异常，且正常与异常不同色', () => {
    // 回归：ok 与 fail 曾同为红（red-500 / rose-600），肉眼分不出正常与异常
    expect(statusStyle('ok').dot).toContain('emerald');
    expect(statusStyle('warn').dot).toContain('amber-500');
    expect(statusStyle('fail').dot).toContain('red-600');
    expect(statusStyle('ok').dot).not.toBe(statusStyle('fail').dot);
    expect(statusStyle('warn').dot).not.toBe(statusStyle('no_evidence').dot);
  });

  it('每档都带 bar（tile 左侧色条），与 dot 同源不另抄色表', () => {
    for (const level of ['ok', 'warn', 'fail', 'unknown', 'no_evidence']) {
      expect(statusStyle(level).bar).toContain('bg-');
    }
  });

  it('下钻条目：环头三行 + 逐证据项（detail 与建议进 hint、来源保留）', () => {
    const entries = evidenceRingDrillEntries(
      ring({
        items: [
          { id: 'C05', name: '台账写入', level: 'fail', detail: '1 笔无台账', suggestion: '查 ledger', source: 'health:C05' },
        ],
      })
    );
    expect(entries[0]).toMatchObject({ label: '环节', value: '数据' });
    expect(entries[1]).toMatchObject({ label: '状态', value: '正常' });
    expect(entries[2].value).toContain('数据质检报告');
    const item = entries[3];
    expect(item.label).toContain('台账写入');
    expect(item.value).toBe('异常');
    expect(item.hint).toContain('1 笔无台账');
    expect(item.hint).toContain('建议：查 ledger');
    expect(item.source).toBe('health:C05');
    expect(evidenceRingDrillEntries(null)).toEqual([]);
  });
});

describe('人工改量（T-FE-05 v2）', () => {
  const mkOrder = (over: Partial<PlanOrder>): PlanOrder => ({
    symbol: '600036.SH',
    side: 'BUY',
    quantity: 1000,
    price: 40,
    estimated_amount: 40000,
    reason: '调仓',
    kind: 'rebalance',
    is_limit_up: false,
    is_limit_down: false,
    is_suspended: false,
    ...over,
  });

  it('载荷只收录「与计划不同且合法」的非退出单', () => {
    const plan = {
      available: true,
      source: 's',
      orders: [
        mkOrder({ symbol: '600036.SH' }),
        mkOrder({ symbol: '000001.SZ', quantity: 500 }),
        mkOrder({ symbol: '600519.SH', side: 'SELL', quantity: 300, kind: 'exit' }),
      ],
    } as PlanBlock;
    const edits = new Map<number, number>([
      [0, 1200], // 改量
      [1, 500], // 与计划相同 → 不收
      [2, 100], // 退出单 → 恒不收（风控不绕过）
    ]);
    expect(quantityOverridesFromPlan(plan, edits)).toEqual([
      { symbol: '600036.SH', side: 'BUY', quantity: 1200 },
    ]);
  });

  it('非法输入（NaN/0/负数/小数）不入载荷，并被门禁识别', () => {
    const plan = { available: true, source: 's', orders: [mkOrder({})] } as PlanBlock;
    expect(quantityOverridesFromPlan(plan, new Map([[0, 0]]))).toEqual([]);
    expect(quantityOverridesFromPlan(plan, new Map([[0, -5]]))).toEqual([]);
    expect(quantityOverridesFromPlan(plan, new Map([[0, Number.NaN]]))).toEqual([]);
    expect(quantityOverridesFromPlan(plan, new Map([[0, 12.7]]))).toEqual([]);
    expect(hasInvalidQuantityEdit(plan, new Map([[0, 0]]))).toBe(true);
    expect(hasInvalidQuantityEdit(plan, new Map([[0, 1500]]))).toBe(false);
    expect(hasInvalidQuantityEdit(plan, new Map())).toBe(false);
  });

  it('退出单即使被改也不触发门禁（不可改但不算非法输入）', () => {
    const plan = {
      available: true,
      source: 's',
      orders: [mkOrder({ kind: 'exit', side: 'SELL' })],
    } as PlanBlock;
    expect(hasInvalidQuantityEdit(plan, new Map([[0, 0]]))).toBe(false);
  });

  it('整数值浮点（input 常见形态）视为合法并向下取整', () => {
    const plan = { available: true, source: 's', orders: [mkOrder({})] } as PlanBlock;
    expect(quantityOverridesFromPlan(plan, new Map([[0, 1200.0]]))).toEqual([
      { symbol: '600036.SH', side: 'BUY', quantity: 1200 },
    ]);
  });

  it('执行报告裁定 → 人话摘要（applied/ignored 含原因）', () => {
    const summary = quantityAdjustmentSummary({
      quantity_adjustments: [
        { symbol: '600036.SH', side: 'BUY', from: 1000, to: 1200, applied: true },
        { symbol: '600519.SH', side: 'SELL', requested: 100, applied: null, reason: '退出规则单不可改量（风控动作不绕过）' },
      ],
    });
    expect(summary.applied).toEqual(['600036.SH 买 1000 → 1200']);
    expect(summary.ignored[0]).toContain('退出规则单不可改量');
    expect(quantityAdjustmentSummary(null)).toEqual({ applied: [], ignored: [] });
    expect(quantityAdjustmentSummary({})).toEqual({ applied: [], ignored: [] });
  });
});

describe('逐层穿透下钻（T-FE-03 v2）', () => {
  const signal = { symbol: '600036', side: 'BUY', rank_pct: 0.982, score: 0.0123 };
  const signalsBlock = {
    trade_date: '2026-09-16',
    buy: 1040,
    sell: 30,
    hold: 4000,
    top_buy: [signal],
    source: 'db:engine_signal_scores',
  };

  it('信号条目：字段分解 + 两层可穿透（原始条目/信号块载荷）', () => {
    const entries = signalItemDrillEntries(signal, signalsBlock);
    expect(entries[0]).toMatchObject({ label: '标的', value: '600036' });
    expect(entries[2].value).toBe('0.982');
    const rawEntry = entries.find((e) => e.label.includes('原始条目'));
    expect(rawEntry?.drill?.entries.some((x) => x.label === 'rank_pct')).toBe(true);
    expect(rawEntry?.drill?.raw).toBe(signal);
    const blockEntry = entries.find((e) => e.label === '当日全体分布');
    expect(blockEntry?.drill?.raw).toBe(signalsBlock);
  });

  it('执行条目：取价来源挂解释层（broker_fill/降级标记如实）', () => {
    const entries = executionItemDrillEntries(
      { mode: 'SIM', symbol: '600036', side: 'BUY', quantity: 1200, status: 'FILLED', price_source: 'prev_close_bar', client_order_id: 'sim-x-1' },
      { source: 'db:sim_orders' }
    );
    const ps = entries.find((e) => e.label === '取价来源');
    expect(ps?.value).toBe('prev_close_bar');
    expect(ps?.hint).toContain('降级');
    expect(ps?.drill?.entries.some((x) => x.label === '降级规则')).toBe(true);
    expect(executionItemDrillEntries({ mode: 'SIM', symbol: 'X', side: 'BUY', quantity: 1, status: 'PENDING' }, null)[4].hint).toContain('未标注');
  });

  it('计划单：退出单与调仓单的触发类别说明不同；命中候选信号时可继续下钻', () => {
    const rebalance = order({ symbol: '600036', kind: 'rebalance' });
    const exit = order({ symbol: '600519.SH', kind: 'exit', side: 'SELL' });
    const plan = { available: true, source: 'engine dry-run', orders: [rebalance, exit] } as PlanBlock;

    const rebEntries = planOrderDrillEntries(rebalance, plan, signalsBlock);
    const rebKind = rebEntries.find((e) => e.label === '触发类别');
    expect(rebKind?.value).toBe('定期调仓');
    expect(rebKind?.drill?.entries.some((x) => String(x.value).includes('可勾选排除'))).toBe(true);
    // 600036 在候选信号内 → 挂信号层，且信号层内部还能再穿一层
    const sigEntry = rebEntries.find((e) => e.label === '对应当日信号');
    expect(sigEntry?.drill?.raw).toBe(signal);
    expect(sigEntry?.drill?.entries.some((x) => x.drill)).toBe(true);

    const exitEntries = planOrderDrillEntries(exit, plan, signalsBlock);
    const exitKind = exitEntries.find((e) => e.label === '触发类别');
    expect(exitKind?.value).toBe('退出规则（风控）');
    expect(exitKind?.drill?.entries.some((x) => String(x.value).includes('不可排除、不可改量'))).toBe(true);
    expect(exitEntries.find((e) => e.label === '对应当日信号')).toBeUndefined();
  });

  it('管线步骤：按体检断言 ID 关联证据环并挂下一层；无环时不挂', () => {
    const step = { key: 'settlement', label: '结算/台账', status: 'fail', detail: '1 笔无台账', source: 'health:C05' };
    const evidence = {
      rings: [
        {
          key: 'ledger',
          label: '账本',
          artifact: '对账报告',
          frequency: '每日',
          level: 'fail',
          summary: '缺口',
          items: [{ id: 'C05', name: '台账写入', level: 'fail', detail: '1 笔无台账', source: 'health:C05' }],
        },
      ],
    };
    const entries = pipelineStepDrillEntries(step, evidence);
    expect(entries[1]).toMatchObject({ label: '状态', value: '异常' });
    const ringEntry = entries.find((e) => e.label === '对应证据环');
    expect(ringEntry?.drill?.raw).toBe(evidence.rings[0]);
    expect(ringEntry?.drill?.entries.some((x) => String(x.label).includes('台账写入'))).toBe(true);

    const noRing = pipelineStepDrillEntries(step, { rings: [] });
    expect(noRing.find((e) => e.label === '对应证据环')).toBeUndefined();
  });
});
