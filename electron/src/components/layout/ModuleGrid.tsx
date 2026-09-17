import React, { useEffect, useState } from 'react';
import { HeaderBar } from './HeaderBar';
import { MarketOverviewCard } from '../modules/MarketOverviewCard';
import { FundOverviewCard } from '../modules/FundOverviewCard';
import { TradeRecordsCard } from '../modules/TradeRecordsCard';
import { StrategyMonitorCard } from '../modules/StrategyMonitorCard';
import IntelligenceChartsCard from '../modules/IntelligenceChartsCard';
import { NotificationQuickCard } from '../modules/NotificationQuickCard';
import { AnimatePresence, motion } from 'framer-motion';
import { useAppSelector } from '../../store';
import { selectCurrentMarket } from '../../store/slices/uiSlice';

const moduleComponents: { [key: string]: React.ComponentType } = {
  market: MarketOverviewCard,
  fund: FundOverviewCard,
  trade: TradeRecordsCard,
  strategy: StrategyMonitorCard,
  charts: IntelligenceChartsCard,
  'ai-quick': NotificationQuickCard,
};

interface ModuleGridProps {
  modules: { id: string; component: string }[];
  onLayoutChange: (layout: any[]) => void;
}

export const ModuleGrid: React.FC<ModuleGridProps> = ({ modules, onLayoutChange }) => {
  const [strategyExpanded, setStrategyExpanded] = useState(false);
  const [notificationExpanded, setNotificationExpanded] = useState(false);
  const hasAnyExpanded = strategyExpanded || notificationExpanded;
  const currentMarket = useAppSelector(selectCurrentMarket);

  useEffect(() => {
    if (!hasAnyExpanded) {
      return;
    }

    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setStrategyExpanded(false);
        setNotificationExpanded(false);
      }
    };
    document.addEventListener('keydown', onKeyDown);

    return () => {
      document.body.style.overflow = previousOverflow;
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [hasAnyExpanded]);

  const renderModule = (moduleId: string) => {
    if (moduleId === 'strategy') {
      return (
        <StrategyMonitorCard
          expanded={false}
          onExpand={() => setStrategyExpanded(true)}
        />
      );
    }

    if (moduleId === 'ai-quick') {
      return (
        <NotificationQuickCard
          expanded={false}
          onExpand={() => setNotificationExpanded(true)}
        />
      );
    }

    const Component = moduleComponents[moduleId];
    return Component ? <Component /> : null;
  };

  return (
    <div className="w-full h-full relative overflow-hidden flex flex-col">
      {/* 顶部标题栏 */}
      <HeaderBar />

      <div
        className="grid gap-5 px-6 pb-20 flex-1 min-h-0 grid-cols-1 min-[820px]:grid-cols-2 xl:grid-cols-3"
        style={{
          gridAutoRows: 'minmax(340px, 1fr)',
          overflowY: 'auto'
        }}
      >
        {modules.map((module) => {
          return (
            <motion.div
              key={`${module.id}-${currentMarket}`}
              initial={{ opacity: 0, scale: 0.98 }}
              animate={{ opacity: 1, scale: 1 }}
              transition={{ duration: 0.4 }}
              className="flex flex-col h-full min-w-0"
            >
              {renderModule(module.component)}
            </motion.div>
          );
        })}
      </div>

      {/* 现代化的全屏弹出层组件 */}
      <AnimatePresence>
        {hasAnyExpanded && (
          <>
            <motion.div
              className="absolute inset-0 z-40 bg-slate-900/10 backdrop-blur-[12px]"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              onClick={() => {
                setStrategyExpanded(false);
                setNotificationExpanded(false);
              }}
            />

            <div className="absolute inset-0 z-50 flex items-center justify-center p-8 pointer-events-none">
              <motion.div
                className="pointer-events-auto rounded-[32px] border border-white/60 bg-white/70 backdrop-blur-3xl shadow-[0_32px_80px_rgba(0,0,0,0.1)] overflow-hidden"
                style={{
                  width: 'min(70vw, 1000px)',
                  height: 'min(85vh, 850px)',
                }}
                initial={{ opacity: 0, scale: 0.9, y: 30 }}
                animate={{ opacity: 1, scale: 1, y: 0 }}
                exit={{ opacity: 0, scale: 0.92, y: 15 }}
                transition={{ type: "spring", damping: 25, stiffness: 200 }}
              >
                {strategyExpanded && (
                  <StrategyMonitorCard
                    expanded
                    onCloseExpand={() => setStrategyExpanded(false)}
                  />
                )}
                {notificationExpanded && (
                  <NotificationQuickCard
                    expanded
                    onCloseExpand={() => setNotificationExpanded(false)}
                  />
                )}
              </motion.div>
            </div>
          </>
        )}
      </AnimatePresence>
    </div>
  );
};
