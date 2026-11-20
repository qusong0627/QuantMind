import React from 'react';
import { Card } from '../common/Card';
import { MarketOverviewSkeleton } from '../common/CardSkeletons';
import { useMarketData } from '../../hooks/useMarketData';
import { useAppSelector } from '../../store';
import { selectCurrentMarket } from '../../store/slices/uiSlice';
import { type MarketId } from '../../services/marketService';
import type { MarketIndex } from '../../services/marketService';
import { TrendingUp, TrendingDown, Minus } from 'lucide-react';

const MARKET_LABELS: Record<MarketId, string> = {
  CN: 'A股',
  HK: '港股',
  US: '美股',
  CRYPTO: '区块链',
  FUTURES: '期货',
  CUSTOM: '自定义',
};

export const MarketOverviewCard: React.FC = () => {
  const currentMarket = useAppSelector(selectCurrentMarket);
  const { data, loading, error, timedOut } = useMarketData({ market: currentMarket, timeoutMs: 8000 });

  if (loading && !timedOut) {
    return <MarketOverviewSkeleton />;
  }

  if (error) {
    console.error('获取市场数据出错:', error);
  }

  // 无真实数据时留空（不再用 basePrice / 0 值假数据填充）
  const displayData: MarketIndex[] = timedOut ? [] : data?.indices || [];
  const hasData = displayData.length > 0;
  const stats = data?.stats;
  const upCount = stats?.up ?? displayData.filter((x) => (x.changePercent ?? 0) > 0).length;
  const downCount = stats?.down ?? displayData.filter((x) => (x.changePercent ?? 0) < 0).length;
  const flatCount = stats?.flat ?? displayData.filter((x) => (x.changePercent ?? 0) === 0).length;
  const trend = !hasData
    ? 'flat'
    : upCount >= downCount
      ? upCount === downCount
        ? 'flat'
        : 'up'
      : 'down';
  const lastUpdate = data?.lastUpdate ? String(data.lastUpdate).slice(0, 10) : '';

  const viewRows = displayData.slice(0, 6);
  const placeholderCount = Math.max(6 - viewRows.length, 0);

  return (
    <Card title={`${MARKET_LABELS[currentMarket]}概览`} height="100%" background="market">
      <div className="flex flex-col h-full py-1 gap-1">
        <div
          className={`flex items-center justify-between px-3 py-1.5 rounded-lg border ${
            trend === 'up'
              ? 'bg-red-50 border-red-100'
              : trend === 'down'
                ? 'bg-emerald-50 border-emerald-100'
                : 'bg-slate-50 border-slate-100'
          }`}
        >
          <div className="flex items-center gap-2">
            {trend === 'up' ? (
              <TrendingUp size={16} className="text-[var(--profit-primary)]" />
            ) : trend === 'down' ? (
              <TrendingDown size={16} className="text-[var(--loss-primary)]" />
            ) : (
              <Minus size={16} className="text-slate-400" />
            )}
            <span
              className={`text-xs font-bold ${
                trend === 'up'
                  ? 'text-[var(--profit-primary)]'
                  : trend === 'down'
                    ? 'text-[var(--loss-primary)]'
                    : 'text-slate-500'
              }`}
            >
              {!hasData
                ? '暂无行情'
                : trend === 'up'
                  ? '市场偏强'
                  : trend === 'down'
                    ? '市场偏弱'
                    : '市场震荡'}
            </span>
          </div>
          {hasData && (
            <div className="flex items-center gap-3 text-[10px] font-semibold">
              <span className="text-[var(--profit-primary)]">↑ {upCount}</span>
              <span className="text-[var(--loss-primary)]">↓ {downCount}</span>
              <span className="text-slate-400">- {flatCount}</span>
              {lastUpdate && <span className="text-slate-400 ml-1">{lastUpdate}</span>}
            </div>
          )}
        </div>

        {viewRows.map((item, index) => {
          const pct = item.changePercent ?? 0;
          const isUp = pct > 0;
          const isDown = pct < 0;
          return (
            <div
              key={`${item.symbol || item.name}-${index}`}
              className="
                flex items-center justify-between px-3 py-1 rounded-lg
                bg-slate-50 border border-slate-100/80
                transition-all duration-200 hover:bg-slate-100 hover:shadow-sm
              "
            >
              <div className="text-sm font-bold text-slate-700 w-[88px] truncate shrink-0">
                <span
                  className="inline-block w-[88px] overflow-hidden text-ellipsis whitespace-nowrap"
                  title={item.name}
                >
                  {item.name}
                </span>
              </div>

              <div className="flex-1 flex items-center justify-center mx-2 min-w-0">
                <span
                  className={`text-sm font-bold font-mono whitespace-nowrap ${
                    isUp
                      ? 'text-[var(--profit-primary)]'
                      : isDown
                        ? 'text-[var(--loss-primary)]'
                        : 'text-slate-500'
                  }`}
                >
                  {pct > 0 ? '+' : ''}
                  {pct.toFixed(2)}%
                </span>
              </div>

              <div className="text-right w-[110px] shrink-0">
                <div className="text-sm font-black text-slate-800 font-mono whitespace-nowrap overflow-hidden text-ellipsis leading-tight">
                  {item.price?.toFixed(item.price && item.price < 10 ? 3 : 2)}
                </div>
                <div className="text-[9px] text-slate-400 font-mono mt-0.5 whitespace-nowrap leading-tight">
                  {item.amount ? formatAmount(item.amount) : '--'}
                </div>
              </div>
            </div>
          );
        })}

        {Array.from({ length: placeholderCount }).map((_, index) => (
          <div
            key={`placeholder-${index}`}
            aria-hidden="true"
            className="flex items-center justify-between px-3 py-1 rounded-lg bg-slate-50/50 border border-slate-100/50 opacity-50"
          >
            <div className="text-sm font-bold text-slate-300 w-[88px] truncate shrink-0">--</div>
            <div className="flex-1 flex items-center justify-center mx-2 min-w-0">
              <span className="text-sm font-bold font-mono text-slate-200">--</span>
            </div>
            <div className="text-right w-[110px] shrink-0">
              <div className="text-sm font-black text-slate-200 font-mono leading-tight">--</div>
              <div className="text-[9px] text-slate-200 font-mono mt-0.5 leading-tight">--</div>
            </div>
          </div>
        ))}

        <div className="text-[9px] text-slate-300 text-right px-1 mt-auto leading-none">
          {timedOut
            ? '行情获取超时 · 暂无数据'
            : hasData
              ? `数据截至 ${lastUpdate || '—'} · ${
                  data?.sourceUsed === 'local_parquet' ? '本地数据' : '实时数据'
                }`
              : '暂无行情数据'}
        </div>
      </div>
    </Card>
  );
};

function formatAmount(amount: number): string {
  const abs = Math.abs(amount);
  if (abs >= 1e12) return `${(amount / 1e12).toFixed(2)}万亿`;
  if (abs >= 1e8) return `${(amount / 1e8).toFixed(1)}亿`;
  if (abs >= 1e4) return `${(amount / 1e4).toFixed(1)}万`;
  return amount.toFixed(0);
}
