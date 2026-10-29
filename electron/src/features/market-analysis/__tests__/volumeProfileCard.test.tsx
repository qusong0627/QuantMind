/** 筹码结构卡：成本/POC/价值区/14% 低分位渲染。 */
import { render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import React from 'react';

const payload = {
  available: true,
  symbol: '000300.SH',
  name: '沪深300',
  trade_date: '2026-10-28',
  window: 60,
  scope: 'index',
  cost_scope: '全市场主力/散户资金加权（指数无逐笔席位）',
  last: 4083.87,
  high: 4744,
  low: 3900,
  vwap: 4400,
  main_cost: 4462.13,
  retail_cost: 4442.86,
  main_premium_pct: -8.47,
  retail_premium_pct: -8.08,
  poc: 4557.37,
  vah: 4724.3,
  val: 4468.9,
  p14_price: 4227.09,
  p14_pct_from_last: 3.5,
  drawdown14: 4079.8,
  drawdown_from_high_pct: -13.9,
  profile: [
    { low: 4468.9, high: 4557.3, mid: 4513.1, amount_yi: 100, share: 0.4 },
    { low: 4557.3, high: 4640, mid: 4598.6, amount_yi: 80, share: 0.3 },
    { low: 4083.8, high: 4227.1, mid: 4155.4, amount_yi: 30, share: 0.1 },
  ],
  time_windows: { available: false, note: '本地分钟数据陈旧，不用旧数据冒充当前交易时段' },
  method: '成交额均匀摊到价格箱',
};

vi.mock('../services/moneyFlowService', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../services/moneyFlowService')>();
  return { ...actual, getVolumeProfile: vi.fn(() => Promise.resolve(payload)) };
});

import { VolumeProfileCard } from '../components/VolumeProfileCard';

describe('VolumeProfileCard', () => {
  it('渲染主力/散户成本、POC/价值区与 14% 低分位', async () => {
    render(<VolumeProfileCard symbol="000300.SH" />);
    await waitFor(() => expect(screen.getByText('筹码结构 · 主力/散户成本')).toBeInTheDocument());
    expect(screen.getByText('4462.13')).toBeInTheDocument();
    expect(screen.getByText('4442.86')).toBeInTheDocument();
    expect(screen.getAllByText(/4557.37/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/4227.09/).length).toBeGreaterThan(0);
    expect(screen.getByText(/本地分钟数据陈旧/)).toBeInTheDocument();
  });
});
