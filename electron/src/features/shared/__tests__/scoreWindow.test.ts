/**
 * 分数 ↔ K 线对齐金样（前端唯一实现）。
 *
 * 后端 `backend/shared/score_window.py` 已算好每个点的 target_date；
 * 前端只负责按模式落点，绝不按自然日推算。这些断言覆盖 T+1/T+5/T+20 的
 * 窗口形状、兑现/预测落点、缺失目标日的丢弃与同日去重。
 */

import { describe, it, expect } from 'vitest';
import {
  buildScoreAlignment,
  extendTimeline,
  futureDatesAfter,
  futureDatesForPeriod,
  impliedTargetPrice,
  isWindowPending,
  isWindowResolved,
  latestScore,
  type ScorePointLike,
} from '../scoreWindow';

const T20_POINTS: ScorePointLike[] = [
  { trade_date: '2026-11-10', target_date: '2026-12-09', fusion_score: 0.11 },
  { trade_date: '2026-11-11', target_date: '2026-12-10', fusion_score: 0.22 },
  { trade_date: '2026-11-12', target_date: '2026-12-11', fusion_score: 0.33 },
];

describe('buildScoreAlignment：预测日 / 兑现日', () => {
  it('预测日模式：分数落在产生日 D', () => {
    const out = buildScoreAlignment(T20_POINTS, 'prediction');
    expect(out.map((p) => p.date)).toEqual(['2026-11-10', '2026-11-11', '2026-11-12']);
    expect(out.map((p) => p.value)).toEqual([0.11, 0.22, 0.33]);
    expect(out[2].scoreDate).toBe('2026-11-12');
  });

  it('兑现日模式：分数落在后端给的 T+20 目标日', () => {
    const out = buildScoreAlignment(T20_POINTS, 'resolution');
    expect(out.map((p) => p.date)).toEqual(['2026-12-09', '2026-12-10', '2026-12-11']);
    expect(out[0].scoreDate).toBe('2026-11-10');
  });

  it('兑现日模式：目标日缺失（日历越界/非交易日）直接丢弃，不猜日期', () => {
    const out = buildScoreAlignment(
      [
        { trade_date: '2026-12-20', target_date: null, fusion_score: 0.5 },
        { trade_date: '2026-12-21', fusion_score: 0.6 },
      ],
      'resolution',
    );
    expect(out).toEqual([]);
  });

  it('分数为 null/NaN 的点丢弃，不显示成 0', () => {
    const out = buildScoreAlignment(
      [
        { trade_date: '2026-11-10', target_date: '2026-12-09', fusion_score: null },
        { trade_date: '2026-11-11', target_date: '2026-12-10', fusion_score: Number.NaN },
        { trade_date: '2026-11-12', target_date: '2026-12-11', fusion_score: 0 },
      ],
      'prediction',
    );
    expect(out).toHaveLength(1);
    expect(out[0].value).toBe(0);
  });

  it('同一天多个点：保留 scoreDate 最新的那条', () => {
    const out = buildScoreAlignment(
      [
        { trade_date: '2026-11-10', target_date: '2026-12-09', fusion_score: 0.1 },
        { trade_date: '2026-11-12', target_date: '2026-12-09', fusion_score: 0.9 },
      ],
      'resolution',
    );
    expect(out).toHaveLength(1);
    expect(out[0].value).toBe(0.9);
    expect(out[0].scoreDate).toBe('2026-11-12');
  });
});

