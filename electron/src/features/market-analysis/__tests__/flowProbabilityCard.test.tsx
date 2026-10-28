/** 概率模型卡：排序分/逆流候选/板块聚合接线 + 产物缺失降级。 */
import { render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import React from 'react';

const payload = {
  available: true,
  trade_date: '20261027',
  requested_date: null,
  as_of_latest: true,
  intent: {
    trade_date: '20261027',
    stocks: {
      SH600056: { news_score: 0.52, news_n: 2 },
      SZ002487: { lhb_net: -120000000 },
      SH601789: { block_premium: -0.08, block_amount: 50000000 },
    },
  },
  artifact: {
    schema: 'flow_probability.v1',
    built_at: '2026-10-28T15:00:00+00:00',
    trade_date: '20261027',
    window: ['20220104', '20261027'],
    metrics: { '5': { auc: 0.5326, top_mean: 0.174, bot_mean: -1.389, top_hit: 0.464, base_hit: 0.426, n_days: 213 } },
    thresholds: { '5': { hi: 0.55, lo: 0.45 } },
    latest: {
      stocks: {
        prob_map: { SH600056: { '5': 0.62 } },
        top_5: [{ symbol: 'SH600056', name: '中国医药', industry: '医药商业', net_ratio: 0.02, z20: 0.4, r5: 1.2, p5: 0.62 }],
        bottom_5: [{ symbol: 'SZ002487', name: '大金重工', industry: '风电设备', net_ratio: -0.03, z20: -1.1, r5: -8.2, p5: 0.31 }],
        outflow_strong_5: [{ symbol: 'SH601789', name: '宁波建工', industry: '建筑装饰', net_ratio: -0.14, z20: -1.6, r5: 0.5, p5: 0.58 }],
        inflow_weak_5: [{ symbol: 'SZ002354', name: '天娱数科', industry: '传媒', net_ratio: 0.02, z20: -0.2, r5: 1.0, p5: 0.2 }],
      },
      sectors: [
        { name: '广播电视', n: 7, net_ratio: -0.05, p5: 0.61 },
        { name: '风电设备', n: 21, net_ratio: 0.03, p5: 0.35 },
      ],
    },
  },
};

vi.mock('../services/moneyFlowService', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../services/moneyFlowService')>();
  return { ...actual, getFlowProbability: vi.fn(() => Promise.resolve(payload)) };
});

import { FlowProbabilityCard } from '../components/FlowProbabilityCard';

describe('FlowProbabilityCard', () => {
  it('渲染模型分、逆流候选与板块聚合', async () => {
    render(<FlowProbabilityCard />);
    await waitFor(() => expect(screen.getByText('未来强度概率（模型）')).toBeInTheDocument());
    expect(screen.getByText(/强势概率 Top/)).toBeInTheDocument();
    expect(screen.getByText('中国医药')).toBeInTheDocument();
    expect(screen.getAllByText(/逆流候选/).length).toBeGreaterThan(0);
    expect(screen.getByText('宁波建工')).toBeInTheDocument();
    expect(screen.getAllByText(/流入但弱/).length).toBeGreaterThan(0);
    expect(screen.getByText('天娱数科')).toBeInTheDocument();
    expect(screen.getByText(/OOS AUC 0.533/)).toBeInTheDocument();
    expect(screen.getByText('消息+')).toBeInTheDocument();
    expect(screen.getByText('龙虎')).toBeInTheDocument();
    expect(screen.getByText('大宗折')).toBeInTheDocument();
    expect(screen.getAllByText(/广播电视/).length).toBeGreaterThan(0);
  });

  it('产物缺失时显示构建提示', async () => {
    const mocked = await import('../services/moneyFlowService');
    (mocked.getFlowProbability as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      available: false,
      reason: 'not_built',
      hint: 'python -m backend.scripts.market_analysis.build_flow_probability',
    });
    render(<FlowProbabilityCard />);
    await waitFor(() => expect(screen.getByText(/概率模型产物未生成/)).toBeInTheDocument());
  });
});
