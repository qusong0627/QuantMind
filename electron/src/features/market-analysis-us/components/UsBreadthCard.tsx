/** 美股市场温度计 —— 美股无涨跌停（有 LULD 熔断），±5% 为**异动**口径而非涨停。
 *
 * 成交额为美元原始值（daily_forward 的 amount 列即美元金额，无换算系数）。
 */

import React from 'react';
import { Flame, TrendingUp, TrendingDown, Minus, Zap, ShieldAlert, Coins, Scale } from 'lucide-react';
import type { UsBreadthData } from '../types';
import { SectionCard, fmtInt } from '../../market-analysis-shared/ui';

export const UsBreadthCard: React.FC<{ breadth: UsBreadthData | null; loading?: boolean }> = ({
  breadth,
  loading,
}) => {
  const b = breadth ?? {
    trade_date: '',
    total_stocks: 0,
    advance_count: 0,
    decline_count: 0,
    flat_count: 0,
    big_up_count: 0,
    big_down_count: 0,
    total_turnover_yi: 0,
    profit_effect: 50,
    sentiment_score: 50,
    median_pct: 0,
    big_move_threshold: 5,
  };
  const total = Math.max(b.total_stocks, 1);
  const advPct = (b.advance_count / total) * 100;
  const decPct = (b.decline_count / total) * 100;

  const meters = [
    { icon: <TrendingUp className="w-3 h-3 text-red-500" />, label: '上涨', value: b.advance_count, color: 'text-red-600', bg: 'bg-red-50 border-red-100', pct: advPct },
    { icon: <TrendingDown className="w-3 h-3 text-green-500" />, label: '下跌', value: b.decline_count, color: 'text-green-600', bg: 'bg-green-50 border-green-100', pct: decPct },
    { icon: <Minus className="w-3 h-3 text-slate-400" />, label: '平盘', value: b.flat_count, color: 'text-slate-500', bg: 'bg-slate-50 border-slate-100', pct: 0 },
    { icon: <Zap className="w-3 h-3 text-orange-500" />, label: `异动涨 ≥${b.big_move_threshold}%`, value: b.big_up_count, color: 'text-orange-600', bg: 'bg-orange-50 border-orange-100', pct: 0 },
    { icon: <ShieldAlert className="w-3 h-3 text-sky-500" />, label: `异动跌 ≤-${b.big_move_threshold}%`, value: b.big_down_count, color: 'text-sky-600', bg: 'bg-sky-50 border-sky-100', pct: 0 },
  ];

  const sentiment = Math.max(0, Math.min(100, b.sentiment_score || 50));
  const sentiColor = sentiment >= 60 ? 'text-red-600' : sentiment <= 40 ? 'text-green-600' : 'text-amber-600';
  const sentiLabel = sentiment >= 60 ? '强势' : sentiment <= 40 ? '弱势' : '中性';

  return (
    <SectionCard
      title={
        <span className="flex items-center gap-1.5">
          <Flame className="w-3.5 h-3.5 text-blue-600" />
          美股市场温度计与赚钱效应
        </span>
      }
      extra={
        <span className="px-2.5 py-0.5 rounded-full bg-blue-50 text-blue-700 text-[11px] font-bold border border-blue-100 font-mono">
          全市场成交: US$ {fmtInt(b.total_turnover_yi)} 亿
        </span>
      }
    >
      {loading && !breadth ? (
        <div className="py-4 text-center text-xs text-slate-400">加载中…</div>
      ) : (
        <div className="grid grid-cols-2 md:grid-cols-5 gap-2.5">
          {meters.map((m) => (
            <div key={m.label} className={`rounded-xl border px-3 py-2 flex flex-col gap-1 ${m.bg}`}>
              <span className="flex items-center gap-1 text-[10px] font-bold text-slate-500">
                {m.icon}
                {m.label}
              </span>
              <span className={`text-lg font-extrabold font-mono ${m.color}`}>{fmtInt(m.value)}</span>
              {m.pct > 0 && (
                <span className="text-[10px] font-mono text-slate-400">{m.pct.toFixed(0)}%</span>
              )}
            </div>
          ))}
        </div>
      )}

      <div className="flex items-center justify-between gap-4">
        <div className="flex-1">
          <div className="flex justify-between text-[10px] font-mono text-slate-500 mb-1">
            <span>上涨占比 {advPct.toFixed(1)}%</span>
            <span>下跌占比 {decPct.toFixed(1)}%</span>
          </div>
          <div className="h-1.5 rounded-full bg-slate-100 overflow-hidden flex">
            <div className="bg-red-500 h-full" style={{ width: `${advPct}%` }} />
            <div className="bg-green-500 h-full" style={{ width: `${decPct}%` }} />
          </div>
        </div>
        {/* 中位数涨幅比均值更能代表「普通股票今天怎么样」 */}
        <div className="flex items-center gap-2 flex-shrink-0" title="全市场涨跌幅中位数（对拆股等异常值更稳健）">
          <Scale className="w-4 h-4 text-slate-400" />
          <div className="flex items-baseline gap-1.5">
            <span
              className={`text-lg font-extrabold font-mono ${
                b.median_pct > 0 ? 'text-red-600' : b.median_pct < 0 ? 'text-green-600' : 'text-slate-500'
              }`}
            >
              {b.median_pct > 0 ? '+' : ''}
              {b.median_pct.toFixed(2)}%
            </span>
            <span className="text-[10px] font-bold text-slate-400">中位</span>
          </div>
        </div>
        <div className="flex items-center gap-2 flex-shrink-0">
          <Coins className="w-4 h-4 text-amber-500" />
          <div className="flex items-baseline gap-1.5">
            <span className={`text-lg font-extrabold font-mono ${sentiColor}`}>{sentiment.toFixed(0)}</span>
            <span className="text-[10px] font-bold text-slate-400">情绪 · {sentiLabel}</span>
          </div>
        </div>
      </div>
    </SectionCard>
  );
};
