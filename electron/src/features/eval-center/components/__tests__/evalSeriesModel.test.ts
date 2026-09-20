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
