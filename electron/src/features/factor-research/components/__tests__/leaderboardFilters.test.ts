/**
 * 排行榜筛选纯函数契约：
 * - 阈值一律存**原值**（% 的换算在输入框层做）；
 * - 回撤列按**幅度**（|v|）比较 —— 回撤恒负，直接比原始值方向是反的；
 * - RankIC 支持「按绝对值」模式；
 * - 数值缺失（null/NaN）的行不匹配任何数值阈值（缺失 ≠ 0）；
 * - 标签组内 OR、组间 AND。
 */
import { describe, test, expect } from 'vitest';
import {
  EMPTY_LEADERBOARD_FILTERS,
  applyLeaderboardFilters,
  countActiveFilters,
} from '../leaderboardFilters';
import { matchTagFilter } from '../common';
import type { LeaderboardRow } from '../../types/factorResearch';

function row(over: Partial<LeaderboardRow>): LeaderboardRow {
  return {
    code: 'x', rank: 1, name_cn: 'x', l1: 'A', l2: 'a',
    composite: 1, env_tag: '', time_tag: '',
    ...over,
  } as LeaderboardRow;
}

const ROWS: LeaderboardRow[] = [
  row({ code: 'f1', name_cn: '动量12', sharpe: 1.5, max_drawdown: -0.08, ic_mean: 0.05, median_mv_yi: 800, mv_style: '大盘', top_industries: [{ name: '银行', count: 3 }] }),
  row({ code: 'f2', name_cn: '反转20', sharpe: 0.5, max_drawdown: -0.35, ic_mean: -0.07, median_mv_yi: null, mv_style: '小盘', top_industries: [{ name: '电子', count: 2 }] }),
  row({ code: 'f3', name_cn: '波动率', sharpe: null as unknown as number, max_drawdown: null as unknown as number, ic_mean: 0.02, mv_style: null }),
];

const run = (over: Partial<Parameters<typeof applyLeaderboardFilters>[1]>, tagFilter: string[] = []) =>
  applyLeaderboardFilters(ROWS, { ...EMPTY_LEADERBOARD_FILTERS, ...over }, tagFilter, null);

describe('applyLeaderboardFilters', () => {
  test('空筛选返回全量', () => {
    expect(run({})).toHaveLength(3);
    expect(countActiveFilters(EMPTY_LEADERBOARD_FILTERS)).toBe(0);
  });

  test('数值下限：低于阈值的行被剔除', () => {
    expect(run({ numeric: { sharpe: { min: 1 } } }).map((r) => r.code)).toEqual(['f1']);
  });

  test('数值缺失的行不匹配任何阈值（缺失 ≠ 0）', () => {
    // f3 的 sharpe 是 null：min -99 / max 99 都该把它排除
    expect(run({ numeric: { sharpe: { min: -99 } } }).map((r) => r.code)).toEqual(['f1', 'f2']);
    expect(run({ numeric: { sharpe: { max: 99 } } }).map((r) => r.code)).toEqual(['f1', 'f2']);
  });

  test('回撤按幅度比较：max=0.2 → 回撤不超过 20% 的行留下', () => {
    expect(run({ numeric: { max_drawdown: { max: 0.2 } } }).map((r) => r.code)).toEqual(['f1']);
    expect(run({ numeric: { max_drawdown: { min: 0.2 } } }).map((r) => r.code)).toEqual(['f2']);
  });

  test('RankIC 按绝对值模式：|IC| ≥ 0.04 同时留下正反两个方向', () => {
    expect(run({ numeric: { ic_mean: { min: 0.04, abs: true } } }).map((r) => r.code)).toEqual(['f1', 'f2']);
    expect(run({ numeric: { ic_mean: { min: 0.04 } } }).map((r) => r.code)).toEqual(['f1']);
  });

  test('文本筛选：名称或代码包含即命中；行业按名称包含', () => {
    expect(run({ name: 'F2' }).map((r) => r.code)).toEqual(['f2']);
    expect(run({ name: '动量' }).map((r) => r.code)).toEqual(['f1']);
    expect(run({ industry: '电子' }).map((r) => r.code)).toEqual(['f2']);
  });

  test('市值风格：只留该风格（null 风格行被剔除）', () => {
    expect(run({ mvStyle: '大盘' }).map((r) => r.code)).toEqual(['f1']);
  });

  test('分类限定与标签筛选叠加生效', () => {
    const rows = [
      row({ code: 'a', l1: 'X', l2: 'x1', env_tag: '牛市进攻型', time_tag: '近期转强' }),
      row({ code: 'b', l1: 'X', l2: 'x2', env_tag: '牛市进攻型', time_tag: '长期稳定型' }),
      row({ code: 'c', l1: 'Y', l2: 'y1', env_tag: '牛市进攻型', time_tag: '近期转强' }),
    ];
    const out = applyLeaderboardFilters(
      rows,
      EMPTY_LEADERBOARD_FILTERS,
      ['牛市进攻型', '近期转强'],
      { l1: 'X', l2: null },
    );
    expect(out.map((r) => r.code)).toEqual(['a']);
  });

  test('countActiveFilters：文本去空白、只数有上下限的数值列', () => {
    expect(
      countActiveFilters({
        name: '  ',
        industry: '',
        mvStyle: '大盘',
        numeric: { sharpe: { min: 1 }, max_drawdown: {} },
      }),
    ).toBe(2);
  });
});

describe('matchTagFilter（组内 OR / 组间 AND）', () => {
  test('空筛选恒真', () => {
    expect(matchTagFilter('牛市进攻型', '近期转强', [])).toBe(true);
  });
  test('单标签只认它', () => {
    expect(matchTagFilter('牛市进攻型', '近期转强', ['熊市防御型'])).toBe(false);
    expect(matchTagFilter('熊市防御型', '近期转强', ['熊市防御型'])).toBe(true);
  });
  test('环境组内 OR', () => {
    expect(matchTagFilter('牛市进攻型', '', ['牛市进攻型', '熊市防御型'])).toBe(true);
  });
  test('组间 AND', () => {
    expect(matchTagFilter('牛市进攻型', '长期稳定型', ['牛市进攻型', '近期转强'])).toBe(false);
    expect(matchTagFilter('牛市进攻型', '近期转强', ['牛市进攻型', '近期转强'])).toBe(true);
  });
});
