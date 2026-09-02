/** 南向资金面板 —— 港股核心特色：港股通持股穿透（总览 + 增减持榜 + 板块配置） */

import React, { useState } from 'react';
import { Waves, Landmark, ArrowUpCircle, ArrowDownCircle } from 'lucide-react';
import type { HkSouthFlow, HkSouthFlowItem, HkSouthOverview, HkSouthSectorItem } from '../types';
import { getSouthFlow, getSouthOverview, getSouthSectors } from '../services/api';
import { EmptyHint, PeriodChips, PctText, RankRow, SectionCard, fmtInt } from './shared/ui';

const PERIODS = [
  { id: '5', label: '5日' },
  { id: '20', label: '20日' },
];

export const HkSouthPanel: React.FC = () => {
  const [overview, setOverview] = useState<HkSouthOverview | null>(null);
  const [flow, setFlow] = useState<HkSouthFlow | null>(null);
  const [sectors, setSectors] = useState<HkSouthSectorItem[]>([]);
  const [period, setPeriod] = useState('5');
  const [loading, setLoading] = useState(false);

  React.useEffect(() => {
    let alive = true;
    setLoading(true);
    Promise.all([getSouthOverview(), getSouthFlow(Number(period)), getSouthSectors(20)])
      .then(([ov, fl, se]) => {
        if (!alive) return;
        setOverview(ov);
        setFlow(fl);
        setSectors(se);
      })
      .catch(() => undefined)
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [period]);

  const maxHold = Math.max(...sectors.map((s) => s.hold_value_yi), 1);

  const renderFlowList = (items: HkSouthFlowItem[], isIncrease: boolean, offset: number) =>
    items.map((it, i) => (
      <RankRow
        key={it.symbol}
        rank={offset + i + 1}
        name={it.name}
        nameSub={it.symbol}
        main={
          <PctText
            value={it.pct_change_abs}
            suffix=" 个百分点"
            className={isIncrease ? 'text-xs' : 'text-xs'}
          />
        }
        right={
          <div className="flex items-center gap-3 flex-shrink-0">
            {it.price !== undefined && (
              <span className="text-[11px] font-mono text-slate-600 w-14 text-right">
                {it.price.toFixed(2)}
              </span>
            )}
            <span className="text-[10px] font-mono text-slate-400 w-16 text-right">
              占 {it.holding_pct.toFixed(2)}%
            </span>
          </div>
        }
      />
    ));

  return (
    <div className="flex flex-col gap-2.5">
      {/* 总览条（紧凑横条） */}
      <SectionCard
        title={<span className="flex items-center gap-1.5"><Waves className="w-3.5 h-3.5 text-purple-600" />南向资金 · 港股通持股总览</span>}
        extra={
          <PeriodChips options={PERIODS} value={period} onChange={setPeriod} />
        }
      >
        {loading && !overview ? (
          <EmptyHint loading />
        ) : overview ? (
          <div className="grid grid-cols-2 md:grid-cols-6 gap-2">
            {[
              { label: '披露日', value: overview.trade_date || '--', mono: true },
              { label: '覆盖股票', value: `${fmtInt(overview.covered_stocks)} 只` },
              { label: '总持仓市值', value: `HK$ ${fmtInt(overview.total_hold_value_yi)} 亿` },
              { label: '当日增持', value: `${fmtInt(overview.up_days_change)} 只`, color: 'text-red-600' },
              { label: '当日减持', value: `${fmtInt(overview.down_days_change)} 只`, color: 'text-green-600' },
              { label: '南向持有', value: `${fmtInt(overview.south_stock_count)} 只` },
            ].map((m) => (
              <div key={m.label} className="rounded-xl border border-purple-100/70 bg-gradient-to-b from-purple-50/50 to-white px-3 py-2">
                <span className="text-[10px] font-bold text-slate-400 block">{m.label}</span>
                <span className={`text-sm font-extrabold font-mono ${m.mono ? 'text-purple-700' : m.color ?? 'text-slate-800'}`}>
                  {m.value}
                </span>
              </div>
            ))}
          </div>
        ) : (
          <EmptyHint text="南向数据不可用（检查 quanthk_hub 数据目录）" />
        )}
      </SectionCard>

      {/* 增持 / 减持 / 板块配置 三栏 */}
      <div className="grid grid-cols-1 xl:grid-cols-3 gap-2.5 items-start">
        <SectionCard
          title={<span className="flex items-center gap-1.5"><ArrowUpCircle className="w-3.5 h-3.5 text-red-500" />南向增持榜（{period}日）</span>}
          extra={flow && <span className="text-[9px] font-mono text-slate-400 whitespace-nowrap">截至 {flow.trade_date}</span>}
        >
          {loading && !flow ? <EmptyHint loading /> : flow?.increase.length ? (
            <div className="flex flex-col max-h-[420px] overflow-y-auto">
              {renderFlowList(flow.increase, true, 0)}
            </div>
          ) : (
            <EmptyHint text="暂无增持变化" />
          )}
        </SectionCard>
        <SectionCard
          title={<span className="flex items-center gap-1.5"><ArrowDownCircle className="w-3.5 h-3.5 text-green-500" />南向减持榜（{period}日）</span>}
          extra={flow && <span className="text-[9px] font-mono text-slate-400 whitespace-nowrap">截至 {flow.trade_date}</span>}
        >
          {loading && !flow ? <EmptyHint loading /> : flow?.decrease.length ? (
            <div className="flex flex-col max-h-[420px] overflow-y-auto">
              {renderFlowList(flow.decrease, false, 0)}
            </div>
          ) : (
            <EmptyHint text="暂无减持变化" />
          )}
        </SectionCard>
        <SectionCard
          title={<span className="flex items-center gap-1.5"><Landmark className="w-3.5 h-3.5 text-purple-600" />南向板块配置</span>}
          extra={overview && <span className="text-[9px] font-mono text-slate-400 whitespace-nowrap">{overview.trade_date}</span>}
        >
          {loading && !sectors.length ? <EmptyHint loading /> : (
            <div className="flex flex-col gap-2 max-h-[420px] overflow-y-auto">
              {sectors.map((s) => (
                <div key={s.name} className="flex items-center gap-2">
                  <span className="w-20 text-[11px] font-bold text-slate-700 truncate flex-shrink-0">{s.name}</span>
                  <div className="flex-1 h-2.5 rounded-full bg-slate-100 overflow-hidden">
                    <div
                      className="h-full rounded-full bg-gradient-to-r from-purple-500 to-indigo-500"
                      style={{ width: `${(s.hold_value_yi / maxHold) * 100}%` }}
                    />
                  </div>
                  <span className="w-16 text-right text-[10px] font-mono text-slate-600 flex-shrink-0">
                    {fmtInt(s.hold_value_yi)}亿
                  </span>
                  <span className="w-12 text-right text-[9px] font-mono text-slate-400 flex-shrink-0">
                    {s.pct_avg.toFixed(1)}%
                  </span>
                </div>
              ))}
            </div>
          )}
        </SectionCard>
      </div>
    </div>
  );
};