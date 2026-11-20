// Triggering rebuild for mode-aware charts
import React from 'react';
import { Card } from '../common/Card';
import { motion } from 'framer-motion';
import { useWebSocket } from '../../contexts/WebSocketContext';
import { Activity, Wifi, WifiOff } from 'lucide-react';
import { EChartsChart } from '../common/EChartsChart';
import { getChartOption } from '../../utils/chartOptions';
import { useIntelligenceCharts } from '../../hooks/useIntelligenceCharts';
import { useMarketAccountPresence } from '../../hooks/useMarketAccountPresence';
import { useAppSelector } from '../../store';
import { selectCurrentMarket } from '../../store/slices/uiSlice';
import { getMarketConfig } from '../../config/marketConfig';
import { formatBackendTime } from '../../utils/format';

// 图表区域占位 shimmer
const ChartShimmer: React.FC<{ className?: string }> = ({ className = '' }) => (
  <div className={`animate-pulse bg-gray-100/80 rounded-lg ${className}`} />
);

const IntelligenceChartsCard: React.FC = () => {
  const tradingMode = useAppSelector((state) => state.ui.tradingMode);
  const currentMarket = useAppSelector(selectCurrentMarket);
  const marketLabel = getMarketConfig(currentMarket).label;
  const { present: marketActive, loading: presenceLoading } = useMarketAccountPresence(currentMarket);
  const {
    data: chartData,
    loading,
    error,
    isStale,
    lastUpdatedAt,
    hasDailyReturn,
    hasTradeCount,
    hasPositionRatio,
  } = useIntelligenceCharts('current', { tradingMode });
  const { isConnected, status } = useWebSocket();

  const tradeStats = chartData.tradeStats;
  // 盈亏比展示：无亏损平仓且有盈利时展示 ∞；否则取平均盈利/平均亏损
  const profitLossRatioText = tradeStats
    ? (tradeStats.profitLossRatio > 0
        ? tradeStats.profitLossRatio.toFixed(2)
        : (tradeStats.winTrades > 0 ? '∞' : '--'))
    : '--';

  const formatTimestamp = (timeStr: string | null) => {
    return formatBackendTime(timeStr, { withSeconds: true });
  };

  return (
    <Card title="智能图表" height="100%" background="charts">
      {!presenceLoading && !marketActive ? (
        <div className="flex flex-col items-center justify-center h-full py-10 text-center">
          <Activity size={28} className="text-slate-200 mb-2" />
          <div className="text-xs font-bold text-slate-400">{marketLabel}暂无账户活动</div>
          <div className="text-[10px] text-slate-300 mt-1">该市场无持仓与成交时不展示其他市场曲线</div>
        </div>
      ) : (
      <>
      <div className="grid grid-rows-[1.05fr_1fr] gap-3 h-full">
        <motion.div
          className="rounded-xl bg-white/30 border border-white/40 p-2"
          whileHover={{ backgroundColor: 'rgba(255, 255, 255, 0.5)' }}
        >
          <div className="text-xs text-[var(--text-secondary)] px-1 pb-1">每日收益率</div>
          <div className="h-[calc(100%-18px)] relative">
            <EChartsChart option={getChartOption('dailyReturn', chartData.dailyReturn)} />
            {!hasDailyReturn && (
              <div className="absolute inset-0 flex items-center justify-center pointer-events-none">
                {loading ? (
                  <ChartShimmer className="absolute inset-0" />
                ) : (
                  <div className="text-xs text-[var(--text-tertiary)] flex items-center gap-1 bg-white/65 px-2 py-1 rounded-md">
                    <Activity size={12} />
                    暂无收益数据
                  </div>
                )}
              </div>
            )}
          </div>
        </motion.div>

        <div className="grid grid-cols-2 gap-3 min-h-0">
          <motion.div
            className="rounded-xl bg-white/30 border border-white/40 p-2 min-h-0 flex flex-col"
            whileHover={{ backgroundColor: 'rgba(255, 255, 255, 0.5)' }}
          >
            <div className="text-xs text-[var(--text-secondary)] px-1 pb-1">交易次数</div>
            {tradeStats && (
              <div className="flex items-center justify-between gap-1 px-1 pb-1 text-[10px] font-mono text-slate-500">
                <span title="累计成交笔数">累计 {tradeStats.totalTrades} 笔</span>
                <span title="盈利平仓次数 / 总平仓次数">胜率 {(tradeStats.winRate * 100).toFixed(1)}%</span>
                <span title="平均盈利 / 平均亏损（手续费计入）">盈亏比 {profitLossRatioText}</span>
              </div>
            )}
            <div className="flex-1 min-h-0">
              {hasTradeCount ? (
                <EChartsChart option={getChartOption('tradeCount', chartData.tradeCount)} />
              ) : (
                <div className="h-full flex items-center justify-center text-xs text-[var(--text-tertiary)]">
                  {loading ? <ChartShimmer className="h-full w-full" /> : '暂无交易统计'}
                </div>
              )}
            </div>
          </motion.div>

          <motion.div
            className="rounded-xl bg-white/30 border border-white/40 p-2 min-h-0"
            whileHover={{ backgroundColor: 'rgba(255, 255, 255, 0.5)' }}
          >
            <div className="text-xs text-[var(--text-secondary)] px-1 pb-1">持仓占比</div>
            <div className="h-[calc(100%-18px)]">
              {hasPositionRatio ? (
                <EChartsChart option={getChartOption('positionRatio', chartData.positionRatio)} />
              ) : (
                <div className="h-full flex items-center justify-center text-xs text-[var(--text-tertiary)]">
                  {loading ? <ChartShimmer className="h-full w-full" /> : '暂无持仓分布'}
                </div>
              )}
            </div>
          </motion.div>
        </div>
      </div>

      <div className="flex items-center justify-between text-xs text-slate-500 mt-2 px-2 py-1 min-h-[28px] bg-white/30 border border-white/40 rounded-xl">
        <div className="flex items-center">
          {isConnected ? <Wifi size={14} className="text-emerald-500" /> : <WifiOff size={14} className="text-rose-500" />}
          <span className="ml-1.5">{status}</span>
          {(error || isStale) && <span className="ml-2 text-amber-500">数据延迟</span>}
        </div>
        <div>最后更新 {formatTimestamp(lastUpdatedAt)}</div>
      </div>
      </>
      )}
    </Card>
  );
};

export default IntelligenceChartsCard;
