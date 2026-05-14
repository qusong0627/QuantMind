import React from 'react';
import { useSelector } from 'react-redux';
import { selectCurrentTab } from '../../store/slices/aiStrategySlice';
import { ModuleGrid } from './ModuleGrid';
import { NewBacktestCenterPage } from '../../pages/NewBacktestCenterPage';
import { MarketWeatherBackground } from './MarketWeatherBackground';

const UserCenterPage = React.lazy(() => import('../../features/user-center/pages/UserCenterPage'));
const RealTradingPage = React.lazy(() => import('../../pages/trading/RealTradingPage'));
const QuantBotPage = React.lazy(() => import('../../features/quantbot/pages/QuantBotPage'));

interface DashboardLayoutProps {
  modules: any[];
  onLayoutChange: (layout: any[]) => void;
}

export const DashboardLayout: React.FC<DashboardLayoutProps> = ({ modules, onLayoutChange }) => {
  const activeTab = useSelector(selectCurrentTab);

  console.log('DashboardLayout: 当前activeTab', activeTab);
  console.log('DashboardLayout: 渲染内容区域，activeTab =', activeTab);

  const renderContent = () => {
    console.log('DashboardLayout: renderContent被调用，activeTab=', activeTab);
    switch (activeTab as any) {
      case 'dashboard':
        return <ModuleGrid modules={modules} onLayoutChange={onLayoutChange} />;
      case 'strategy':
        console.log('DashboardLayout: 策略页已切换至策略向导路由');
        return <ModuleGrid modules={modules} onLayoutChange={onLayoutChange} />;
      case 'backtest':
        console.log('DashboardLayout: 渲染回测中心组件');
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

  const showWeatherBackground = activeTab === 'dashboard' || activeTab === 'strategy';

  return (
    <div
      className="dashboard-layout w-full h-full p-0 relative z-0"
    >
      {/* 动态大盘天气背景层 - 仅在仪表盘页面显示 */}
      {showWeatherBackground && <MarketWeatherBackground />}
      
      {/* 内容层 - z-10 */}
      <div className="relative z-10 h-full w-full">
        {renderContent()}
      </div>
    </div>
  );
};
