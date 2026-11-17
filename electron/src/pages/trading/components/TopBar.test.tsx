import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import TopBar from './TopBar';

const fullAccount = {
  total_asset: 1234567,
  initial_equity: 1000000,
  day_open_equity: 1234567,
  month_open_equity: 1000000,
  cash: 1234567,
  market_value: 0,
  frozen: 0,
  daily_pnl: 0,
  daily_pnl_percent: 0,
  floating_pnl: 0,
  floating_pnl_percent: 0,
  total_pnl: 234567,
  total_pnl_percent: 0.234567,
  position_ratio: 0,
  position_count: 0,
};

describe('TopBar 账户概览', () => {
  it('无账户数据时用占位符而不是 0.00', () => {
    render(<TopBar isConnected={false} strategyStatus="stopped" />);
    expect(screen.getAllByText('—').length).toBeGreaterThan(3);
    expect(screen.queryByText('0.00')).not.toBeInTheDocument();
  });

  it('渲染连接、启动状态与编排方式', () => {
    render(<TopBar isConnected strategyStatus="starting" runMode="SIMULATION" orchestrationMode="docker" />);
    expect(screen.getByText('数据已同步')).toBeInTheDocument();
    expect(screen.getByText('正在启动')).toBeInTheDocument();
    expect(screen.getByText('docker')).toBeInTheDocument();
  });

  it('渲染正负盈亏，并对超配仓位做钳制', () => {
    render(
      <TopBar
        isConnected={false}
        strategyStatus="running"
        runMode="SIMULATION"
        orchestrationMode="k8s"
        accountInfo={{
          ...fullAccount,
          total_asset: 100,
          initial_equity: 100,
          cash: 0,
          market_value: 100,
          daily_pnl: 5,
          daily_pnl_percent: 0.05,
          floating_pnl: -3,
          floating_pnl_percent: -0.03,
          total_pnl: -8,
          total_pnl_percent: -0.08,
          position_ratio: 1.25,
          position_count: 1,
        }}
      />,
    );
    expect(screen.getByText('等待数据')).toBeInTheDocument();
    expect(screen.getByText('策略运行中')).toBeInTheDocument();
    expect(screen.getByText('k8s')).toBeInTheDocument();
    expect(screen.getAllByText('100.0%').length).toBe(2);
    expect(screen.getByText('0.0%')).toBeInTheDocument();
    expect(screen.getByText('+5.00')).toBeInTheDocument();
    expect(screen.getAllByText('-3.00').length).toBeGreaterThan(0);
    expect(screen.getAllByText('-8.00').length).toBeGreaterThan(0);
  });

  it('渲染总资产与累计盈亏', () => {
    render(<TopBar isConnected strategyStatus="stopped" accountInfo={fullAccount} />);
    expect(screen.getAllByText('1,234,567.00').length).toBe(2);
    expect(screen.getByText('+234,567.00')).toBeInTheDocument();
    expect(screen.getByText('策略待运行')).toBeInTheDocument();
  });

  it('按交易模式切换标题与账户标签', () => {
    const { unmount } = render(<TopBar isConnected={false} strategyStatus="stopped" tradingMode="simulation" />);
    expect(screen.getByText('模拟交易仪表盘')).toBeInTheDocument();
    expect(screen.getByText('模拟账户')).toBeInTheDocument();
    expect(screen.getByText('本地沙箱')).toBeInTheDocument();
    unmount();
    render(<TopBar isConnected={false} strategyStatus="stopped" tradingMode="real" />);
    expect(screen.getByText('实盘交易仪表盘')).toBeInTheDocument();
    expect(screen.getByText('实盘账户')).toBeInTheDocument();
  });
});
