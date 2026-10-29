/** 成交量 × 主力加权系数：曲线渲染、缺失不显示成 0、60/120 日切换。 */
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import React from 'react';
import { VolumeForceChart } from '../components/VolumeForceChart';
import type { SentimentVolume } from '../services/sentimentService';

const dates = Array.from(
  { length: 70 },
  (_, i) => `2026-${String(7 + Math.floor(i / 28)).padStart(2, '0')}-${String((i % 28) + 1).padStart(2, '0')}`,
);
const volume: SentimentVolume = {
  dates,
  turnover_yi: dates.map((_, i) => 12000 + i * 10),
  vw_breadth: dates.map((_, i) => (i % 2 ? 0.4 : -0.2)),
  vw_breadth_pct: dates.map(() => 0.55),
  main_net_ratio: dates.map(() => 0.0075),
  main_net_yi: dates.map(() => 121.9),
  main_breadth: dates.map(() => 0.65),
  main_breadth_amt: dates.map(() => 0.63),
  latest: {
    date: '2026-10-28',
    turnover_yi: 16421,
    vw_breadth: 0.5693,
    main_net_yi: 121.91,
    main_net_ratio: 0.0075,
    main_net_z20: 0.551,
    main_breadth: 0.6506,
    main_breadth_amt: 0.6286,
  },
};

describe('VolumeForceChart', () => {
  it('渲染成交额/主力加权系数与曲线', () => {
    const { container } = render(<VolumeForceChart volume={volume} />);
    expect(screen.getByText('成交量 × 主力加权系数')).toBeInTheDocument();
    expect(screen.getByText('成交额加权系数')).toBeInTheDocument();
    expect(screen.getByText('主力净额 / 成交额')).toBeInTheDocument();
    expect(screen.getByText('+0.569')).toBeInTheDocument();
    expect(container.querySelectorAll('path').length).toBeGreaterThanOrEqual(2);
    expect(container.querySelectorAll('rect').length).toBeGreaterThan(10);
  });

  it('切到近 120 日不报错', async () => {
    render(<VolumeForceChart volume={volume} />);
    await userEvent.click(screen.getByRole('button', { name: '近120日' }));
    expect(screen.getByText('近120日')).toBeInTheDocument();
  });
});
