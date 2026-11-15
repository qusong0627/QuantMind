import { render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import EquityLineChart from '../EquityLineChart';

const mocks = vi.hoisted(() => {
  const setData = vi.fn();
  const fitContent = vi.fn();
  const createChart = vi.fn(() => ({
    addSeries: vi.fn(() => ({ setData })),
    applyOptions: vi.fn(),
    timeScale: () => ({ fitContent }),
    remove: vi.fn(),
  }));
  return { setData, fitContent, createChart };
});

vi.mock('lightweight-charts', () => ({
  createChart: mocks.createChart,
  LineSeries: {},
  ColorType: { Solid: 'solid' },
}));

class ResizeObserverStub {
  observe() {}
  disconnect() {}
}

describe('EquityLineChart', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.stubGlobal('ResizeObserver', ResizeObserverStub);
  });

  it('renders an empty state when there are fewer than two points', () => {
    // Act
    render(<EquityLineChart points={[{ date: '2025-01-01', value: 100000 }]} />);

    // Assert
    expect(screen.getByText('无净值数据')).toBeTruthy();
    expect(mocks.createChart).not.toHaveBeenCalled();
  });

  it('feeds the equity points into the line series', () => {
    // Arrange
    const points = [
      { date: '2025-01-01', value: 100000 },
      { date: '2025-01-02', value: 101200 },
    ];

    // Act
    render(<EquityLineChart points={points} />);

    // Assert
    expect(mocks.createChart).toHaveBeenCalledTimes(1);
    expect(mocks.setData).toHaveBeenCalledWith([
      { time: '2025-01-01', value: 100000 },
      { time: '2025-01-02', value: 101200 },
    ]);
    expect(mocks.fitContent).toHaveBeenCalled();
  });

  it('creates the chart when data arrives after an empty first render', () => {
    // Arrange：首次不足 2 点走空态（容器不挂载），数据到位后必须真的建图
    const { rerender } = render(<EquityLineChart points={[]} />);
    expect(mocks.createChart).not.toHaveBeenCalled();

    // Act
    rerender(
      <EquityLineChart
        points={[
          { date: '2025-01-01', value: 100000 },
          { date: '2025-01-02', value: 100500 },
        ]}
      />,
    );

    // Assert
    expect(mocks.createChart).toHaveBeenCalledTimes(1);
    expect(mocks.setData).toHaveBeenCalledWith([
      { time: '2025-01-01', value: 100000 },
      { time: '2025-01-02', value: 100500 },
    ]);
  });
});
