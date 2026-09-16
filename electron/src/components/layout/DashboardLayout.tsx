import React from 'react';
import { useSelector } from 'react-redux';
import { selectCurrentTab } from '../../store/slices/aiStrategySlice';
import { ModuleGrid } from './ModuleGrid';
import { NewBacktestCenterPage } from '../../pages/NewBacktestCenterPage';
import { MarketWeatherBackground } from './MarketWeatherBackground';
import { ComplianceFooter } from '../shared/compliance/ComplianceChrome';

const UserCenterPage = React.lazy(() => import('../../features/user-center/pages/UserCenterPage'));
const RealTradingPage = React.lazy(() => import('../../pages/trading/RealTradingPage'));
const QuantBotPage = React.lazy(() => import('../../features/quantbot/pages/QuantBotPage'));

interface DashboardLayoutProps {
  modules: any[];
  onLayoutChange: (layout: any[]) => void;
}

export const DashboardLayout: React.FC<DashboardLayoutProps> = ({ modules, onLayoutChange }) => {
  const activeTab = useSelector(selectCurrentTab);

  const renderContent = () => {
    switch (activeTab as any) {
      case 'dashboard':
        return <ModuleGrid modules={modules} onLayoutChange={onLayoutChange} />;
      case 'backtest':
        return (
          <div className="w-full h-full">
            <NewBacktestCenterPage />
          </div>
        );
      case 'agent':
        return (
          <React.Suspense fallback={<div className="w-full h-full flex items-center justify-center" />}>
            <div className="w-full h-full flex items-center justify-center">
              <QuantBotPage />
            </div>
          </React.Suspense>
        );
      case 'trading':
        return (
          <React.Suspense fallback={<div className="w-full h-full" />}>
            <div className="w-full h-full">
              <RealTradingPage />
            </div>
          </React.Suspense>
        );
      case 'profile':
        return (
          <React.Suspense fallback={<div className="w-full h-full flex items-center justify-center" />}>
            <div className="w-full h-full flex items-center justify-center">
              <UserCenterPage />
            </div>
          </React.Suspense>
        );
      default:
        return <ModuleGrid modules={modules} onLayoutChange={onLayoutChange} />;
    }
  };

  const showWeatherBackground = activeTab === 'dashboard';

  return (
    <div
      className="dashboard-layout w-full h-full p-0 relative z-0"
    >
      {/* 动态大盘天气背景层 - 仅在仪表盘页面显示 */}
      {showWeatherBackground && <MarketWeatherBackground />}

      {/* 内容层 - z-10；T-FE-17 免责页脚仅在仪表盘首页常驻 */}
      <div className="relative z-10 h-full w-full flex flex-col">
        <div className="flex-1 min-h-0">
          {renderContent()}
        </div>
        {activeTab === 'dashboard' && (
          <div className="shrink-0 px-4 pb-0.5">
            <ComplianceFooter />
          </div>
        )}
      </div>
    </div>
  );
};
