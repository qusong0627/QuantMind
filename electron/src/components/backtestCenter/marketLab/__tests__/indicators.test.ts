import { describe, it, expect } from 'vitest';
import { alignSeries, paneGroups, seriesColor, toPoints } from '../indicators';
import type { IndicatorSet } from '../api';

describe('indicator alignment', () => {
  it('aligns a full-history series to the tail of the K-line window', () => {
    // Arrange：指标是全历史，K 线只取最近 3 根
    const values = [1, 2, 3, 4, 5, 6];

    // Act
    const aligned = alignSeries(values, 3);

    // Assert
    expect(aligned).toEqual([4, 5, 6]);
  });

  it('leaves shorter series untouched', () => {
    expect(alignSeries([1, 2], 5)).toEqual([1, 2]);
  });

  it('refuses to draw a series from a different source', () => {
    // Arrange：指标比 K 线还短 → 不同源，宁可不画
    const bars = [{ date: '2025-01-01' }, { date: '2025-01-02' }, { date: '2025-01-03' }];

    // Act
    const points = toPoints(bars, [1, 2]);

    // Assert
    expect(points).toEqual([]);
  });

  it('maps null values to whitespace points instead of connecting a fake line', () => {
    // Arrange
    const bars = [{ date: '2025-01-01' }, { date: '2025-01-02' }];

    // Act
    const points = toPoints(bars, [null, 3]);

    // Assert
    expect(points).toEqual([
      { time: '2025-01-01' },
      { time: '2025-01-02', value: 3 },
    ]);
  });

  it('keeps pane group order and de-duplicates repeats', () => {
    // Arrange
    const set: IndicatorSet = {
      bars: 1,
      overlays: [],
      panes: [
        { key: 'dif', group: 'macd', label: 'DIF', kind: 'line', values: [] },
        { key: 'dea', group: 'macd', label: 'DEA', kind: 'line', values: [] },
        { key: 'rsi', group: 'rsi', label: 'RSI', kind: 'line', values: [] },
      ],
    };

    // Act / Assert
    expect(paneGroups(set)).toEqual(['macd', 'rsi']);
  });

  it('cycles palette colors by index', () => {
    expect(seriesColor(true, 0)).not.toBe(seriesColor(true, 1));
    expect(seriesColor(true, 8)).toBe(seriesColor(true, 0));
  });
});
