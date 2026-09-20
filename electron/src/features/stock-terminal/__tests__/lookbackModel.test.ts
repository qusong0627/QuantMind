/**
 * 信号准确率回看 · 纯函数测试。
 *
 * 重点盯两类错误（都会「看起来正常」地骗人）：
 * 1. 把 null 显示成 0 或 0.00% —— 等于宣称「没涨没跌」；
 * 2. 三个回看点缺一个时，把它的值顺延到别的档位充数。
 */
import { describe, expect, it } from 'vitest';
import type { LookbackDetailItem, LookbackPoint, LookbackSummaryRow } from '../lookbackModel';
import {
  formatRank,
  formatScore,
  isVerdictUsable,
  lookbackLabel,
  orderPoints,
  priceSourceLabel,
  toPct,
  usableSummaryRows,
} from '../lookbackModel';

const pt = (lookback: number, ret: number | null = 0.01, score: number | null = 0.1): LookbackPoint => ({
  lookback,
  signal_date: '2026-09-07',
  score,
  rank: 1,
  rank_pct: 0.99,
  day_n: 3271,
  ret,
  price_source: 'close',
});

const row = (spread: number | null): LookbackSummaryRow => ({
  lookback: 3,
  label: 'T-3',
  signal_date: '2026-09-16',
  base_price_date: '2026-09-16',
  run_id: 'r1',
  model_version: 'v1',
  comparable: true,
  sample: 3271,
  n_hi: 655,
  n_lo: 655,
  n_neg: 943,
  missing_price: 0,
  score_std: 0.1466,
  hi_avg: 0.0123,
  lo_avg: 0.0114,
  spread,
  hi_hit: 0.647,
  lo_hit: 0.304,
  neg_avg: 0.0119,
  avg_score_hi: 0.258,
  avg_score_lo: -0.148,
});

describe('lookbackLabel', () => {
  it('回看点数字转成 T-N 标签', () => {
    expect(lookbackLabel(3)).toBe('T-3');
    expect(lookbackLabel(10)).toBe('T-10');
  });

  it('非法输入回退成空串而不是 NaN', () => {
    expect(lookbackLabel(Number.NaN)).toBe('');
  });
});

describe('formatScore —— 缺失绝不成 0', () => {
  it('null / undefined / NaN 一律破折号', () => {
    expect(formatScore(null)).toBe('—');
    expect(formatScore(undefined)).toBe('—');
    expect(formatScore(Number.NaN)).toBe('—');
  });

  it('0 分是真实数值，不是缺失', () => {
    expect(formatScore(0)).toBe('0.000');
  });

  it('负分保留符号（用户要看「分数﹣的跌了多少」）', () => {
    expect(formatScore(-0.34138983)).toBe('-0.341');
  });

  it('三位小数与左侧候选列表口径一致', () => {
    expect(formatScore(0.9377669)).toBe('0.938');
  });
});

describe('formatRank', () => {
  it('带当日总样本，否则名次没有意义', () => {
    expect(formatRank(1127, 3271)).toBe('#1127 / 3271');
  });

  it('名次缺失给破折号，不显示 #0 或 #null', () => {
    expect(formatRank(null, 3271)).toBe('—');
    expect(formatRank(undefined, 3271)).toBe('—');
  });

  it('总数缺失时只给名次，不编分母', () => {
    expect(formatRank(1127, null)).toBe('#1127');
  });
});

describe('toPct —— 交给 PctText 的必须是百分数', () => {
  it('小数转百分数', () => {
    expect(toPct(0.0112)).toBeCloseTo(1.12, 6);
    expect(toPct(-0.078125)).toBeCloseTo(-7.8125, 6);
  });

  it('缺失保持 null，让 PctText 显示 -- 而不是 0.00%', () => {
    expect(toPct(null)).toBeNull();
    expect(toPct(undefined)).toBeNull();
    expect(toPct(Number.NaN)).toBeNull();
  });

  it('0 收益是真实的 0，不是缺失', () => {
    expect(toPct(0)).toBe(0);
  });
});

describe('orderPoints —— 缺档就是缺档，不顺延', () => {
  const points: LookbackPoint[] = [pt(3), pt(10)]; // 缺 T-5

  it('按请求顺序（T-10 → T-3）排列', () => {
    expect(orderPoints(points, [3, 5, 10]).map((p) => p.lookback)).toEqual([10, 3]);
  });

  it('缺的那档不出现，绝不拿别的档顶替', () => {
    const out = orderPoints(points, [3, 5, 10]);
    expect(out.some((p) => p.lookback === 5)).toBe(false);
    expect(out).toHaveLength(2);
  });

  it('后端多给了没请求的档位时按请求列表裁剪', () => {
    expect(orderPoints([pt(1), pt(3), pt(10)], [3, 10]).map((p) => p.lookback)).toEqual([10, 3]);
  });

  it('空输入不抛错', () => {
    expect(orderPoints([], [3, 5, 10])).toEqual([]);
    expect(orderPoints(points, [])).toEqual([]);
  });
});

describe('isVerdictUsable —— 零项参与不算通过', () => {
  it('完全没有汇总行 → 不可下结论', () => {
    expect(isVerdictUsable([])).toBe(false);
  });

  it('所有行都是缺价（spread=null）→ 不可下结论，不能显示成「无区分度」', () => {
    expect(isVerdictUsable([row(null), row(null)])).toBe(false);
  });

  it('只要有一行算出价差就能下结论', () => {
    expect(isVerdictUsable([row(null), row(0.0068)])).toBe(true);
  });

  it('价差恰好为 0 是真实结论（「模型没有区分度」），不是缺数据', () => {
    expect(isVerdictUsable([row(0)])).toBe(true);
  });

  it('usableSummaryRows 只留算得出价差的行', () => {
    expect(usableSummaryRows([row(null), row(0.0068)]).map((r) => r.spread)).toEqual([0.0068]);
  });
});

describe('priceSourceLabel —— 表头必须说清现价来自哪里', () => {
  it('全部实时', () => {
    expect(priceSourceLabel('live', 3271, 3271)).toBe('实时 3271 只');
  });

  it('全部收盘', () => {
    expect(priceSourceLabel('close', 0, 3271)).toBe('收盘价 · 3271 只');
  });

  it('混合时两个数都要给出来', () => {
    expect(priceSourceLabel('mixed', 12, 3271)).toBe('实时 12 / 收盘 3259 只');
  });

  it('未知来源不硬编', () => {
    expect(priceSourceLabel(undefined, 0, 0)).toBe('');
  });
});

describe('detail item 结构对齐', () => {
  it('points 顺序与 summary 一致（同一次响应用同一套 lookbacks）', () => {
    const item: LookbackDetailItem = {
      symbol: '600606.SH',
      name: '绿地控股',
      score_now: 0.938,
      rank_now: 1,
      side_now: 'BUY',
      points: [pt(3), pt(10)],
    };
    expect(orderPoints(item.points, [3, 5, 10]).map((p) => p.lookback)).toEqual([10, 3]);
  });
});
