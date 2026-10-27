import React from 'react';
import type { IndexSentiment } from '../services/sentimentService';

const HORIZONS = [5, 10, 20] as const;

interface Props {
  indices: Record<string, IndexSentiment>;
  order: readonly string[];
  selected: string;
  onSelect: (key: string) => void;
}

const fmt = (v: number): string => `${v > 0 ? '+' : ''}${v.toFixed(2)}%`;

/**
 * 多周期模型预测矩阵：三个指数 × T+5/T+10/T+20。
 *
 * 主指标是「上涨概率」——预测值本身只有零点几个点，接近 50% 的概率就是
 * 「没有方向优势」，这样展示比给一条精确到 0.01% 的预测线诚实。
 */
export const ForecastMatrix: React.FC<Props> = ({ indices, order, selected, onSelect }) => {
  const rows = order.filter((k) => indices[k]);
  if (!rows.length) return null;
  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-center justify-between">
        <span className="text-[10px] font-extrabold text-slate-600">多周期模型预测（广度 + 量能）</span>
        <span className="text-[9px] text-slate-400">点行切换指数 · 概率≈50% ＝ 没有方向优势</span>
      </div>
      <div className="grid grid-cols-[86px_1fr_1fr_1fr] items-center gap-2 text-[10px]">
        <span className="text-slate-400" />
        {HORIZONS.map((h) => (
          <span key={h} className="text-center font-bold text-slate-400">
            T+{h}
          </span>
        ))}
        {rows.map((k) => {
          const it = indices[k];
          const active = k === selected;
          return (
            <React.Fragment key={k}>
              <button
                onClick={() => onSelect(k)}
                className={`text-left truncate transition-colors ${
                  active ? 'font-extrabold text-violet-700' : 'font-bold text-slate-600 hover:text-slate-900'
                }`}
              >
                {it.name}
              </button>
              {HORIZONS.map((h) => {
                const f = it.forecast?.[String(h)];
                if (!f || f.pred === null) {
                  return (
                    <span key={h} className="text-center text-slate-300 font-mono">
                      —
                    </span>
                  );
                }
                const up = f.pred >= 0;
                return (
                  <div
                    key={h}
                    className={`rounded-md py-0.5 text-center ${
                      active ? 'bg-violet-50/70' : ''
                    }`}
                    title={`预测 ${fmt(f.pred)} · 上涨概率 ${f.prob_up === null ? '—' : Math.round(f.prob_up * 100) + '%'} · 八成区间 ${f.p10?.toFixed(1)}% ~ ${f.p90?.toFixed(1)}%`}
                  >
                    <div className={`font-mono font-extrabold leading-tight ${up ? 'text-red-600' : 'text-emerald-600'}`}>
                      {fmt(f.pred)}
                    </div>
                    <div className="text-[9px] text-slate-400 leading-tight">
                      {f.prob_up === null ? '—' : `涨 ${Math.round(f.prob_up * 100)}%`}
                    </div>
                  </div>
                );
              })}
            </React.Fragment>
          );
        })}
      </div>
    </div>
  );
};
