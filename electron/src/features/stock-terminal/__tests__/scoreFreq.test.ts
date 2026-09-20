/**
 * 分数分频标注测试。
 *
 * 盯的是「把隔夜分当盘中分」这个错误的两种写法：
 * 1. 未知/缺失频率被默认成实时（用户以为分数在盘中刷新，实际是昨天的批次分）；
 * 2. 开关开着就宣称实时（后端在行情未到达时**不发布**伪实时分，此时计数仍是 0）。
 */

import { describe, expect, it } from 'vitest';
import { scoreFreqView, tableFreqView } from '../scoreFreq';

describe('scoreFreqView · 单行徽章', () => {
  it('realtime 给「实时」徽章并把日期写进悬停说明', () => {
    const v = scoreFreqView('realtime', '2026-09-20');

    expect(v?.label).toBe('实时');
    expect(v?.freq).toBe('realtime');
    expect(v?.title).toContain('2026-09-20');
    expect(v?.title).toContain('盘中');
  });

  it('daily 给「日频」徽章并说明为何不刷新', () => {
    const v = scoreFreqView('daily', '2026-09-21');

    expect(v?.label).toBe('日频');
    expect(v?.title).toContain('2026-09-21');
    expect(v?.title).toContain('未开启');
  });

  it('没有分数信息时不给徽章（而不是默认成实时）', () => {
    // 该行没有分数 / 取数失败 / 老后端没这个字段：宁可不标，也不能标错
    expect(scoreFreqView(null, '2026-09-21')).toBeNull();
    expect(scoreFreqView(undefined)).toBeNull();
    expect(scoreFreqView('')).toBeNull();
    expect(scoreFreqView('intraday')).toBeNull();
  });

  it('缺 asOf 也不崩，只是不写日期', () => {
    const v = scoreFreqView('realtime', null);

    expect(v?.label).toBe('实时');
    expect(v?.title).not.toContain('· ');
  });
});

describe('tableFreqView · 列表头部总览', () => {
  it('有实时行时头部标「含实时分」并说明行徽章为准', () => {
    const v = tableFreqView(529, '2026-09-20');

    expect(v.freq).toBe('realtime');
    expect(v.title).toContain('529');
    expect(v.title).toContain('实时」徽章');
  });

  it('实时行计数为 0 时头部标日频（开关开着但没落库 ≠ 实时）', () => {
    const v = tableFreqView(0, '2026-09-21');

    expect(v.freq).toBe('daily');
    expect(v.title).toContain('未开启');
  });

  it('计数缺失（老后端）按日频处理', () => {
    expect(tableFreqView(null, '2026-09-21').freq).toBe('daily');
    expect(tableFreqView(undefined).freq).toBe('daily');
  });
});
