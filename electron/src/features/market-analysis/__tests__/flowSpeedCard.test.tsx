/** 资金流速率卡：板块/个股切换、加速流入/流出、日均与加速度展示。 */
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import React from 'react';

import { FlowSpeedCard } from '../components/FlowSpeedCard';

const speed = {
  sectors_up: [
    { name: '半导体', net_daily: 2.4e8, prev_net_daily: 1.1e8, accel: 1.3e8, net_ratio: 0.031, prev_net_ratio: 0.015, accel_ratio: 0.016 },
  ],
  sectors_down: [
    { name: '证券', net_daily: -1.2e8, prev_net_daily: -0.4e8, accel: -0.8e8, net_ratio: -0.02, prev_net_ratio: -0.008, accel_ratio: -0.012 },
  ],
  stocks_up: [
    { symbol: 'SH600056', name: '中国医药', sector: '医药商业', net_daily: 0.8e8, prev_net_daily: 0.2e8, accel: 0.6e8, net_ratio: 0.04, prev_net_ratio: 0.01, accel_ratio: 0.03 },
  ],
  stocks_down: [
    { symbol: 'SZ002487', name: '大金重工', sector: '风电设备', net_daily: -0.5e8, prev_net_daily: -0.1e8, accel: -0.4e8, net_ratio: -0.03, prev_net_ratio: -0.01, accel_ratio: -0.02 },
  ],
};

describe('FlowSpeedCard', () => {
  it('默认显示板块加速流入', () => {
    render(<FlowSpeedCard speed={speed} period="2d" />);
    expect(screen.getByText('资金流速率')).toBeInTheDocument();
    expect(screen.getByText('半导体')).toBeInTheDocument();
    expect(screen.getByText('+1.30亿')).toBeInTheDocument();
  });

  it('切到个股/加速流出', async () => {
    render(<FlowSpeedCard speed={speed} period="5d" />);
    await userEvent.click(screen.getByRole('button', { name: '个股' }));
    await userEvent.click(screen.getByRole('button', { name: /加速流出/ }));
    expect(screen.getByText('大金重工')).toBeInTheDocument();
    expect(screen.getByText('-0.40亿')).toBeInTheDocument();
  });
});
