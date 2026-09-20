import { describe, expect, it } from 'vitest';
import type { EvalSeriesData } from '../../types/evalCenter';
import {
  buildSeriesCharts,
  seriesAnswer,
  seriesToOption,
  SERIES_QUESTIONS,
} from '../evalSeriesModel';

/** 因子侧车样例（`/eval/series` 的 data 部分） */
function factorPayload(): EvalSeriesData {
  return {
    series: {
      daily_ic: [
        { date: '2026-09-14', value: 0.02 },
        { date: '2026-09-15', value: -0.01 },
        { date: '2026-09-16', value: 0.05 },
      ],
      decile_mean: [
        { bucket: 1, value: -0.004 },
        { bucket: 2, value: -0.002 },
        { bucket: 10, value: 0.012 },
      ],
      ic_decay: [
        { horizon: 1, value: 0.1 },
        { horizon: 5, value: 0.06 },
        { horizon: 20, value: 0.02 },
      ],
      segment_ic: [
        { label: '2025', value: 0.01, n_days: 240, is_min_segment: true },
        { label: '2026', value: 0.03, n_days: 120, is_min_segment: false },
      ],
      correlation: [
        { name: 'gtja_042', value: 0.87 },
        { name: 'a158_LOW0', value: 0.95 },
      ],
    },
    scalars: { ic_mean: 0.02, ic_ir: 0.4 },
    notes: {},
  };
}

function modelPayload(): EvalSeriesData {
  return {
    series: {
      daily_ic: [
        { date: '2026-09-15', value: 0.03 },
        { date: '2026-09-16', value: 0.01 },
      ],
      decile_mean: [{ bucket: 1, value: -0.001 }],
      ic_decay: [],
      turnover: [
        { pair: 1, value: 0.2 },
        { pair: 2, value: 0.4 },
      ],
    },
    scalars: { ic_mean_20: 0.021 },
    notes: { ic_decay: 'IC 衰减按持有期需要多周期标签，而 pred.parquet 只有单一持有期的 label —— 不可算，不给平线占位' },
  };
}

describe('buildSeriesCharts 一图一问', () => {
  it('因子卡五问：IC 线 / 十分位 / 衰减 / 分年 / 相关性，顺序固定', () => {
    const charts = buildSeriesCharts('factor', factorPayload());

    expect(charts.map((c) => c.kind)).toEqual([
      'ic_line',
      'decile_bar',
      'decay_bar',
      'segment_bar',
      'corr_bar',
    ]);
    expect(charts.every((c) => c.option !== null)).toBe(true);
    expect(charts.every((c) => c.question.endsWith('？'))).toBe(true);
  });

  it('模型卡第四问换成换手（模型有 turnover 序列，因子没有）', () => {
    const charts = buildSeriesCharts('model', modelPayload());

    expect(charts.map((c) => c.kind)).toEqual([
      'ic_line',
      'decile_bar',
      'decay_bar',
      'segment_bar',
      'turnover_line',
    ]);
  });

  it('序列缺省时不留白图：option=null 且 answer 是后端给的原因原文', () => {
    const charts = buildSeriesCharts('model', modelPayload());
    const decay = charts.find((c) => c.kind === 'decay_bar')!;

    expect(decay.option).toBeNull();
    expect(decay.answer).toContain('不可算');
    expect(decay.answer).toContain('pred.parquet');
  });

  it('既没序列也没说明时，answer 如实说「后端未产出」，不给空串', () => {
    const empty: EvalSeriesData = { series: { turnover: [] }, notes: {} };

    const chart = buildSeriesCharts('model', empty).find((c) => c.kind === 'turnover_line')!;

    expect(chart.option).toBeNull();
    expect(chart.answer).toContain('未产出');
  });

  it('载荷为空 → 无图可画（由调用方渲染 meta.note）', () => {
    expect(buildSeriesCharts('factor', null)).toEqual([]);
    expect(buildSeriesCharts('factor', { series: {}, notes: {} })).toEqual([]);
  });
});

