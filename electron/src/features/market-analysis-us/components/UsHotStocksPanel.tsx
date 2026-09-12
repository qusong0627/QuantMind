/** 美股今日热门榜 —— 成交额 / 量比 / 涨幅 / 跌幅 / 放量异动
 *
 * 这是「看哪儿热」的核心面板。专业口径：
 * - **成交额**（美元）才是跨标的可比的关注度指标（20 美元的股成交 1 亿股
 *   与 800 美元的股成交 100 万股，成交量差 100 倍但成交额可能相当）
 * - **量比** = 当日量 / **前** 20 日均量，是「异动」的第一判据
 * - **距 52 周高点** 用来区分「新高附近的放量（强势突破）」与「下跌中的放量（出货）」
 */

import React, { useEffect, useState } from 'react';
import { Flame, TrendingUp, TrendingDown, Zap, BarChart3 } from 'lucide-react';
import { getHotStocks, getUnusualVolume } from '../services/api';
import type { UsHotKind, UsHotStockRow } from '../types';
import { SectionCard, EmptyHint, DateBadge, fmtInt } from '../../market-analysis-shared/ui';

type TabId = UsHotKind | 'unusual';

const TABS: Array<{ id: TabId; label: string; hint: string }> = [
  { id: 'amount', label: '成交额', hint: '成交额榜：资金真正在交易的标的' },
  { id: 'rvol', label: '量比', hint: '量比榜：相对自身历史放量（基准=前20日均量）' },
  { id: 'gainers', label: '涨幅', hint: '今日涨幅榜' },
  { id: 'losers', label: '跌幅', hint: '今日跌幅榜' },
  { id: 'unusual', label: '异动', hint: '量比 ≥2 的放量异动' },
];

/** 量比着色：≥5 深橙（极端异动）≥2 橙 ≥1.5 浅橙，其余灰 */
function rvolClass(v: number | null): string {
  if (v === null || !Number.isFinite(v)) return 'text-slate-300';
  if (v >= 5) return 'text-orange-700 bg-orange-100 rounded px-1 font-extrabold';
  if (v >= 2) return 'text-orange-600 font-extrabold';
  if (v >= 1.5) return 'text-orange-500 font-bold';
  return 'text-slate-400';
}

/** 距 52 周高点：0 附近标「新高」，-3 以内标「近高」，深跌标「深跌」 */
function HighTag({ dd }: { dd: number | null }) {
  if (dd === null || !Number.isFinite(dd)) return <span className="text-slate-300">--</span>;
  if (dd >= -0.5)
    return <span className="px-1 rounded bg-red-100 text-red-700 font-extrabold">新高</span>;
  if (dd >= -3)
    return <span className="px-1 rounded bg-orange-50 text-orange-600 font-bold">近高</span>;
  if (dd <= -40)
    return <span className="px-1 rounded bg-green-50 text-green-700 font-bold">深跌</span>;
  return <span className="text-slate-500 font-mono">{dd.toFixed(1)}%</span>;
}

