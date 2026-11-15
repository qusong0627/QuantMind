import { render } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import KLineChart from '../KLineChart';
import type { BtTrade, Kline } from '../api';

const mocks = vi.hoisted(() => {
  const setMarkers = vi.fn();
  const setData = vi.fn();
  const addSeries = vi.fn(() => ({ setData }));
  const createChart = vi.fn(() => ({
    addSeries,
    priceScale: vi.fn(() => ({ applyOptions: vi.fn() })),
    applyOptions: vi.fn(),
    timeScale: () => ({ fitContent: vi.fn() }),
    panes: () => [],
    remove: vi.fn(),
  }));
  const createSeriesMarkers = vi.fn(() => ({ setMarkers }));
  return { setMarkers, setData, addSeries, createChart, createSeriesMarkers };
});

vi.mock('lightweight-charts', () => ({
  createChart: mocks.createChart,
  createSeriesMarkers: mocks.createSeriesMarkers,
  CandlestickSeries: {},
  HistogramSeries: {},
  LineSeries: {},
  ColorType: { Solid: 'solid' },
  CrosshairMode: { Normal: 0 },
}));

class ResizeObserverStub {
  observe() {}
  disconnect() {}
}

const bars: Kline[] = [
  { date: '2025-01-01', open: 10, high: 11, low: 9, close: 10.5, volume: 100 },
  { date: '2025-01-02', open: 10.5, high: 12, low: 10, close: 11.5, volume: 120 },
];

const trades: BtTrade[] = [
  {
    entry_time: '2025-01-01',
    entry_price: 10,
    qty: 100,
    signal: 'long',
    exit_time: '2025-01-02',
    exit_price: 11,
    profit: 100,
    profit_pct: 10,
  },
];

describe('KLineChart 合规用词', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.stubGlobal('ResizeObserver', ResizeObserverStub);
  });

  it('marks entries/exits with arrows only — no 买/卖 text', () => {
    // Act
    render(<KLineChart bars={bars} trades={trades} />);

    // Assert
    expect(mocks.setMarkers).toHaveBeenCalled();
    const marks = mocks.setMarkers.mock.calls.at(-1)?.[0] as {
      text?: string;
      shape?: string;
    }[];
    expect(marks).toHaveLength(2);
    expect(marks.map((m) => m.shape)).toEqual(['arrowUp', 'arrowDown']);
    for (const m of marks) {
      expect(m.text).toBe('');
      expect(JSON.stringify(m)).not.toMatch(/买|卖/);
    }
  });

  it('uses the market-specific up/down colors', () => {
    // Act：美股绿涨红跌
    render(<KLineChart bars={bars} trades={trades} upColor="#1a9e5c" downColor="#e2373b" />);

    // Assert：第一个 series = 蜡烛图
    const candleOptions = mocks.addSeries.mock.calls[0][1] as {
      upColor: string;
      downColor: string;
    };
    expect(candleOptions.upColor).toBe('#1a9e5c');
    expect(candleOptions.downColor).toBe('#e2373b');
  });

  it('maps minute timestamps to epoch seconds (intraday axis)', () => {
    // Arrange
    const minuteBars: Kline[] = [
      { date: '2025-01-02 09:35:00', open: 10, high: 11, low: 9, close: 10.5, volume: 100 },
      { date: '2025-01-02 09:40:00', open: 10.5, high: 12, low: 10, close: 11.5, volume: 120 },
    ];

    // Act
    render(<KLineChart bars={minuteBars} freq="5m" tzOffsetHours={8} />);

    // Assert
    const seriesData = mocks.setData.mock.calls.at(-1)?.[0] as { time: unknown }[];
    expect(typeof seriesData[0].time).toBe('number');
    const sec = seriesData[0].time as number;
    // 2025-01-02 09:35 北京 = 2025-01-02T01:35:00Z
    expect(new Date(sec * 1000).toISOString()).toBe('2025-01-02T01:35:00.000Z');
  });
});