describe('时间轴延长', () => {
  it('T+5 窗口：执行日到目标日 6 个日期全部并入时间轴', () => {
    const history = ['2026-11-11', '2026-11-12'];
    const window = [
      '2026-11-13', '2026-11-16', '2026-11-17',
      '2026-11-18', '2026-11-19', '2026-11-20',
    ];
    expect(extendTimeline(history, window)).toEqual([...history, ...window]);
    expect(futureDatesAfter(history, window)).toEqual(window);
  });

  it('窗口日期已在 K 线里时不重复延长', () => {
    const history = ['2026-11-12', '2026-11-13'];
    const window = ['2026-11-13', '2026-11-16', '2026-11-17'];
    expect(futureDatesAfter(history, window)).toEqual(['2026-11-16', '2026-11-17']);
  });

  it('非法日期不进入时间轴', () => {
    expect(extendTimeline(['2026-11-12'], ['bad-date'])).toEqual(['2026-11-12']);
  });
});

describe('分数隐含目标价', () => {
  it('return 口径才允许换算', () => {
    expect(impliedTargetPrice(0.1, 100, 'return')).toBeCloseTo(110);
    expect(impliedTargetPrice(0.1, 100, 'rank')).toBeNull();
    expect(impliedTargetPrice(0.1, 100, '')).toBeNull();
    expect(impliedTargetPrice(0.1, 100)).toBeNull();
  });

  it('缺失价格/分数返回 null，不编造', () => {
    expect(impliedTargetPrice(null, 100, 'return')).toBeNull();
    expect(impliedTargetPrice(0.1, null, 'return')).toBeNull();
    expect(impliedTargetPrice(0.1, 0, 'return')).toBeNull();
  });
});

describe('窗口兑现状态', () => {
  it('目标日已出现在 K 线里 = 已兑现', () => {
    expect(isWindowResolved('2026-11-20', '2026-11-20')).toBe(true);
    expect(isWindowResolved('2026-11-20', '2026-11-19')).toBe(false);
  });

  it('执行日晚于最后一根 K 线 = 尚未开始', () => {
    expect(isWindowPending('2026-11-13', '2026-11-12')).toBe(true);
    expect(isWindowPending('2026-11-13', '2026-11-13')).toBe(false);
  });
});

describe('latestScore', () => {
  it('返回最后一条有效分数与日期', () => {
    expect(latestScore(T20_POINTS)).toEqual({ value: 0.33, date: '2026-11-12' });
    expect(latestScore([])).toBeNull();
    expect(latestScore([{ trade_date: '2026-11-12', fusion_score: null }])).toBeNull();
  });
});

describe('futureDatesForPeriod：日 K 全窗口 / 周月单点', () => {
  const windowDates = [
    '2026-11-13', '2026-11-16', '2026-11-17',
    '2026-11-18', '2026-11-19', '2026-11-20',
  ];
  const bars = ['2026-11-11', '2026-11-12'];

  it('日 K：完整窗口日期全部延长', () => {
    expect(futureDatesForPeriod('daily', windowDates, bars, '2026-11-13', '2026-11-20'))
      .toEqual(windowDates);
  });

  it('日 K：已在 K 线里的日期不重复', () => {
    expect(futureDatesForPeriod('daily', ['2026-11-12', '2026-11-13'], bars))
      .toEqual(['2026-11-13']);
  });

  it('周 K：只取执行日/目标日两个真实交易日', () => {
    expect(futureDatesForPeriod('weekly', windowDates, bars, '2026-11-13', '2026-11-20'))
      .toEqual(['2026-11-13', '2026-11-20']);
  });

  it('月 K：目标日已在历史桶里时不延长（窗口已兑现）', () => {
    expect(futureDatesForPeriod('monthly', windowDates, ['2026-11-20'], '2026-11-13', '2026-11-20'))
      .toEqual([]);
  });

  it('周/月缺执行日/目标日：不猜，返回空', () => {
    expect(futureDatesForPeriod('weekly', windowDates, bars, null, undefined)).toEqual([]);
  });

  it('周/月：过去的目标日不延长（预测已兑现/分数过期）', () => {
    expect(
      futureDatesForPeriod(
        'weekly',
        ['2026-10-01', '2026-10-20'],
        ['2026-11-11', '2026-11-12'],
        '2026-10-01',
        '2026-10-20',
      ),
    ).toEqual([]);
  });
});