describe('seriesToOption 图形口径', () => {
  it('十分位柱：高档（做多侧）红、低档绿 —— A 股红涨绿跌', () => {
    const option = seriesToOption('decile_bar', factorPayload()) as any;
    const data = option.series[0].data;

    expect(data[0].itemStyle.color).toBe('#059669');
    expect(data[2].itemStyle.color).toBe('#dc2626');
    expect(option.xAxis.data).toEqual(['D1', 'D2', 'D10']);
  });

  it('逐日 IC 线带 0 轴虚线（「忽正忽负」看得见）', () => {
    const option = seriesToOption('ic_line', factorPayload()) as any;

    expect(option.series[0].type).toBe('line');
    expect(option.series[0].data).toEqual([0.02, -0.01, 0.05]);
    expect(option.series[0].markLine.data).toEqual([{ yAxis: 0 }]);
    expect(option.xAxis.data[0]).toBe('2026-09-14');
  });

  it('分年柱：正红负绿，最低段加琥珀描边（不靠颜色深浅猜）', () => {
    const option = seriesToOption('segment_bar', factorPayload()) as any;
    const data = option.series[0].data;

    expect(data[0].itemStyle.borderWidth).toBe(1.5);
    expect(data[1].itemStyle.borderWidth).toBe(0);
    expect(data[1].itemStyle.color).toBe('#dc2626');
  });

  it('相关性横条：≥0.9 用风险色（红线去重），其余中性', () => {
    const option = seriesToOption('corr_bar', factorPayload()) as any;
    const data = option.series[0].data as Array<{ value: number; itemStyle: { color: string } }>;

    // 纵轴倒序 → 最相关那条显示在最上面
    expect(option.yAxis.data).toEqual(['a158_LOW0', 'gtja_042']);
    expect(data[0].itemStyle.color).toBe('#d97706');
    expect(data[1].itemStyle.color).toBe('#64748b');
  });

  it('换手线：横轴是配对序号', () => {
    const option = seriesToOption('turnover_line', modelPayload()) as any;

    expect(option.xAxis.data).toEqual(['#1', '#2']);
    expect(option.series[0].data).toEqual([0.2, 0.4]);
  });

  it('序列为空 → null（不给一张空图当证据）', () => {
    expect(seriesToOption('corr_bar', factorPayload())).not.toBeNull();
    expect(seriesToOption('decay_bar', modelPayload())).toBeNull();
    expect(seriesToOption('ic_line', null)).toBeNull();
  });
});

describe('seriesAnswer 每问一句话（数字全部来自所画那条序列）', () => {
  it('IC 线：均值 + 正向占比', () => {
    expect(seriesAnswer('ic_line', factorPayload())).toBe(
      '近 3 个交易日 IC 均值 0.0200，正向占比 66.7%'
    );
  });

  it('十分位：首末档 + 递增步数', () => {
    expect(seriesAnswer('decile_bar', factorPayload())).toBe(
      '第 1 档 -0.0040 → 第 10 档 0.0120，递增 2/2 步'
    );
  });

  it('衰减：|IC| 从短持有期到长持有期保留比例', () => {
    expect(seriesAnswer('decay_bar', factorPayload())).toBe(
      '|IC| 从 1 日 0.1000 到 20 日 0.0200（保留 20.0%）'
    );
  });

  it('单点序列不算结论，只报点数', () => {
    const one: EvalSeriesData = { series: { ic_decay: [{ horizon: 1, value: 0.1 }] }, notes: {} };

    expect(seriesAnswer('decay_bar', one)).toBe('只有 1 个视界，看不出衰减');
  });

  it('分年：段数与最低段', () => {
    expect(seriesAnswer('segment_bar', factorPayload())).toBe('共 2 段，最低 2025（0.0100）');
  });

  it('相关性：最高相关那一条点名', () => {
    expect(seriesAnswer('corr_bar', factorPayload())).toBe('最高相关 a158_LOW0 0.9500');
  });

  it('换手：均值换手率', () => {
    expect(seriesAnswer('turnover_line', modelPayload())).toBe('均值换手 30.0%（共 2 期）');
  });
});

describe('SERIES_QUESTIONS 问题表', () => {
  it('每类都有问句与缺省兜底说明', () => {
    for (const kind of Object.keys(SERIES_QUESTIONS)) {
      expect(SERIES_QUESTIONS[kind as keyof typeof SERIES_QUESTIONS].trim().length).toBeGreaterThan(4);
    }
  });
});

// ── 策略 / 账户 / 每日选股：长序列侧车（阶段 3）────────────────────────

