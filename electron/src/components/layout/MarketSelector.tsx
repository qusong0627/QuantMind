import React, { startTransition } from 'react';
import { motion } from 'framer-motion';
import { useAppDispatch, useAppSelector } from '../../store';
import { setMarket, selectCurrentMarket, type AppMarket } from '../../store/slices/uiSlice';
import { isMarketEnabled } from '../../config/marketFlags';
import { notifySharedAdminMarket, type SharedAdminMarket } from '../../features/admin/hooks/useSharedAdminMarket';

interface MarketOption {
  id: AppMarket;
  label: string;
  color: string;
  bgColor: string;
  borderColor: string;
}

const MARKET_OPTIONS: MarketOption[] = (
  [
    { id: 'CN', label: 'A股', color: 'text-red-600', bgColor: 'bg-red-50', borderColor: 'border-red-200' },
    { id: 'HK', label: '港股', color: 'text-orange-600', bgColor: 'bg-orange-50', borderColor: 'border-orange-200' },
    { id: 'US', label: '美股', color: 'text-blue-600', bgColor: 'bg-blue-50', borderColor: 'border-blue-200' },
    { id: 'CRYPTO', label: '区块链', color: 'text-purple-600', bgColor: 'bg-purple-50', borderColor: 'border-purple-200' },
    { id: 'FUTURES', label: '期货', color: 'text-amber-600', bgColor: 'bg-amber-50', borderColor: 'border-amber-200' },
    { id: 'CUSTOM', label: '自定义', color: 'text-cyan-600', bgColor: 'bg-cyan-50', borderColor: 'border-cyan-200' },
  ] as MarketOption[]
).filter((m) => isMarketEnabled(m.id));

export const MarketSelector: React.FC = () => {
  const dispatch = useAppDispatch();
  const currentMarket = useAppSelector(selectCurrentMarket);

  // 当前市场被屏蔽时，回落 A股（避免选中的 CRYPTO 无高亮）
  const activeMarket = isMarketEnabled(currentMarket) ? currentMarket : 'CN';

  return (
    <motion.div
      initial={{ opacity: 0, scale: 0.95 }}
      animate={{ opacity: 1, scale: 1 }}
      className="flex items-center"
    >
      <div
        className="relative flex h-8 items-center rounded-full border border-slate-200/80 bg-white/68 backdrop-blur-md shadow-sm"
        role="radiogroup"
        aria-label="市场切换"
      >
        {MARKET_OPTIONS.map((market) => {
          const isActive = activeMarket === market.id;
          return (
            <button
              key={market.id}
              type="button"
              role="radio"
              aria-checked={isActive}
              // 数据订阅者多（~30 个），用并发切换：胶囊动画 urgent 先画，数据渲染不阻塞动画帧
              // 同时广播给管理后台（训练服务/模型管理跟随）。
              onClick={() => startTransition(() => { dispatch(setMarket(market.id)); notifySharedAdminMarket(market.id as SharedAdminMarket); })}
              className={`relative z-10 flex items-center justify-center px-3 h-full text-[11px] font-bold tracking-tight transition-colors ${
                isActive ? 'text-slate-800' : 'text-slate-400 hover:text-slate-600'
              }`}
            >
              {isActive && (
                <motion.div
                  layoutId="market-active-bg"
                  className={`absolute inset-0.5 rounded-full ${market.bgColor} border ${market.borderColor} shadow-sm`}
                  transition={{ type: 'spring', bounce: 0.15, duration: 0.3 }}
                />
              )}
              <span className="relative z-10">{market.label}</span>
            </button>
          );
        })}
      </div>
    </motion.div>
  );
};
