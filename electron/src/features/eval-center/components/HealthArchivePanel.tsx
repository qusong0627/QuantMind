/**
 * 策略体检档案面板：最新结论 + **晋级门禁预演**（与执行点同源）+ 结论历史。
 *
 * 从 `EvalCenterPanel` 抽出（面板只做编排）；除排版口径外行为与抽出前一致。
 *
 * 2026-10-09 改版：结论块由整卡底色（大块粉/红满屏）改为白底 + 左色边 +
 * 徽章化结论 —— 与详情页主卡同一语言，门禁放行/拦截用独立语义色（绿=放行），
 * 不再随结论色漂移。
 */

import React from 'react';
import type { StrategyHealthArchive } from '../types/evalCenter';
import { gradeColor, gradeMeta } from './evalCenterModel';
import { CARD } from '../../desk/components/cardKit';

interface HealthArchivePanelProps {
  archive: StrategyHealthArchive;
}

export const HealthArchivePanel: React.FC<HealthArchivePanelProps> = ({ archive }) => {
  const latest = archive.latest;
  const meta = gradeMeta(latest?.verdict, false);
  const color = gradeColor(latest?.verdict);
  return (
    <div className="space-y-3">
      <section
        className="rounded-2xl border border-slate-200/80 bg-white p-4 shadow-[0_1px_2px_rgba(15,23,42,0.04)]"
        style={{ borderLeft: `4px solid ${color}` }}
      >
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <span className={`shrink-0 rounded-lg border px-2 py-0.5 text-xs font-extrabold ${meta.className}`}>
                {latest ? String(latest.verdict || '—') : '—'}
              </span>
              <span className="text-base font-bold text-slate-800">
                {latest ? latest.verdict_label || latest.verdict : '暂无体检记录'}
              </span>
            </div>
            {latest && (
              <div className="mt-1 text-[11px] text-slate-400">
                可信度 <b className="tabular-nums text-slate-600">{Math.round(latest.confidence ?? 0)}</b>/100
                {latest.evidence_source ? ` · 证据源 ${latest.evidence_source}` : ''}
                {latest.snapshot_date ? ` · ${latest.snapshot_date}` : ''}
              </div>
            )}
          </div>
          <span
            className={`shrink-0 rounded-full border px-2.5 py-1 text-[11px] font-bold ${
              archive.gate.allowed
                ? 'border-emerald-200 bg-emerald-50 text-emerald-700'
                : 'border-red-200 bg-red-50 text-red-700'
            }`}
          >
            晋级门禁：{archive.gate.allowed ? '放行' : '拦截'}
          </span>
        </div>
        <div className="mt-2 text-xs text-slate-600">{archive.gate.note}</div>
        {latest && (latest.reasons?.length > 0 || latest.suggestions?.length > 0) && (
          <div className="mt-2 space-y-1 border-t border-slate-100 pt-2 text-xs text-slate-600">
            {latest.reasons?.map((reason, index) => (
              <div key={`r-${index}`} className="flex gap-1.5">
                <span className="text-slate-300">·</span>
                <span>{reason}</span>
              </div>
            ))}
            {latest.suggestions?.map((suggestion, index) => (
              <div key={`s-${index}`} className="flex gap-1.5 text-slate-500">
                <span className="shrink-0 text-slate-300">建议 {index + 1}</span>
                <span>{suggestion}</span>
              </div>
            ))}
          </div>
        )}
      </section>

      <div className={CARD}>
        <h4 className="mb-2 text-sm font-semibold text-slate-800">结论历史</h4>
        {archive.history.length === 0 ? (
          <p className="text-xs text-slate-400">
            暂无历史（体检在回测完成后自动生成；月度复检每月留档）
          </p>
        ) : (
          <div className="space-y-1.5">
            {archive.history.map((point, index) => {
              const pointMeta = gradeMeta(point.verdict, false);
              return (
                <div key={index} className="flex items-center gap-2 text-xs">
                  <span className="w-24 text-slate-500">{point.snapshot_date || '—'}</span>
                  <span className={`rounded border px-1.5 py-0.5 ${pointMeta.className}`}>
                    {point.verdict || '—'}
                  </span>
                  <span className="text-slate-600">可信度 {Math.round(point.confidence ?? 0)}</span>
                  {point.evidence_source && (
                    <span className="text-slate-400">（{point.evidence_source}）</span>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
};