/** 策略侧车样例：净值 + 回撤 + 月度收益（`strategy_series_payload` 产物） */
function strategyPayload(): EvalSeriesData {
  return {
    series: {
      equity: [
        { date: '2026-01-02', value: 1000000 },
        { date: '2026-01-05', value: 1010000 },
        { date: '2026-03-02', value: 1040000 },
      ],
      drawdown: [
        { date: '2026-01-02', value: 0 },
        { date: '2026-01-05', value: -0.02 },
        { date: '2026-03-02', value: -0.0031 },
      ],
      monthly_return: [
        { label: '2026-01', value: 0.01 },
        { label: '2026-02', value: -0.02 },
        { label: '2026-03', value: 0.04 },
      ],
    },
    scalars: { score: 82, window_return: 0.04 },
    notes: { equity: '', drawdown: '', monthly_return: '' },
  };
}

/** 账户侧车样例：只有 5 天快照 —— 曲线照画，样本量必须一起摊开 */
function accountPayload(): EvalSeriesData {
  return {
    series: {
      equity: [
        { date: '2026-09-11', value: 1000000 },
        { date: '2026-09-12', value: 1001200 },
        { date: '2026-09-13', value: 1000500 },
        { date: '2026-09-14', value: 1002000 },
        { date: '2026-09-15', value: 1001800 },
      ],
      daily_pnl: [
        { date: '2026-09-11', value: 1200 },
        { date: '2026-09-12', value: -400 },
        { date: '2026-09-13', value: 300 },
        { date: '2026-09-14', value: 0 },
        { date: '2026-09-15', value: 200 },
      ],
    },
    scalars: { sample_days: 5, sample_sufficient: false },
    notes: {
      equity: '模拟盘净值快照仅 5 个交易日（2026-09-11 → 2026-09-15）——点数少，只能当抽样看，别读成趋势',
      daily_pnl: '',
    },
  };
}

/** 每日选股侧车样例：入选数 / 事后超额 / 命中率 */
function dailySelectionPayload(): EvalSeriesData {
  return {
    series: {
      picked: [
        { date: '2026-09-14', value: 4 },
        { date: '2026-09-15', value: 0 },
        { date: '2026-09-16', value: 2 },
      ],
      realized_excess: [
        { date: '2026-09-14', value: 0.012 },
        { date: '2026-09-15', value: -0.004 },
      ],
      hit_rate: [
        { date: '2026-09-14', value: 0.75 },
        { date: '2026-09-15', value: 0.25 },
      ],
    },
    scalars: { horizon: 5, pending_backfill: false },
    notes: {
      picked: '',
      realized_excess: '1 个交易日的 T+5 前向数据未齐（待回填）；这些天不在曲线上（不假填 0）',
      hit_rate: '',
    },
  };
}

describe('buildSeriesCharts 三类新对象的图位与序列键', () => {
  it('策略三问：净值 / 回撤 / 月度收益（同一图形挂不同序列由 slot 指定）', () => {
    const charts = buildSeriesCharts('strategy', strategyPayload());

    expect(charts.map((c) => c.kind)).toEqual(['equity_line', 'drawdown_area', 'signed_bar']);
    expect(charts.map((c) => c.key)).toEqual(['equity', 'drawdown', 'monthly_return']);
    expect(charts.every((c) => c.question.endsWith('？'))).toBe(true);
    expect(charts.every((c) => c.option !== null)).toBe(true);
  });

  it('账户两问：净值 / 当日盈亏（signed_bar 的键被槽位覆盖成 daily_pnl）', () => {
    const charts = buildSeriesCharts('account', accountPayload());

    expect(charts.map((c) => c.kind)).toEqual(['equity_line', 'signed_bar']);
    expect(charts.map((c) => c.key)).toEqual(['equity', 'daily_pnl']);
    expect(charts.every((c) => c.option !== null)).toBe(true);
  });

  it('每日选股三问：入选数 / 事后超额 / 命中率', () => {
    const charts = buildSeriesCharts('daily_selection', dailySelectionPayload());

    expect(charts.map((c) => c.kind)).toEqual(['count_bar', 'signed_bar', 'rate_line']);
    expect(charts.map((c) => c.key)).toEqual(['picked', 'realized_excess', 'hit_rate']);
    expect(charts.every((c) => c.option !== null)).toBe(true);
  });

  it('有数据的图也要带后端 note（账户「样本 5 天」必须显示出来）', () => {
    const charts = buildSeriesCharts('account', accountPayload());
    const equity = charts.find((c) => c.kind === 'equity_line')!;

    expect(equity.option).not.toBeNull();
    expect(equity.note).toContain('5 个交易日');
    expect(equity.note).toContain('别读成趋势');
  });

  it('没数据的图不重复贴 note（原因已经在 answer 里）', () => {
    const charts = buildSeriesCharts('daily_selection', {
      series: { picked: [{ date: '2026-09-14', value: 4 }] },
      notes: { realized_excess: '前向未齐，待回填' },
    });
    const excess = charts.find((c) => c.kind === 'signed_bar')!;

    expect(excess.option).toBeNull();
    expect(excess.note).toBe('');
    expect(excess.answer).toContain('前向未齐');
  });
});