export const UsHotStocksPanel: React.FC<{ limit?: number; className?: string }> = ({
  limit = 18,
  className = '',
}) => {
  const [tab, setTab] = useState<TabId>('amount');
  const [rows, setRows] = useState<UsHotStockRow[]>([]);
  const [tradeDate, setTradeDate] = useState('');
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    const fetch =
      tab === 'unusual' ? getUnusualVolume(limit, 2.0) : getHotStocks(tab as UsHotKind, limit);
    fetch
      .then((d) => {
        if (!alive) return;
        setRows(Array.isArray(d.items) ? d.items : []);
        setTradeDate(d.trade_date || '');
      })
      .catch(() => undefined)
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [tab, limit]);

  const active = TABS.find((t) => t.id === tab);

  return (
    <SectionCard
      className={`!p-2.5 ${className}`}
      title={
        <span className="flex items-center gap-1.5">
          <Flame className="w-3.5 h-3.5 text-orange-500" />
          今日热门
          <span className="text-[9px] font-normal text-slate-400">{active?.hint}</span>
        </span>
      }
      extra={<DateBadge date={tradeDate} />}
    >
      {/* Tab 条：紧凑按钮组 */}
      <div className="flex items-center gap-1 -mt-1">
        {TABS.map((t) => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            title={t.hint}
            className={`px-2 py-[3px] rounded-md text-[10px] font-extrabold transition-colors ${
              tab === t.id
                ? 'bg-blue-600 text-white'
                : 'bg-slate-100 text-slate-500 hover:bg-slate-200 hover:text-slate-700'
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {rows.length === 0 ? (
        <EmptyHint loading={loading} />
      ) : (
        <div className="overflow-hidden">
          {/* 表头 */}
          <div className="grid grid-cols-[18px_1fr_62px_58px_64px_50px_46px] gap-1 px-1 py-[3px] text-[9px] font-bold text-slate-400 border-b border-slate-200">
            <span>#</span>
            <span>标的</span>
            <span className="text-right">现价</span>
            <span className="text-right">涨跌</span>
            <span className="text-right">成交额</span>
            <span className="text-right">量比</span>
            <span className="text-right">52周</span>
          </div>
          {rows.map((r, i) => (
            <div
              key={r.symbol}
              className="grid grid-cols-[18px_1fr_62px_58px_64px_50px_46px] gap-1 px-1 py-[3px] text-[10px] items-center border-b border-slate-50 last:border-0 hover:bg-slate-50/80"
            >
              <span
                className={`text-[9px] font-extrabold ${
                  i < 3 ? 'text-orange-600' : 'text-slate-400'
                }`}
              >
                {i + 1}
              </span>
              <span className="flex items-center gap-1 min-w-0">
                <span className="font-bold text-slate-800 truncate" title={r.name}>
                  {r.name}
                </span>
                <span className="font-mono text-[9px] text-slate-400">{r.symbol}</span>
                <span className="text-[9px] text-slate-300 truncate hidden xl:inline">
                  {r.sector}
                </span>
              </span>
              <span className="text-right font-mono text-slate-700">
                {r.close?.toFixed(2) ?? '--'}
              </span>
              <span
                className={`text-right font-mono font-extrabold ${
                  r.pct_change > 0
                    ? 'text-red-600'
                    : r.pct_change < 0
                    ? 'text-green-600'
                    : 'text-slate-500'
                }`}
              >
                {r.pct_change > 0 ? '+' : ''}
                {r.pct_change?.toFixed(2) ?? '--'}%
              </span>
              <span className="text-right font-mono text-slate-600">
                {fmtInt(r.amount_yi)}
                <span className="text-[9px] text-slate-400">亿</span>
              </span>
              <span className={`text-right font-mono ${rvolClass(r.rvol)}`}>
                {r.rvol === null || r.rvol === undefined ? '--' : r.rvol.toFixed(2)}
              </span>
              <span className="text-right text-[9px]">
                <HighTag dd={r.drawdown_pct} />
              </span>
            </div>
          ))}
        </div>
      )}
    </SectionCard>
  );
};

/** 市场活力条：一行紧凑指标，回答「今天市场活不活跃」 */
export const UsMarketPulseStrip: React.FC<{ stats: import('../types').UsMarketStats | null }> = ({
  stats,
}) => {
  if (!stats || !stats.trade_date) return null;
  const cells = [
    {
      icon: <BarChart3 className="w-3 h-3 text-blue-500" />,
      label: '全市场成交',
      value: `${fmtInt(stats.total_amount_yi)}`,
      unit: '亿',
      tone: 'text-slate-800',
    },
    {
      icon: <Zap className="w-3 h-3 text-orange-500" />,
      label: '量比中位',
      value: stats.rvol_median === null ? '--' : stats.rvol_median.toFixed(2),
      unit: '',
      tone: (stats.rvol_median ?? 0) >= 1 ? 'text-orange-600' : 'text-slate-500',
    },
    {
      icon: <Flame className="w-3 h-3 text-orange-500" />,
      label: '放量占比',
      value: stats.active_ratio.toFixed(1),
      unit: '%',
      tone: stats.active_ratio >= 30 ? 'text-orange-600' : 'text-slate-600',
    },
    {
      icon: <Zap className="w-3 h-3 text-amber-500" />,
      label: '量比≥2',
      value: `${stats.high_rvol_count}`,
      unit: '只',
      tone: 'text-amber-600',
    },
    {
      icon: <TrendingUp className="w-3 h-3 text-red-500" />,
      label: '涨≥5%',
      value: `${stats.up_5pct}`,
      unit: '只',
      tone: 'text-red-600',
    },
    {
      icon: <TrendingDown className="w-3 h-3 text-green-500" />,
      label: '跌≤-5%',
      value: `${stats.down_5pct}`,
      unit: '只',
      tone: 'text-green-600',
    },
  ];
  return (
    <div className="rounded-xl bg-white/90 border border-slate-200/80 shadow-sm px-3 py-1.5 flex items-center gap-x-5 gap-y-1 flex-wrap">
      {cells.map((c) => (
        <span key={c.label} className="flex items-center gap-1.5 whitespace-nowrap">
          {c.icon}
          <span className="text-[10px] font-bold text-slate-400">{c.label}</span>
          <span className={`text-[13px] font-extrabold font-mono ${c.tone}`}>{c.value}</span>
          {c.unit && <span className="text-[9px] text-slate-400 -ml-1">{c.unit}</span>}
        </span>
      ))}
    </div>
  );
};
