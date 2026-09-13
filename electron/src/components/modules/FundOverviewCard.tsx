import React from 'react';
import { Card } from '../common/Card';
import { FundOverviewSkeleton } from '../common/CardSkeletons';
import { motion } from 'framer-motion';
import { message } from 'antd';
import { useFundData } from '../../hooks/useFundData';
import { FundData } from '../../services/userService';
import {
  BoxPlaceholder,
  MarketChip,
  formatBoxTitle,
  useBoxContent,
  useMarketContent,
  useOpenSimAccount,
} from '../../features/dashboard-shared';

/** 金额格式化：货币符号来自市场内容规格（A股 ¥ / 港股 HK$ / 美股 $ …） */
const formatMoney = (value: number): string =>
  value.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });

const formatSignedMoney = (value: number, currency: string): string => {
  const sign = value > 0 ? '+' : value < 0 ? '-' : '';
  return `${sign}${currency}${formatMoney(Math.abs(value))}`;
};

export const FundOverviewCard: React.FC = () => {
  // 市场内容规格（标题/货币/空态文案）与当前市场，全部来自共享层
  const { market, content } = useBoxContent('fund');
  const marketContent = useMarketContent();
  const { openAccount, opening } = useOpenSimAccount();

  const { data, loading, error, isSimulated, notInitialized, tradingMode, refresh } = useFundData({
    autoRefresh: true,
    refreshInterval: 5000, // 实时数据刷新，间隔缩短
    market,
  });

  const modeLabel = tradingMode === 'real' ? '实盘' : '模拟';
  const currency = content.currency || '¥';
  const cardTitle = formatBoxTitle(content, { label: marketContent.label, mode: modeLabel });
  // 实盘账户是账户级（后端 /account 无 market 维度），切换市场不会换账户，必须明示
  const isAccountLevelReal = tradingMode === 'real' && !isSimulated;

  const handleOpenAccount = async () => {
    const result = await openAccount(market, marketContent.defaultSeedCash);
    if (result.ok) {
      message.success(`${marketContent.label}${result.message}`);
      await refresh();
    } else {
      message.error(result.message);
    }
  };

  if (loading && !data && !notInitialized) {
    return <FundOverviewSkeleton />;
  }

  const fallbackFundInfo: FundData = {
    totalAsset: 0,
    availableBalance: 0,
    frozenBalance: 0,
    todayPnL: 0,
    dailyReturn: 0,
    totalPnL: 0,
    totalReturn: 0,
    initialCapital: 0,
    initialCapitalAvailable: false,
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
  const initialCapitalAvailable = fundInfo.initialCapitalAvailable !== false;
  const todayPnLAvailable = fundInfo.todayPnLAvailable !== false;
  const dailyReturnAvailable = fundInfo.dailyReturnAvailable !== false;
  const totalPnLAvailable = fundInfo.totalPnLAvailable !== false;
  const totalReturnAvailable = fundInfo.totalReturnAvailable !== false;
  const monthlyPnLAvailable = fundInfo.monthlyPnLAvailable !== false && monthlyPnL !== null;
  const initialCapitalLabel = tradingMode === 'real'
    ? (fundInfo.initialCapitalEstimated ? '初始权益(估算)' : '初始权益')
    : '初始资金';

  // 未开通该市场模拟盘（或加载失败）：渲染空态，绝不回落到其它市场账户
  if (!loading && (notInitialized || (error && !data))) {
    return (
      <Card title={cardTitle} background="fund" height="100%">
        <div className="h-full min-h-0 flex flex-col">
          <div className="flex items-center justify-between mb-1">
            <MarketChip market={market} source={content.source} />
          </div>
          <BoxPlaceholder
            content={content}
            state={{ loading: false, hasData: false, notInitialized, error: notInitialized ? null : error }}
            onRetry={refresh}
            onOpenAccount={handleOpenAccount}
            opening={opening}
          />
        </div>
      </Card>
    );
  }

  return (
    <Card title={cardTitle} background="fund" height="100%">
      <div className="h-full min-h-0 flex flex-col relative">
        <div className="flex items-center justify-between mb-0.5">
          <MarketChip market={market} source={content.source} />
          {isAccountLevelReal && (
            <span className="text-[9px] text-slate-400" title="实盘账户为账户级快照，不按市场拆分">
              实盘账户（账户级）
            </span>
          )}
        </div>
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
            {currency}{formatMoney(fundInfo.totalAsset)}
          </motion.div>
          <div className="text-[10px] font-bold text-slate-400 uppercase tracking-widest leading-none mt-0.5">Total Net Asset Value</div>
        </div>

        {/* 6个数据项，分3排显示 - 居中且增加高度 */}
        <div className="flex-1 min-h-0 space-y-1.5 flex flex-col justify-center py-1">
          {/* 第1排：初始权益、今日盈亏 */}
          <div className="grid grid-cols-2 gap-2">
            <div title="统一基线口径，对应账户基线 initial_equity。" className="bg-slate-50 border border-slate-100 rounded-2xl p-3.5 hover:bg-slate-100 transition-colors flex flex-col items-center justify-center text-center">
              <div className="text-[10px] font-bold text-slate-400 uppercase tracking-wider mb-0.5">{initialCapitalLabel}</div>
              {!initialCapitalAvailable ? (
                <div className="text-lg font-black text-slate-300 font-mono leading-tight">--</div>
              ) : (
                <div className="text-lg font-black text-slate-800 font-mono leading-tight">{currency}{formatMoney(fundInfo.initialCapital)}</div>
              )}
            </div>
            <div title="统一日账本口径，对应 daily_pnl / today_pnl。" className="bg-slate-50 border border-slate-100 rounded-2xl p-3.5 hover:bg-slate-100 transition-colors flex flex-col items-center justify-center text-center">
              <div className="text-[10px] font-bold text-slate-400 uppercase tracking-wider mb-0.5">今日盈亏</div>
              {!todayPnLAvailable ? (
                <div className="text-lg font-black text-slate-300 font-mono leading-tight">--</div>
              ) : (
                <div className={`text-lg font-black font-mono leading-tight ${fundInfo.todayPnL >= 0 ? 'text-[var(--profit-primary)]' : 'text-[var(--loss-primary)]'}`}>
                  {formatSignedMoney(fundInfo.todayPnL, currency)}
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
                  {formatSignedMoney(monthlyPnL, currency)}
                </div>
              )}
            </div>
            <div title="统一账户累计盈亏口径，对应 total_pnl。" className="bg-slate-50 border border-slate-100 rounded-2xl p-3.5 hover:bg-slate-100 transition-colors flex flex-col items-center justify-center text-center">
              <div className="text-[10px] font-bold text-slate-400 uppercase tracking-wider mb-0.5">总盈亏</div>
              {!totalPnLAvailable ? (
                <div className="text-lg font-black text-slate-300 font-mono leading-tight">--</div>
              ) : (
                <div className={`text-lg font-black font-mono leading-tight ${(fundInfo.totalPnL || 0) >= 0 ? 'text-[var(--profit-primary)]' : 'text-[var(--loss-primary)]'}`}>
                  {formatSignedMoney(fundInfo.totalPnL || 0, currency)}
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
