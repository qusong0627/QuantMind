import React from 'react';
import { Card } from '../common/Card';
import { MarketOverviewSkeleton } from '../common/CardSkeletons';
import { useMarketData } from '../../hooks/useMarketData';
import { useAppSelector } from '../../store';
import { selectCurrentMarket } from '../../store/slices/uiSlice';
import { MARKET_INDICES, type MarketId } from '../../services/marketService';
import type { MarketIndex } from '../../services/marketService';
import { TrendingUp, TrendingDown, Minus } from 'lucide-react';
import { MarketChip, formatBoxTitle, useBoxContent, useMarketContent } from '../../features/dashboard-shared';

export const MarketOverviewCard: React.FC = () => {
  const currentMarket = useAppSelector(selectCurrentMarket);
  // 标题/徽标/数据源标注走市场内容规格（六宫格统一口径）
  const { content } = useBoxContent('market');
  const marketContent = useMarketContent();
  const { data, loading, error, timedOut } = useMarketData({ market: currentMarket, timeoutMs: 8000 });

  // 市场默认数据
  const fallbackData: Partial<MarketIndex>[] = (MARKET_INDICES[currentMarket] || MARKET_INDICES.CN).map(
    ({ name, basePrice }) => ({ name, price: basePrice, change: 0, changePercent: 0 })
  );

  // 超时：直接显示 0，不再等待 loading
  if (loading && !timedOut) {
    return <MarketOverviewSkeleton />;
  }

  if (error) {
    console.error('获取市场数据出错:', error);
  }

  // 超时未取到数据时，指数价格/涨跌幅统一显示 0
  const displayData = timedOut
    ? fallbackData.map(({ name }) => ({ name, price: 0, change: 0, changePercent: 0, amount: undefined }))
    : (data?.indices || fallbackData) as MarketIndex[];
  const stats = data?.stats;
  const upCount = stats?.up ?? displayData.filter((x) => (x.changePercent ?? 0) > 0).length;
  const downCount = stats?.down ?? displayData.filter((x) => (x.changePercent ?? 0) < 0).length;
  const flatCount = stats?.flat ?? displayData.filter((x) => (x.changePercent ?? 0) === 0).length;
  const trend = upCount >= downCount ? (upCount === downCount ? 'flat' : 'up') : 'down';
  const lastUpdate = data?.lastUpdate ? String(data.lastUpdate).slice(0, 10) : '';
  const isRealData = Boolean(data?.sourceUsed) || (data?.indices && data.indices.length > 0 && data.indices.some((x) => x.change !== 0));

  // 固定渲染 6 行，数据不足时补占位行，保证各市场卡片高度一致
  const viewRows = displayData.slice(0, 6);
  const placeholderCount = Math.max(6 - viewRows.length, 0);

  return (
    <Card
      title={
        <span className="inline-flex items-center gap-2">
          <span>{formatBoxTitle(content, { label: marketContent.label })}</span>
          <MarketChip market={currentMarket} source={content.source} />
        </span>
      }
      height="100%"
      background="market"
    >
      <div className="flex flex-col h-full py-1 gap-1">
        {/* 市场状态横幅 */}
        <div className={`flex items-center justify-between px-3 py-1.5 rounded-lg border ${
          trend === 'up'
            ? 'bg-red-50 border-red-100'
            : trend === 'down'
              ? 'bg-emerald-50 border-emerald-100'
              : 'bg-slate-50 border-slate-100'
        }`}>
          <div className="flex items-center gap-2">
            {trend === 'up' ? (
              <TrendingUp size={16} className="text-[var(--profit-primary)]" />
            ) : trend === 'down' ? (
              <TrendingDown size={16} className="text-[var(--loss-primary)]" />
            ) : (
              <Minus size={16} className="text-slate-400" />
            )}
            <span className={`text-xs font-bold ${
              trend === 'up' ? 'text-[var(--profit-primary)]' : trend === 'down' ? 'text-[var(--loss-primary)]' : 'text-slate-500'
            }`}>
              {trend === 'up' ? '市场偏强' : trend === 'down' ? '市场偏弱' : '市场震荡'}
            </span>
          </div>
          <div className="flex items-center gap-3 text-[10px] font-semibold">
            <span className="text-[var(--profit-primary)]">↑ {upCount}</span>
            <span className="text-[var(--loss-primary)]">↓ {downCount}</span>
            <span className="text-slate-400">- {flatCount}</span>
            {lastUpdate && <span className="text-slate-400 ml-1">{lastUpdate}</span>}
          </div>
        </div>

        {/* 市场数据项 - 优化布局 */}
        {viewRows.map((item, index) => {
          const pct = item.changePercent ?? 0;
          const isUp = pct > 0;
          const isDown = pct < 0;
          return (
            <div
              key={index}
              className="
                flex items-center justify-between px-3 py-1 rounded-lg
                bg-slate-50 border border-slate-100/80
                transition-all duration-200 hover:bg-slate-100 hover:shadow-sm
              "
            >
              {/* 股票名称 - 固定宽度 + 单行省略，防止长名称换行导致行高不同 */}
              <div className="text-sm font-bold text-slate-700 w-[88px] truncate shrink-0">
                <span className="inline-block w-[88px] overflow-hidden text-ellipsis whitespace-nowrap" title={item.name}>
                  {item.name}
                </span>
              </div>

              {/* 涨跌幅 - 传统百分比居中显示 */}
              <div className="flex-1 flex items-center justify-center mx-2 min-w-0">
                <span className={`text-sm font-bold font-mono whitespace-nowrap ${
                  isUp ? 'text-[var(--profit-primary)]' : isDown ? 'text-[var(--loss-primary)]' : 'text-slate-500'
                }`}>
                  {pct > 0 ? '+' : ''}{pct.toFixed(2)}%
                </span>
              </div>

              {/* 价格 + 成交额 - 固定两行高度，amount 缺失时显示 -- 保持行高一致 */}
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

        {/* 占位行：数据不足 6 条时填充，统一各市场卡片高度 */}
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

        {/* 数据源标识 */}
        <div className="text-[9px] text-slate-300 text-right px-1 mt-auto leading-none">
          {timedOut
            ? '行情获取超时'
            : isRealData
              ? `数据截至 ${lastUpdate}`
              : '实时行情'} · {timedOut ? '暂无数据' : data?.sourceUsed === 'local_parquet' ? '本地数据' : '实时数据'}
        </div>
      </div>
    </Card>
  );
};

// 格式化大额成交额（万/亿/万亿）
function formatAmount(amount: number): string {
  const abs = Math.abs(amount);
  if (abs >= 1e12) return `${(amount / 1e12).toFixed(2)}万亿`;
  if (abs >= 1e8) return `${(amount / 1e8).toFixed(1)}亿`;
  if (abs >= 1e4) return `${(amount / 1e4).toFixed(1)}万`;
  return amount.toFixed(0);
}