describe('新图形的口径（A 股红涨绿跌、缺测不进图）', () => {
  it('净值线：涨红跌绿，横轴是日期', () => {
    const up = seriesToOption('equity_line', strategyPayload(), 'equity') as any;

    expect(up.series[0].data).toEqual([1000000, 1010000, 1040000]);
    expect(up.series[0].lineStyle.color).toBe('#dc2626');
    expect(up.xAxis.data).toEqual(['2026-01-02', '2026-01-05', '2026-03-02']);

    const falling: EvalSeriesData = {
      series: { equity: [{ date: 'a', value: 1000 }, { date: 'b', value: 900 }] },
      notes: {},
    };
    const down = seriesToOption('equity_line', falling, 'equity') as any;

    expect(down.series[0].lineStyle.color).toBe('#059669');
  });

  it('净值线：点少（账户 5 天）时必须显点，不把抽样画成趋势线', () => {
    const option = seriesToOption('equity_line', accountPayload(), 'equity') as any;

    expect(option.series[0].showSymbol).toBe(true);
  });

  it('净值金额轴与提示都按金额口径格式化', () => {
    const option = seriesToOption('equity_line', strategyPayload(), 'equity') as any;

    expect(option.yAxis.axisLabel.formatter(1040000)).toBe('104.0万');
    expect(option.tooltip.valueFormatter(1040000, 0)).toBe('1040000.00');
  });

  it('回撤面积：绿（亏向）负值 + 0 轴虚线', () => {
    const option = seriesToOption('drawdown_area', strategyPayload(), 'drawdown') as any;

    expect(option.series[0].type).toBe('line');
    expect(option.series[0].data).toEqual([0, -0.02, -0.0031]);
    expect(option.series[0].lineStyle.color).toBe('#059669');
    expect(option.series[0].areaStyle.color).toBe('#059669');
    expect(option.series[0].markLine.data).toEqual([{ yAxis: 0 }]);
  });

  it('月度收益柱：横轴是月份，正红负绿', () => {
    const option = seriesToOption('signed_bar', strategyPayload(), 'monthly_return') as any;

    expect(option.xAxis.data).toEqual(['2026-01', '2026-02', '2026-03']);
    expect(option.series[0].data.map((d: any) => d.itemStyle.color)).toEqual([
      '#dc2626',
      '#059669',
      '#dc2626',
    ]);
  });

  it('signed_bar 不带键时用默认键（账户 payload 没有 monthly_return → 无图）', () => {
    expect(seriesToOption('signed_bar', accountPayload())).toBeNull();
    expect(seriesToOption('signed_bar', accountPayload(), 'daily_pnl')).not.toBeNull();
  });

  it('当日盈亏柱：横轴是日期，提示按金额（不是百分数）', () => {
    const option = seriesToOption('signed_bar', accountPayload(), 'daily_pnl') as any;

    expect(option.xAxis.data[0]).toBe('2026-09-11');
    expect(option.tooltip.valueFormatter(-400, 1)).toBe('-400.00');
  });

  it('入选数柱：中性色（计数没有正负），提示是整数', () => {
    const option = seriesToOption('count_bar', dailySelectionPayload(), 'picked') as any;

    expect(option.series[0].data.map((d: any) => d.value)).toEqual([4, 0, 2]);
    expect(option.series[0].data[0].itemStyle.color).toBe('#64748b');
    expect(option.tooltip.valueFormatter(3, 0)).toBe('3');
  });

  it('命中率线：0.5 抛硬币基准线 + 百分数刻度', () => {
    const option = seriesToOption('rate_line', dailySelectionPayload(), 'hit_rate') as any;

    expect(option.series[0].data).toEqual([0.75, 0.25]);
    expect(option.series[0].markLine.data[0].yAxis).toBe(0.5);
    expect(option.yAxis.axisLabel.formatter(0.5)).toBe('50%');
  });

  it('百分数刻度分得出高低：0 点附近的小范围不能整轴都印成「0%」', () => {
    // 实测：事后超额只有一天（-0.35%）时，固定 0 位小数让 7 个刻度全长一个样
    const option = seriesToOption('signed_bar', dailySelectionPayload(), 'realized_excess') as any;
    const fmt = option.yAxis.axisLabel.formatter;
    const ticks = [-0.004, -0.003, -0.002, -0.001, 0];

    expect(ticks.map(fmt)).toEqual(['-0.40%', '-0.30%', '-0.20%', '-0.10%', '0.00%']);
    expect(new Set(ticks.map(fmt)).size).toBe(ticks.length);
  });

  it('value 为 null 的点不进图、也不算 0（缺测不与 0 同形）', () => {
    const withNull: EvalSeriesData = {
      series: {
        realized_excess: [
          { date: '2026-09-14', value: 0.012 },
          { date: '2026-09-15', value: null as unknown as number },
        ],
      },
      notes: {},
    };
    const option = seriesToOption('signed_bar', withNull, 'realized_excess') as any;

    expect(option.series[0].data.map((d: any) => d.value)).toEqual([0.012]);
    expect(seriesAnswer('signed_bar', withNull, 'realized_excess')).toBe(
      '只有 1 期 T+H 超额，看不出正负节奏'
    );
  });
});

