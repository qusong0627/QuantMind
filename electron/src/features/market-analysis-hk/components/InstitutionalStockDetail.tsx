/** 个股机构持仓详情 —— 分类结构 / 参与者明细 / 分类持仓趋势 */

import React, { useEffect, useState } from 'react';
import type { InstitutionalParticipant, InstitutionalStockDetail as StockDetail } from '../types';
import { getInstitutionalStock } from '../services/api';
import { EmptyHint, PctText, fmtInt } from './shared/ui';
import { InstitutionalTrendChart } from './InstitutionalTrendChart';

const KIND_BADGE: Record<string, string> = {
  settlement: 'bg-purple-50 text-purple-600 border border-purple-100',
  custodian: 'bg-indigo-50 text-indigo-600 border border-indigo-100',
  broker: 'bg-amber-50 text-amber-600 border border-amber-100',
  other: 'bg-slate-50 text-slate-500 border border-slate-100',
};
const KIND_LABEL: Record<string, string> = {
  settlement: '结算',
  custodian: '托管',
  broker: '券商',
  other: '其他',
};

function DeltaQtyText({ value }: { value: number }) {
  const v = Number(value);
  if (Number.isNaN(v)) return <span className="text-slate-400 text-[10px]">--</span>;
  const color = v > 0 ? 'text-red-600' : v < 0 ? 'text-green-600' : 'text-slate-500';
  return (
    <span className={`text-right text-[10px] font-mono font-bold ${color}`}>
      {v > 0 ? '+' : ''}
      {fmtInt(v)}
    </span>
  );
}

export const InstitutionalStockDetail: React.FC<{ symbol: string }> = ({ symbol }) => {
  const [data, setData] = useState<StockDetail | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    getInstitutionalStock(symbol)
      .then((d) => alive && setData(d))
      .catch(() => alive && setData(null))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [symbol]);

  if (loading) return <EmptyHint loading />;
  if (!data) return <EmptyHint text="机构持仓数据不可用（该股可能不在 CCASS 披露内）" />;

  return (
    <div className="flex flex-col gap-2.5">
      {/* 头部 */}
      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-sm font-extrabold text-slate-900">{data.name}</span>
        <span className="text-[10px] font-mono text-slate-400">({data.symbol})</span>
        {data.price ? (
          <span className="text-[11px] font-mono font-extrabold text-slate-700">
            HK$ {data.price.toFixed(2)}
          </span>
        ) : null}
        <span className="ml-auto text-[9px] font-mono text-slate-400 whitespace-nowrap">
          {data.trade_date || '--'}
        </span>
      </div>
      <div className="flex items-center gap-1.5 flex-wrap">
        {data.south_pct !== null && data.south_pct !== undefined && (
          <span className="px-1.5 py-0.5 rounded-md bg-orange-50 text-orange-600 border border-orange-100 text-[9px] font-extrabold">
            南向(港股通)持股 {data.south_pct.toFixed(2)}%
          </span>
        )}
        <span className="px-1.5 py-0.5 rounded-md bg-purple-50 text-purple-600 border border-purple-100 text-[9px] font-extrabold">
          前50披露席位合计 {data.disclosed_pct.toFixed(1)}%
        </span>
      </div>

      {/* 分类明细 */}
      <div className="grid grid-cols-[1fr_58px_52px_60px_60px_60px] gap-1 px-1 text-[9px] font-extrabold text-slate-400 border-b border-slate-100 pb-1">
        <span>资金属性</span>
        <span className="text-right">市值(亿)</span>
        <span className="text-right">占比%</span>
        <span className="text-right">Δ5日(亿)</span>
        <span className="text-right">Δ20日(亿)</span>
        <span className="text-right">Δ60日(亿)</span>
      </div>
      <div className="flex flex-col">
        {data.categories.map((c) => {
          const d5 = c.deltas.find((d) => d.window === 5);
          const d20 = c.deltas.find((d) => d.window === 20);
          const d60 = c.deltas.find((d) => d.window === 60);
          return (
            <div
              key={c.category}
              className="grid grid-cols-[1fr_58px_52px_60px_60px_60px] gap-1 px-1 py-1.5 border-b border-slate-50 last:border-0 items-center"
            >
              <span className="flex items-center gap-1.5 min-w-0">
                <span className="text-[10px] font-extrabold text-slate-700 truncate">{c.label}</span>
                <span className="text-[9px] font-mono text-slate-400 flex-shrink-0">
                  {fmtInt(c.holding_qty)}
                </span>
              </span>
              <span className="text-right text-[11px] font-mono font-extrabold text-slate-800">
                {c.value_yi.toFixed(1)}
              </span>
              <span className="text-right text-[10px] font-mono text-slate-500">
                {c.pct_of_total.toFixed(2)}
              </span>
              <span className="text-right">
                <PctText value={d5?.delta_yi} suffix="亿" className="!text-[10px]" />
              </span>
              <span className="text-right">
                <PctText value={d20?.delta_yi} suffix="亿" className="!text-[10px]" />
              </span>
              <span className="text-right">
                <PctText value={d60?.delta_yi} suffix="亿" className="!text-[10px]" />
              </span>
            </div>
          );
        })}
      </div>

      {/* 参与者明细 */}
      <div className="flex flex-col">
        <div className="grid grid-cols-[1fr_64px_52px_60px] gap-1 px-1 pb-1 text-[9px] font-extrabold text-slate-400 border-b border-slate-100">
          <span>席位（前 15）</span>
          <span className="text-right">持股量</span>
          <span className="text-right">占比%</span>
          <span className="text-right">Δ5日(股)</span>
        </div>
        <div className="flex flex-col max-h-56 overflow-y-auto">
          {data.participants.slice(0, 15).map((p: InstitutionalParticipant, i: number) => (
            <div
              key={(p.participant_id || '') + i}
              className="grid grid-cols-[1fr_64px_52px_60px] gap-1 px-1 py-1.5 border-b border-slate-50 last:border-0 items-center"
            >
              <span className="flex items-center gap-1.5 min-w-0">
                <span
                  className={`w-7 text-center rounded-md px-1 py-0.5 text-[8px] font-extrabold flex-shrink-0 ${
                    KIND_BADGE[p.kind] || KIND_BADGE.other
                  }`}
                >
                  {KIND_LABEL[p.kind] || p.kind}
                </span>
                <span className="text-[10px] font-bold text-slate-700 truncate">
                  {p.participant_name}
                </span>
              </span>
              <span className="text-right text-[10px] font-mono text-slate-500">
                {fmtInt(p.holding_quantity)}
              </span>
              <span className="text-right text-[10px] font-mono text-slate-700">
                {p.holding_pct !== null && p.holding_pct !== undefined
                  ? p.holding_pct.toFixed(2)
                  : '--'}
              </span>
              <span className="text-right">
                <DeltaQtyText value={p.delta_5d_qty} />
              </span>
            </div>
          ))}
        </div>
      </div>

      {/* 分类持仓趋势 */}
      {data.trend.dates.length > 0 && data.trend.series.length > 0 && (
        <div>
          <div className="text-[10px] font-extrabold text-slate-500 mb-1">分类持仓量走势（60 交易日）</div>
          <InstitutionalTrendChart dates={data.trend.dates} series={data.trend.series} />
        </div>
      )}
    </div>
  );
};