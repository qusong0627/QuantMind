import React from 'react';
import { Card } from '../common/Card';
import { FundOverviewSkeleton } from '../common/CardSkeletons';
import { motion } from 'framer-motion';
import { useFundData } from '../../hooks/useFundData';
import { FundData } from '../../services/userService';


const formatMoney = (value: number): string =>
  value.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });

const formatSignedMoney = (value: number): string => {
  const sign = value >= 0 ? '+' : '';
  return `${sign}￥${formatMoney(Math.abs(value))}`;
};

export const FundOverviewCard: React.FC = () => {
  const { data, loading, error, isSimulated, tradingMode } = useFundData({
    autoRefresh: true,
    refreshInterval: 5000 // 实时数据刷新，间隔缩短
  });

  const cardTitle = tradingMode === 'real' ? '资金概览 (实盘账户)' : '资金概览 (模拟账户)';

  if (loading && !data) {
    return <FundOverviewSkeleton />;
  }

  if (error && !data) {
    return (
      <Card title={cardTitle} background="fund" height="100%">
        <div className="flex flex-col items-center justify-center h-full">
          <div className="text-[var(--error)] text-sm mb-2">数据加载失败</div>
          <div className="text-xs text-[var(--text-tertiary)]">系统将自动重试...</div>
        </div>
      </Card>
    );
  }

  const fallbackFundInfo: FundData = {
    totalAsset: 0,
    availableBalance: 0,
    frozenBalance: 0,
    todayPnL: 0,
    dailyReturn: 0,
    totalPnL: 0,
    totalReturn: 0,
    initialCapital: 100000,
    initialCapitalEstimated: false,
    winRate: 0,
    maxDrawdown: 0,
    sharpeRatio: 0,
    monthlyPnL: undefined,
    todayPnLAvailable: true,
    dailyReturnAvailable: true,
    totalPnLAvailable: true,
    totalReturnAvailable: true,
    monthlyPnLAvailable: false,
    metricsSource: 'fund_card_fallback',
    accountOnline: undefined,
    lastUpdate: new Date().toISOString(),
  };
  const fundInfo: FundData = data || fallbackFundInfo;

  const monthlyPnL = typeof fundInfo.monthlyPnL === 'number' ? fundInfo.monthlyPnL : null;
  const dailyReturn = Number.isFinite(fundInfo.dailyReturn) ? fundInfo.dailyReturn : 0;
  const returnRate = Number.isFinite(fundInfo.totalReturn) ? fundInfo.totalReturn : 0;
  const isRealAccountOffline = tradingMode === 'real' && !isSimulated && fundInfo.accountOnline === false;
  const todayPnLAvailable = fundInfo.todayPnLAvailable !== false;
  const dailyReturnAvailable = fundInfo.dailyReturnAvailable !== false;
  const totalPnLAvailable = fundInfo.totalPnLAvailable !== false;
  const totalReturnAvailable = fundInfo.totalReturnAvailable !== false;
  const monthlyPnLAvailable = fundInfo.monthlyPnLAvailable !== false && monthlyPnL !== null;
  const initialCapitalLabel = tradingMode === 'real'
    ? (fundInfo.initialCapitalEstimated ? '初始权益(估算)' : '初始权益')
    : '初始资金';

  return (
    <Card title={cardTitle} background="fund" height="100%">
      <div className="h-full min-h-0 flex flex-col relative">
        {/* 顶部总资产 - 增大字号且微调间距 */}
        <div className="text-center mb-1 mt-[-4px]">
          <motion.div
            className="text-5xl font-black text-slate-800 tracking-tight"
            initial={{ scale: 0.9, opacity: 0 }}
            animate={{ scale: 1, opacity: 1 }}
            transition={{ type: 'spring', stiffness: 200, damping: 15 }}
            key={fundInfo.totalAsset}
            style={{ fontFamily: 'Outfit, sans-serif' }}
          >
            ￥{formatMoney(fundInfo.totalAsset)}
          </motion.div>
          <div className="text-[10px] font-bold text-slate-400 uppercase tracking-widest leading-none mt-0.5">Total Net Asset Value</div>
        </div>

        {/* 6个数据项，分3排显示 - 居中且增加高度 */}
        <div className="flex-1 min-h-0 space-y-1.5 flex flex-col justify-center py-1">
          {/* 第1排：初始权益、今日盈亏 */}
          <div className="grid grid-cols-2 gap-2">
            <div title="统一基线口径，对应账户基线 initial_equity。" className="bg-slate-50 border border-slate-100 rounded-2xl p-3.5 hover:bg-slate-100 transition-colors flex flex-col items-center justify-center text-center">
              <div className="text-[10px] font-bold text-slate-400 uppercase tracking-wider mb-0.5">{initialCapitalLabel}</div>
              <div className="text-lg font-black text-slate-800 font-mono leading-tight">￥{fundInfo.initialCapital ? formatMoney(fundInfo.initialCapital) : '100,000.00'}</div>
            </div>
            <div title="统一日账本口径，对应 daily_pnl / today_pnl。" className="bg-slate-50 border border-slate-100 rounded-2xl p-3.5 hover:bg-slate-100 transition-colors flex flex-col items-center justify-center text-center">
              <div className="text-[10px] font-bold text-slate-400 uppercase tracking-wider mb-0.5">今日盈亏</div>
              {!todayPnLAvailable ? (
                <div className="text-lg font-black text-slate-300 font-mono leading-tight">--</div>
              ) : (
                <div className={`text-lg font-black font-mono leading-tight ${fundInfo.todayPnL >= 0 ? 'text-[var(--profit-primary)]' : 'text-[var(--loss-primary)]'}`}>
                  {formatSignedMoney(fundInfo.todayPnL)}
                </div>
              )}
            </div>
          </div>

          {/* 第2排：本月盈亏、总盈亏 */}
          <div className="grid grid-cols-2 gap-2">
            <div title="按月初权益基线推导的本月累计盈亏。" className="bg-slate-50 border border-slate-100 rounded-2xl p-3.5 hover:bg-slate-100 transition-colors flex flex-col items-center justify-center text-center">
              <div className="text-[10px] font-bold text-slate-400 uppercase tracking-wider mb-0.5">本月盈亏</div>
              {!monthlyPnLAvailable ? (
                <div className="text-lg font-black text-slate-300 font-mono leading-tight">--</div>
              ) : (
                <div className={`text-lg font-black font-mono leading-tight ${monthlyPnL >= 0 ? 'text-[var(--profit-primary)]' : 'text-[var(--loss-primary)]'}`}>
                  {formatSignedMoney(monthlyPnL)}
                </div>
              )}
            </div>
            <div title="统一账户累计盈亏口径，对应 total_pnl。" className="bg-slate-50 border border-slate-100 rounded-2xl p-3.5 hover:bg-slate-100 transition-colors flex flex-col items-center justify-center text-center">
              <div className="text-[10px] font-bold text-slate-400 uppercase tracking-wider mb-0.5">总盈亏</div>
              {!totalPnLAvailable ? (
                <div className="text-lg font-black text-slate-300 font-mono leading-tight">--</div>
              ) : (
                <div className={`text-lg font-black font-mono leading-tight ${(fundInfo.totalPnL || 0) >= 0 ? 'text-[var(--profit-primary)]' : 'text-[var(--loss-primary)]'}`}>
                  {formatSignedMoney(fundInfo.totalPnL || 0)}
                </div>
              )}
            </div>
          </div>

          {/* 第3排：日收益率、总收益率 */}
          <div className="grid grid-cols-2 gap-2">
            <div title="统一日收益率口径，对应 daily_return_pct / daily_return_ratio。" className="bg-slate-50 border border-slate-100 rounded-2xl p-3.5 hover:bg-slate-100 transition-colors flex flex-col items-center justify-center text-center">
              <div className="text-[10px] font-bold text-slate-400 uppercase tracking-wider mb-0.5">日收益率</div>
              {!dailyReturnAvailable ? (
                <div className="text-lg font-black text-slate-300 font-mono leading-tight">--</div>
              ) : (
                <div className={`text-lg font-black font-mono leading-tight ${dailyReturn >= 0 ? 'text-[var(--profit-primary)]' : 'text-[var(--loss-primary)]'}`}>
                  {dailyReturn >= 0 ? '+' : ''}{dailyReturn.toFixed(2)}%
                </div>
              )}
            </div>
            <div title="统一累计收益率口径，对应 total_return_pct / total_return_ratio。" className="bg-slate-50 border border-slate-100 rounded-2xl p-3.5 hover:bg-slate-100 transition-colors flex flex-col items-center justify-center text-center">
              <div className="text-[10px] font-bold text-slate-400 uppercase tracking-wider mb-0.5">总收益率</div>
              {!totalReturnAvailable ? (
                <div className="text-lg font-black text-slate-300 font-mono leading-tight">--</div>
              ) : (
                <div className={`text-lg font-black font-mono leading-tight ${returnRate >= 0 ? 'text-[var(--profit-primary)]' : 'text-[var(--loss-primary)]'}`}>
                  {returnRate >= 0 ? '+' : ''}{returnRate.toFixed(2)}%
                </div>
              )}
            </div>
          </div>
        </div>



      </div>
    </Card>
  );
};
