import { describe, it, expect } from 'vitest';
import { fromPineReport, fromTemplate } from '../labResult';
import type { BtResult, PineReport } from '../api';

const btResult = (): BtResult => ({
  stats: {
    'Net profit': { value: 1200, pct: 1.2 },
    'Max equity drawdown': { value: null, pct: null },
  },
  trades: [
    {
      entry_time: '2025-01-02',
      entry_price: 10,
      qty: 100,
      signal: 'buy',
      exit_time: '2025-01-10',
      exit_price: 11,
      profit: 100,
      profit_pct: 10,
    },
  ],
  equity: [
    { date: '2025-01-02', value: 100000 },
    { date: '2025-01-10', value: 101200 },
  ],
  indicators: undefined,
  meta: {
    strategy: 'sma_cross',
    name: '双均线交叉',
    symbol: '600309.SH',
    name_cn: '万华化学',
    adj: 'backward',
    market: 'us',
    start: '2025-01-01',
    end: '2025-01-31',
    bars: 600,
    params: { fast: 5, slow: 20 },
  },
});

describe('labResult adapters', () => {
  it('normalizes template results and keeps the unit + capital', () => {
    // Act
    const r = fromTemplate(btResult());

    // Assert
    expect(r.source).toBe('template');
    expect(r.market).toBe('us');
    expect(r.freq).toBe('1d');
    expect(r.symbol).toBe('600309.SH');
    expect(r.unit).toBe('¥');
    expect(r.capital).toBe(100000);
    expect(r.trades).toHaveLength(1);
    expect(r.indicators).toEqual({ bars: 0, overlays: [], panes: [] });
  });

  it('keeps missing stats as null instead of substituting 0', () => {
    // Act
    const r = fromTemplate(btResult());

    // Assert
    expect(r.stats['Max equity drawdown']).toEqual({ value: null, pct: null });
  });

  it('uses trade_rows (not the trade count) for Pine reports', () => {
    // Arrange：Pine 报告里 `trades` 是**笔数**，明细在 `trade_rows`
    const report: PineReport = {
      id: 'pine-1',
      symbol: '600519.SH',
      adj: 'backward',
      trades: 2,
      stats: { 'Net profit': { pct: 5.5 } },
      trade_rows: [
        {
          entry_time: '2025-02-03',
          entry_price: 100,
          qty: 10,
          signal: 'buy',
          exit_time: '2025-02-10',
          exit_price: 110,
          profit: 100,
          profit_pct: 10,
        },
      ],
      equity: [{ date: '2025-02-03', value: 50000 }],
    };

    // Act
    const r = fromPineReport(report, '策略库标题');

    // Assert
    expect(r.trades).toHaveLength(1);           // 不是 2（笔数）
    expect(r.title).toBe('策略库标题');
    expect(r.market).toBe('cn');                // 老报告无 market → 按 A 股
    expect(r.freq).toBe('1d');                  // 老报告无 freq → 日线
    expect(r.unit).toBe('');
    expect(r.capital).toBe(50000);
  });

  it('treats an empty indicators object as no indicators', () => {
    // Arrange：后端缺字段时可能给 {}（truthy，直接读 .overlays 会炸）
    const report = {
      id: 'x',
      symbol: '600000.SH',
      adj: 'backward',
      trades: 0,
      stats: {},
      trade_rows: [],
      equity: [],
      indicators: {},
    } as unknown as PineReport;

    // Act
    const r = fromPineReport(report);

    // Assert
    expect(r.indicators).toEqual({ bars: 0, overlays: [], panes: [] });
  });
});
