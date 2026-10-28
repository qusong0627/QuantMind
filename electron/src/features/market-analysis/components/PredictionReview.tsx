/**
 * 预测复盘：把「当时模型怎么预测」与「后来实际怎么走」摆在一起。
 *
 * 数据来源：
 * - ``forecast[h].pred / prob_up``：as_of 当天按 walk-forward 口径拟合的预测（无前视）；
 * - ``actual``：as_of 之后真实走出的路径（不足 20 天时给部分路径）；
 * - ``forecast[h].accuracy / review``：同一套样本外预测的历史命中率与最近若干次明细。
 *
 * 这里只做展示，不把「历史命中率」包装成未来承诺 —— 命中率≈50% 就是没有方向优势。
 */
import React from 'react';
import type { IndexSentiment } from '../services/sentimentService';

const HORIZONS = [5, 10, 20] as const;

const fmt = (v: number | null | undefined): string =>
  v === null || v === undefined ? '—' : `${v > 0 ? '+' : ''}${v.toFixed(2)}%`;

interface Props {
  block: IndexSentiment;
  asOf: boolean;
  horizon: number;
}

export const PredictionReview: React.FC<Props> = ({ block, asOf, horizon }) => {
  const rows = HORIZONS.map((h) => {
    const f = block.forecast?.[String(h)];
    const realized = block.actual?.find((p) => p.k === h)?.pct ?? null;
    const walked = block.actual_days ?? 0;
    const pred = f?.pred ?? null;
    const hit = pred !== null && realized !== null ? pred >= 0 === realized >= 0 : null;
    let status: string;
    if (realized !== null) status = fmt(realized);
    else if (walked > 0 && walked < h) status = `已走 ${walked}/${h} 天`;
    else if (asOf) status = `未走完（${walked}/${h}）`;
    else status = '待走出';
    return { h, f, pred, realized, hit, status };
  });
  const any = rows.some((r) => r.f?.pred !== null && r.f?.pred !== undefined);
  if (!any) return null;

  return (
    <div className="rounded-xl border border-slate-200 bg-slate-50/60 px-3 py-2 flex flex-col gap-1.5">
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <span className="text-[10px] font-extrabold text-slate-600">
          预测复盘 · 当时模型预测 vs 之后实际
        </span>
        <span className="text-[9px] text-slate-400">
          {asOf ? '点击扇形图里任意历史交易日可切换' : '先选一个历史日期，才能看到实际结果'}
        </span>
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
        {rows.map(({ h, f, pred, hit, status }) => {
          const acc = f?.accuracy;
          return (
            <div key={h} className="rounded-lg bg-white border border-slate-100 px-2.5 py-1.5">
              <div className="flex items-center justify-between">
                <span className="text-[10px] font-extrabold text-slate-500">T+{h}</span>
                <span
                  className={`text-[10px] font-bold ${
                    hit === null ? 'text-slate-300' : hit ? 'text-emerald-600' : 'text-rose-500'
                  }`}
                >
                  {hit === null ? '—' : hit ? '✓ 方向对' : '✗ 方向反'}
                </span>
              </div>
              <div className="flex items-baseline gap-1.5 text-[11px] font-mono mt-0.5">
                <span className={pred !== null && pred >= 0 ? 'text-red-600' : 'text-emerald-600'}>
                  {fmt(pred)}
                </span>
                <span className="text-[9px] text-slate-400">
                  涨{typeof f?.prob_up === 'number' ? Math.round(f.prob_up * 100) : '—'}%
                </span>
                <span className="ml-auto text-slate-500">{status}</span>
              </div>
              {acc && (
                <div className="flex items-center justify-between gap-1 mt-0.5 text-[9px] text-slate-400">
                  <span title="方向命中率（非重叠样本）· 平均绝对误差 · 秩相关">
                    历史命中 {acc.hit === null ? '—' : `${Math.round(acc.hit * 100)}%`}
                    <span className="text-slate-300"> · </span>MAE {acc.mae.toFixed(2)}%
                    {acc.ic !== null && (
                      <>
                        <span className="text-slate-300"> · </span>IC {acc.ic.toFixed(2)}
                      </>
                    )}
                  </span>
                  <span className="flex items-center gap-[2px]" title={`最近 ${f?.review?.length ?? 0} 次（间隔 ${h} 日）`}>
                    {(f?.review ?? []).map((r) => (
                      <span
                        key={r.date}
                        title={`${r.date}：预测 ${fmt(r.pred)}，实际 ${fmt(r.actual)}`}
                        className={`inline-block w-1.5 h-1.5 rounded-full ${
                          r.hit ? 'bg-emerald-400' : 'bg-rose-400'
                        }`}
                      />
                    ))}
                  </span>
                </div>
              )}
            </div>
          );
        })}
      </div>
      <p className="text-[9px] text-slate-400">
        命中率按「每 {horizon} 天取一个不重叠样本」统计；历史命中率不等于未来胜率。
      </p>
    </div>
  );
};
