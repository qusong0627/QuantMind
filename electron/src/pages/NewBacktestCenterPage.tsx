/**
 * 新版回测中心主页面
 *
 * 布局:1400×900 固定窗口
 * - 顶部工具栏 (60px)
 * - 左侧导航 (240px)
 * - 右侧内容区 (1160px, 可滚动)
 */

import React, { useEffect } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { BacktestSidebar } from '../components/backtestCenter/BacktestSidebar';
import { QlibQuickBacktest } from '../components/backtest/QlibQuickBacktest';
import { QlibExpertBacktest } from '../components/backtest/QlibExpertBacktest';
import { BacktestHistoryModule } from '../components/backtestCenter/BacktestHistoryModule';
import { StrategyComparisonModule } from '../components/backtestCenter/StrategyComparisonModule';
import { ParameterOptimizationModule } from '../components/backtestCenter/ParameterOptimizationModule';
import { StrategyManagementModule } from '../components/backtestCenter/StrategyManagementModule';
import { EnhancedAdvancedAnalysisModule } from '../components/backtestCenter/EnhancedAdvancedAnalysisModule';
import { useBacktestCenterStore, ModuleId } from '../stores/backtestCenterStore';
import { Bell } from 'lucide-react';
import { PAGE_LAYOUT } from '../config/pageLayout';

// 固定尺寸常量已移除，改为自适应布局

export const NewBacktestCenterPage: React.FC = () => {
  const { activeModule, setActiveModule } = useBacktestCenterStore();
  // 组件挂载时，仅在 activeModule 为空或无效时重置为默认模块
  useEffect(() => {
    const validModules: ModuleId[] = [
      'quick-backtest', 'expert-mode', 'backtest-history',
      'strategy-compare', 'parameter-optimize', 'strategy-management', 'advanced-analysis'
    ];
    if (!validModules.includes(activeModule)) {
      setActiveModule('quick-backtest');
    }
  }, [activeModule, setActiveModule]);

  // 渲染对应模块内容
  const renderModuleContent = () => {
    switch (activeModule) {
      case 'quick-backtest':
        return <QlibQuickBacktest />;
      case 'expert-mode':
        return <QlibExpertBacktest />;
      case 'backtest-history':
        return <BacktestHistoryModule />;
      case 'strategy-compare':
        return <StrategyComparisonModule />;
      case 'parameter-optimize':
        return <ParameterOptimizationModule />;
      case 'strategy-management':
        return <StrategyManagementModule />;
      case 'advanced-analysis':
        return <EnhancedAdvancedAnalysisModule />;
      default:
        return <QlibQuickBacktest />;
    }
  };

  // 获取面包屑路径
  const getBreadcrumb = () => {
    const moduleNames: Record<ModuleId, string> = {
      'quick-backtest': '快速回测',
      'expert-mode': '专家模式',
      'backtest-history': '回测历史',
      'strategy-compare': '策略对比',
      'parameter-optimize': '参数优化',
      'strategy-management': '策略管理',
      'advanced-analysis': '高级分析',
    };
    return ['回测中心', moduleNames[activeModule]];
  };

  return (
    <div className={PAGE_LAYOUT.outerClass}>
      <div className={PAGE_LAYOUT.frameClass}>
        {/* 顶部工具栏 */}
        <header
          className={PAGE_LAYOUT.headerClass}
          style={{ height: `${PAGE_LAYOUT.headerHeight}px` }}
        >
          <div className="flex items-center gap-4">
            {/* Logo和标题 */}
            <div className="flex items-center gap-3">
              <div className="w-10 h-10 bg-gradient-to-br from-blue-500 to-purple-500 rounded-2xl flex items-center justify-center shadow-lg">
                <span className="text-white font-bold text-lg">Q</span>
              </div>
              <div className="flex items-center gap-2.5 ml-1">
                <h1 className="text-xl font-bold text-slate-800 tracking-tight">QuantMind</h1>
                <div className="h-4 w-[1px] bg-slate-200 self-center" />
                <span className="text-sm font-medium text-slate-500">回测中心</span>
              </div>
            </div>
          </div>

          {/* 右侧工具按钮 */}
          <div className="flex items-center gap-2">
            <button className="p-2 hover:bg-gray-100 rounded-2xl transition-colors">
              <Bell className="w-5 h-5 text-gray-600" />
            </button>
          </div>
        </header>

        {/* 主内容区域 */}
        <div className="flex flex-1 overflow-hidden">
          {/* 左侧导航 */}
            <BacktestSidebar
            width={PAGE_LAYOUT.sidebarWidth}
            activeModule={activeModule}
            onModuleChange={setActiveModule}
          />

          {/* 右侧内容区 */}
          <main className="flex-1 flex flex-col bg-gray-50/50 min-w-0">
            {/* 面包屑导航 */}
            <div className={PAGE_LAYOUT.breadcrumbClass}>
              <div className="flex items-center gap-2 text-sm">
                {getBreadcrumb().map((item, index) => (
                  <React.Fragment key={index}>
                    {index > 0 && <span className="text-gray-400">/</span>}
                    <span className={index === getBreadcrumb().length - 1 ? 'text-gray-800 font-medium' : 'text-gray-500'}>
                      {item}
                    </span>
                  </React.Fragment>
                ))}
              </div>
            </div>

            {/* 内容区域（可滚动） */}
              <div
              className={`${PAGE_LAYOUT.scrollContainerClass} ${activeModule === 'parameter-optimize' ? 'overflow-hidden' : 'overflow-y-auto overflow-x-hidden'}`}
              style={{
                scrollbarWidth: 'thin',
                scrollbarColor: '#cbd5e1 #f1f5f9'
              }}
            >
              <div className={`${activeModule === 'parameter-optimize' ? 'h-full' : PAGE_LAYOUT.contentOuterClass} h-full`}>
                <AnimatePresence mode="wait">
                  <motion.div
                    key={activeModule}
                    initial={{ opacity: 0, x: 20 }}
                    animate={{ opacity: 1, x: 0 }}
                    exit={{ opacity: 0, x: -20 }}
                    transition={{ duration: 0.2 }}
                    className="h-full"
                  >
                    {renderModuleContent()}
                  </motion.div>
                </AnimatePresence>
              </div>
            </div>
          </main>
        </div>
      </div>
    </div>
  );
};
