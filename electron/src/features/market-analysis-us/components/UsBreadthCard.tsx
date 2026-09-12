/** 美股市场温度计 —— 紧凑版
 *
 * 美股无涨跌停（有 LULD 熔断），±5% 为**异动**口径而非涨停。
 * 成交额为美元原始值（daily_forward 的 amount 列即美元金额，无换算系数）。
 *
 * 首屏右栏空间宝贵，故做成「一行指标 + 一条涨跌占比条」的紧凑形态，
 * 不做大卡片矩阵。
 */

import React from 'react';
import { Flame, Coins, Scale } from 'lucide-react';
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

  const sentiment = Math.max(0, Math.min(100, b.sentiment_score || 50));
  const sentiColor =
    sentiment >= 60 ? 'text-red-600' : sentiment <= 40 ? 'text-green-600' : 'text-amber-600';
  const sentiLabel = sentiment >= 60 ? '强势' : sentiment <= 40 ? '弱势' : '中性';

  const cells = [
    { label: '上涨', value: b.advance_count, tone: 'text-red-600', sub: `${advPct.toFixed(0)}%` },
    { label: '下跌', value: b.decline_count, tone: 'text-green-600', sub: `${decPct.toFixed(0)}%` },
    { label: '平盘', value: b.flat_count, tone: 'text-slate-500', sub: '' },
    { label: `异动涨≥${b.big_move_threshold}%`, value: b.big_up_count, tone: 'text-orange-600', sub: '' },
    { label: `异动跌≤-${b.big_move_threshold}%`, value: b.big_down_count, tone: 'text-sky-600', sub: '' },
  ];

  return (
    <SectionCard
      className="!p-2.5"
      title={
        <span className="flex items-center gap-1.5">
          <Flame className="w-3.5 h-3.5 text-blue-600" />
          市场温度计
          <span className="text-[9px] font-normal text-slate-400">
            成交 {fmtInt(b.total_turnover_yi)} 亿美元
          </span>
        </span>
      }
      extra={
        <span className="flex items-center gap-2">
          <span className="flex items-center gap-1" title="全市场涨跌幅中位数（对拆股等异常值更稳健）">
            <Scale className="w-3 h-3 text-slate-400" />
            <span
              className={`text-[11px] font-extrabold font-mono ${
                b.median_pct > 0
                  ? 'text-red-600'
                  : b.median_pct < 0
                  ? 'text-green-600'
                  : 'text-slate-500'
              }`}
            >
              {b.median_pct > 0 ? '+' : ''}
              {b.median_pct.toFixed(2)}%
            </span>
          </span>
          <span className="flex items-center gap-1">
            <Coins className="w-3 h-3 text-amber-500" />
            <span className={`text-[11px] font-extrabold font-mono ${sentiColor}`}>
              {sentiment.toFixed(0)}
            </span>
            <span className="text-[9px] font-bold text-slate-400">{sentiLabel}</span>
          </span>
        </span>
      }
    >
      {loading && !breadth ? (
        <div className="py-3 text-center text-[11px] text-slate-400">加载中…</div>
      ) : (
        <div className="flex flex-col gap-1.5">
          {/* 五档家数：一行紧凑指标 */}
          <div className="grid grid-cols-5 gap-1">
            {cells.map((c) => (
              <div
                key={c.label}
                className="rounded-lg bg-slate-50/80 border border-slate-100 px-1 py-1 flex flex-col items-center"
              >
                <span className="text-[9px] font-bold text-slate-400 whitespace-nowrap">
                  {c.label}
                </span>
                <span className={`text-[13px] font-extrabold font-mono leading-tight ${c.tone}`}>
                  {fmtInt(c.value)}
                </span>
                {c.sub && <span className="text-[9px] font-mono text-slate-400">{c.sub}</span>}
              </div>
            ))}
          </div>
          {/* 涨跌占比条 */}
          <div className="h-1.5 rounded-full bg-slate-100 overflow-hidden flex">
            <div className="bg-red-500 h-full" style={{ width: `${advPct}%` }} />
            <div className="bg-green-500 h-full" style={{ width: `${decPct}%` }} />
          </div>
        </div>
      )}
    </SectionCard>
  );
};
