/** 资金流预测力卡：研究产物缺失/存在两条路径 + 当日匹配接线。 */
import { render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import React from 'react';

const payload = {
  available: true,
  artifact: {
    schema: 'flow_predictive.v1',
    built_at: '2026-10-28T15:00:00+00:00',
    trade_date: '20261027',
    window: ['20220104', '20261027'],
    tables: {
      'net_exec_h5': [
        { q: 1, n: 100, mean: -0.08, stock_win: 0.44, date_win: 0.37, date_mean: -0.1 },
        { q: 5, n: 100, mean: 0.07, stock_win: 0.45, date_win: 0.59, date_mean: 0.11 },
      ],
      'composite_exec_h5': [
        { q: 1, n: 100, mean: -0.43, stock_win: 0.41, date_win: 0.35, date_mean: -0.47 },
        { q: 5, n: 100, mean: 0.3, stock_win: 0.47, date_win: 0.6, date_mean: 0.29 },
      ],
      'composite_exec_h5_is': [{ q: 5, n: 80, mean: 0.3, stock_win: 0.47, date_win: 0.62, date_mean: 0.3 }],
      'composite_exec_h5_oos': [{ q: 5, n: 20, mean: 0.2, stock_win: 0.45, date_win: 0.55, date_mean: 0.2 }],
    },
    patterns: {
      '强一致+未大涨（一致性前20% & 5日涨幅≤2%）|h5': {
        pattern: '强一致+未大涨（一致性前20% & 5日涨幅≤2%）', h: 5, n: 1000,
        date_win: 0.65, date_mean: 0.28, stock_win: 0.47, yearly: { '2026': 0.6 },
      },
      '高净流入+大涨（z20≥1.5 & 5日涨幅≥5%）|h5': {
        pattern: '高净流入+大涨（z20≥1.5 & 5日涨幅≥5%）', h: 5, n: 800,
        date_win: 0.43, date_mean: -0.6, stock_win: 0.42, yearly: { '2026': 0.45 },
      },
    },
    'heatmap_exec_h5': {
      mean: Array.from({ length: 5 }, () => Array.from({ length: 5 }, () => -0.1)),
      win: Array.from({ length: 5 }, () => Array.from({ length: 5 }, () => 0.45)),
      n: Array.from({ length: 5 }, () => Array.from({ length: 5 }, () => 100)),
    },
    disclosure: '历史统计不等于未来收益',
  },
  current: {
    available: true,
    trade_date: '20261028',
    coverage: { scored: 2900 },
    top: [{ symbol: 'SH600056', name: '中国医药', industry: '医药商业', score: 88.7, r5: 0.0, z20: 0.5, net_ratio: 0.01 }],
    bottom: [{ symbol: 'SZ002487', name: '大金重工', industry: '风电设备', score: 9.7, r5: -8.2, z20: -1.2, net_ratio: -0.03 }],
    patterns: { consistency_not_extended: { count: 1, items: [{ symbol: 'SH600056', name: '中国医药', industry: '医药商业', score: 88.7 }] } },
    sectors_top: [{ name: '广播电视', score: 67.2, n: 7 }],
    sectors_bottom: [{ name: '自动化设备', score: 25.2, n: 40 }],
  },
  meta: {
    score_definition: '分位等权',
    components: [],
    universe: '剔除 ST / 低价 / 低成交 / 小市值',
    disclosure: '历史统计不等于未来收益',
  },
};

vi.mock('../services/moneyFlowService', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../services/moneyFlowService')>();
  return { ...actual, getFlowPredictive: vi.fn(() => Promise.resolve(payload)) };
});

import { FlowPredictiveCard } from '../components/FlowPredictiveCard';

describe('FlowPredictiveCard', () => {
  it('渲染金额对照、质量分、热力图、形态与当日靠前', async () => {
    render(<FlowPredictiveCard />);
    await waitFor(() => expect(screen.getByText('资金流预测力')).toBeInTheDocument());
    expect(screen.getByText(/主力净流入分位/)).toBeInTheDocument();
    expect(screen.getByText(/资金流质量分分位/)).toBeInTheDocument();
    expect(screen.getByText(/资金流 × 价格位置/)).toBeInTheDocument();
    expect(screen.getByText(/强一致\+未大涨/)).toBeInTheDocument();
    expect(screen.getByText('中国医药')).toBeInTheDocument();
    expect(screen.getByText(/2900 只可评分/)).toBeInTheDocument();
  });

  it('产物缺失时给可照做的提示，仍显示当日分数', async () => {
    const mocked = await import('../services/moneyFlowService');
    (mocked.getFlowPredictive as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      ...payload,
      artifact: null,
    });
    render(<FlowPredictiveCard />);
    await waitFor(() => expect(screen.getByText(/研究产物尚未生成/)).toBeInTheDocument());
    expect(screen.getByText('中国医药')).toBeInTheDocument();
  });
});
