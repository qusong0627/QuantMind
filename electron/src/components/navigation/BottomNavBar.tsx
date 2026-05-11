import React from 'react';
import { DashboardTab } from '../../state/atoms';
import { clsx } from 'clsx';

const tabs: { key: DashboardTab; label: string }[] = [
  { key: 'dashboard', label: '仪表盘' },
  { key: 'strategy', label: '智能策略' },
  { key: 'backtest', label: '策略回测' },
  { key: 'trading', label: '实盘交易' },
  { key: 'profile', label: '个人中心' }
];

interface Props {
  current: DashboardTab;
  onChange: (tab: DashboardTab) => void;
}

export const BottomNavBar: React.FC<Props> = ({ current, onChange }) => {
  return (
    <nav className="bottom-nav glass-blur">
      {tabs.map(t => (
        <button
          key={t.key}
            className={clsx('nav-btn', current === t.key && 'active')}
          onClick={() => onChange(t.key)}
        >
          {t.label}
        </button>
      ))}
    </nav>
  );
};
