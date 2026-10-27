/**
 * 大盘情绪面板接线用例。
 *
 * 钉住三件事：
 * 1. 快照能正常渲染分数、区间与三个指数切换；
 * 2. 切换指数后扇形与统计随之更新（不是永远显示第一个）；
 * 3. 快照缺失（503）时给可照做的空态，而不是白屏或假数据。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import React from 'react';

import { MarketSentimentPanel } from '../components/MarketSentimentPanel';
import { coldPercentile } from '../components/BreadthFactorBars';

const fan = (p10: number, p50: number, p90: number) =>
  Array.from({ length: 21 }, (_, k) => ({
    k,
    p10: (p10 * k) / 20,
    p25: (p10 * k) / 40,
    p50: (p50 * k) / 20,
    p75: (p90 * k) / 40,
    p90: (p90 * k) / 20,
  }));

const history = Array.from({ length: 60 }, (_, i) => ({
  date: `2026-07-${String((i % 28) + 1).padStart(2, '0')}`,
  close: 4000 + i,
}));

const payload = {
  trade_date: '2026-10-26',
  generated_at: '2026-10-27T06:00:00+00:00',
  source: 'test',
  horizon: 20,
  stress: {
    score: 80.2,
    zone: 'extreme_pressure',
    zone_label: '极端压力',
    zone_note: '历史同状态偏多，但波动最大',
    percentile: 0.94,
  },
  indicators: [
    { key: 'breadth20', label: '站上 MA20 个股占比', value: 22.3, unit: '%', pct: 0.17, zone: 'normal' },
    { key: 'trin_ema10', label: 'TRIN 10 日 EMA', value: 0.958, unit: '', pct: 0.6, zone: 'normal' },
  ],
  as_of: false,
  available_from: '2026-01-05',
  available_to: '2026-10-26',
  factor_history: {
    dates: Array.from({ length: 10 }, (_, i) => `2026-10-${String(i + 1).padStart(2, '0')}`),
    items: [
      { key: 'breadth20', label: '站上 MA20', cold: [10, 20, 30, 40, 50, 60, 70, 80, 90, 95] },
    ],
  },
  stress_history: Array.from({ length: 40 }, (_, i) => ({
    date: `2026-08-${String((i % 28) + 1).padStart(2, '0')}`,
    stress: 40 + i,
    breadth20: 0.3,
  })),
  indices: {
    '000300.SH': {
      name: '沪深300',
      last: 4103.54,
      change_pct: -0.04,
      history,
      fan: fan(-5, 0.6, 8),
      stats: { n_days: 127, n_clusters: 24, mean: 1.19, median: 0.63, win: 0.567, p10: -4.71, p90: 7.87, p_drop5: 0.205, p_drop10: 0.008 },
      baseline: { mean: 0.35, win: 0.508, p_drop10: 0.043 },
      actual: [],
      future_dates: [],
      forecast: {
        '5': { pred: 0.17, prob_up: 0.5, base_prob: 0.48, p10: -3.1, p50: 0.1, p90: 2.8 },
        '10': { pred: 0.31, prob_up: 0.47, base_prob: 0.49, p10: -3.7, p50: 0.5, p90: 5.1 },
        '20': { pred: 0.35, prob_up: 0.49, base_prob: 0.49, p10: -5.3, p50: 0.4, p90: 7.4 },
      },
    },
    '000001.SH': {
      name: '上证指数',
      last: 3697.52,
      change_pct: 0.19,
      history,
      fan: fan(-4, 1.0, 7),
      stats: { n_days: 127, n_clusters: 24, mean: 1.58, median: 1.07, win: 0.606, p10: -3.77, p90: 7.33, p_drop5: 0.142, p_drop10: 0 },
      baseline: { mean: 0.28, win: 0.518, p_drop10: 0.039 },
      actual: [],
      future_dates: [],
      forecast: { '5': { pred: 0.04, prob_up: 0.5, base_prob: 0.52, p10: -2.8, p50: 0.1, p90: 2.4 } },
    },
    '000852.SH': {
      name: '中证1000',
      last: 6630.78,
      change_pct: -1.54,
      history,
      fan: fan(-6, 1.5, 14),
      stats: { n_days: 127, n_clusters: 24, mean: 3.05, median: 1.68, win: 0.63, p10: -6.15, p90: 13.77, p_drop5: 0.331, p_drop10: 0.15 },
      baseline: { mean: 0.13, win: 0.48, p_drop10: 0.12 },
      actual: [],
      future_dates: [],
      forecast: { '5': { pred: 0.09, prob_up: 0.51, base_prob: 0.51, p10: -4.7, p50: 0.0, p90: 3.6 } },
    },
  },
};

const jsonResponse = (body: unknown, status = 200) =>
  Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(body),
  } as Response);

describe('MarketSentimentPanel', () => {
  beforeEach(() => {
    localStorage.setItem('access_token', 't');
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('渲染压力分、区间标签与三个指数', async () => {
    vi.stubGlobal('fetch', vi.fn(() => jsonResponse(payload)));
    render(<MarketSentimentPanel />);

    await waitFor(() => expect(screen.getByText('80.2')).toBeInTheDocument());
    expect(screen.getByText('极端压力')).toBeInTheDocument();
    expect(screen.getByText('比历史 94% 的交易日更冷')).toBeInTheDocument();
    expect(screen.getAllByRole('button', { name: /上证指数/ }).length).toBeGreaterThan(0);
    expect(screen.getAllByRole('button', { name: /中证1000/ }).length).toBeGreaterThan(0);
  });

  it('切换指数后统计随之更新', async () => {
    vi.stubGlobal('fetch', vi.fn(() => jsonResponse(payload)));
    render(<MarketSentimentPanel />);
    await waitFor(() => expect(screen.getByText('80.2')).toBeInTheDocument());

    // 中位在扇形右标签与统计块各出现一次
    expect(screen.getAllByText('+0.6%').length).toBeGreaterThan(0);
    await userEvent.click(screen.getAllByRole('button', { name: /中证1000/ })[0]);
    await waitFor(() => expect(screen.getAllByText('+1.7%').length).toBeGreaterThan(0));
  });

  it('快照缺失时显示可照做的空态', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => jsonResponse({ detail: { message: '大盘情绪快照尚未生成' } }, 503)),
    );
    render(<MarketSentimentPanel />);

    await waitFor(() => expect(screen.getByText('大盘情绪快照不可用')).toBeInTheDocument());
    expect(screen.getByText(/market_sentiment_snapshot/)).toBeInTheDocument();
  });
});

describe('BreadthFactorBars 方向归一', () => {
  const mk = (key: string, pct: number) => ({
    key,
    label: key,
    value: 1,
    unit: '',
    pct,
    zone: 'normal' as const,
  });

  it('ADR 越高越热（冷度下降）', () => {
    expect(coldPercentile(mk('adr', 0.9))).toBeCloseTo(10);
    expect(coldPercentile(mk('adr', 0.1))).toBeCloseTo(90);
  });

  it('TRIN 越高越冷（冷度上升）', () => {
    expect(coldPercentile(mk('trin_ema10', 0.9))).toBeCloseTo(90);
    expect(coldPercentile(mk('trin_ema10', 0.1))).toBeCloseTo(10);
  });

  it('分位缺失返回 null，不当作 0', () => {
    expect(coldPercentile({ ...mk('adr', 0.5), pct: null })).toBeNull();
  });
});

describe('回看模式', () => {
  it('叠加实际走势并给出一眼可见的对错判定', async () => {
    const asof = {
      ...payload,
      as_of: true,
      trade_date: '2026-08-14',
      indices: {
        ...payload.indices,
        '000300.SH': {
          ...payload.indices['000300.SH'],
          actual: Array.from({ length: 21 }, (_, k) => ({ k, pct: -3.34 * (k / 20) })),
          stats: { ...payload.indices['000300.SH'].stats, median: 0.63, p10: -4.71, p90: 7.87 },
        },
      },
    };
    vi.stubGlobal('fetch', vi.fn(() => jsonResponse(asof)));
    render(<MarketSentimentPanel />);

    await waitFor(() => expect(screen.getByText('之后 20 天实际走势')).toBeInTheDocument());
    expect(screen.getByText('-3.34%')).toBeInTheDocument();
    // 实际 -3.34% 落在区间内，但当时典型为 +0.63% → 方向相反
    expect(screen.getByText('△ 区间对、方向反了')).toBeInTheDocument();
    expect(screen.getByText('回到最新')).toBeInTheDocument();
  });
});