describe('新图形的一句话结论（数字全部来自所画那条序列）', () => {
  it('净值：首末值与区间涨跌', () => {
    expect(seriesAnswer('equity_line', strategyPayload(), 'equity')).toBe(
      '窗口净值 1000000 → 1040000（3 点），区间 +4.00%'
    );
  });

  it('回撤：图上最深点与无回撤天数', () => {
    expect(seriesAnswer('drawdown_area', strategyPayload(), 'drawdown')).toBe(
      '图上最深回撤 -2.00%（2026-01-05）；1/3 个交易日无回撤'
    );
  });

  it('月度收益：正期占比与均值', () => {
    expect(seriesAnswer('signed_bar', strategyPayload(), 'monthly_return')).toBe(
      '3 期里 2 期为正（66.7%），均值 +1.00%'
    );
  });

  it('当日盈亏：金额口径（不加百分号）', () => {
    expect(seriesAnswer('signed_bar', accountPayload(), 'daily_pnl')).toBe(
      '5 期里 3 期为正（60.0%），均值 260.00'
    );
  });

  it('入选数：日均与空仓天数', () => {
    expect(seriesAnswer('count_bar', dailySelectionPayload(), 'picked')).toBe(
      '3 天日均 2.0 只（最少 0、最多 4，1 天空仓）'
    );
  });

  it('命中率：均值与在抛硬币线之上的天数', () => {
    expect(seriesAnswer('rate_line', dailySelectionPayload(), 'hit_rate')).toBe(
      '2 天命中率均值 50.0%（1 天在 50% 抛硬币线之上）'
    );
  });

  it('单点序列不算结论，只报点数', () => {
    const one: EvalSeriesData = {
      series: { equity: [{ date: '2026-09-15', value: 1000000 }] },
      notes: {},
    };
    const onePicked: EvalSeriesData = {
      series: { picked: [{ date: '2026-09-15', value: 2 }] },
      notes: {},
    };

    expect(seriesAnswer('equity_line', one, 'equity')).toBe('只有 1 个净值点，画不出曲线');
    expect(seriesAnswer('count_bar', onePicked, 'picked')).toBe(
      '只有 1 天有入选数（2 只），看不出节奏'
    );
  });

  it('序列为空 → 结论是后端 notes 原文，不是自己编的说法', () => {
    const only: EvalSeriesData = {
      series: { hit_rate: [] },
      notes: { hit_rate: '还没有回填完成的日子' },
    };

    expect(seriesAnswer('rate_line', only, 'hit_rate')).toBe('还没有回填完成的日子');
  });
});
