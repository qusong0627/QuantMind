// Triggering rebuild for mode-aware charts
import React from 'react';
import { Card } from '../common/Card';
import { motion } from 'framer-motion';
import { useWebSocket } from '../../contexts/WebSocketContext';
import { Activity, Wifi, WifiOff } from 'lucide-react';
import { EChartsChart } from '../common/EChartsChart';
import { getChartOption } from '../../utils/chartOptions';
import { useIntelligenceCharts } from '../../hooks/useIntelligenceCharts';
import { useAppSelector } from '../../store';
import { formatBackendTime } from '../../utils/format';

// 图表区域占位 shimmer
const ChartShimmer: React.FC<{ className?: string }> = ({ className = '' }) => (
  <div className={`animate-pulse bg-gray-100/80 rounded-lg ${className}`} />
);

const IntelligenceChartsCard: React.FC = () => {
  const tradingMode = useAppSelector((state) => state.ui.tradingMode);
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

  const formatTimestamp = (timeStr: string | null) => {
    return formatBackendTime(timeStr, { withSeconds: true });
  };

  return (
    <Card title="智能图表" height="100%" background="charts">
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
            className="rounded-xl bg-white/30 border border-white/40 p-2 min-h-0"
            whileHover={{ backgroundColor: 'rgba(255, 255, 255, 0.5)' }}
          >
            <div className="text-xs text-[var(--text-secondary)] px-1 pb-1">交易次数</div>
            <div className="h-[calc(100%-18px)]">
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
    </Card>
  );
};

export default IntelligenceChartsCard;
