/**
 * 策略体检档案面板：最新结论 + **晋级门禁预演**（与执行点同源）+ 结论历史。
 *
 * 从 `EvalCenterPanel` 抽出（面板只做编排）；除排版口径外行为与抽出前一致。
 */

import React from 'react';
import type { StrategyHealthArchive } from '../types/evalCenter';
import { gradeMeta } from './evalCenterModel';
import { CARD } from '../../desk/components/cardKit';

interface HealthArchivePanelProps {
  archive: StrategyHealthArchive;
}

export const HealthArchivePanel: React.FC<HealthArchivePanelProps> = ({ archive }) => {
  const latest = archive.latest;
  const meta = gradeMeta(latest?.verdict, false);
  return (
    <div className="space-y-3">
      <div className={`rounded-2xl border p-4 ${meta.className}`}>
        <div className="flex items-center justify-between gap-3">
          <div>
            <div className="text-base font-bold">
              {latest ? `结论：${latest.verdict_label || latest.verdict}` : '暂无体检记录'}
            </div>
            {latest && (
              <div className="mt-0.5 text-xs opacity-80">
                可信度 {Math.round(latest.confidence ?? 0)}/100
                {latest.evidence_source ? ` · 证据源 ${latest.evidence_source}` : ''}
                {latest.snapshot_date ? ` · ${latest.snapshot_date}` : ''}
              </div>
            )}
          </div>
          <span className="rounded-full border border-current bg-white/70 px-2 py-1 text-xs">
            晋级门禁：{archive.gate.allowed ? '放行' : '拦截'}
          </span>
        </div>
        <div className="mt-2 text-xs opacity-90">{archive.gate.note}</div>
        {latest && (latest.reasons?.length > 0 || latest.suggestions?.length > 0) && (
          <div className="mt-2 space-y-0.5 text-xs">
            {latest.reasons?.map((reason, index) => (
              <div key={`r-${index}`}>· {reason}</div>
            ))}
            {latest.suggestions?.map((suggestion, index) => (
              <div key={`s-${index}`} className="opacity-90">
                建议 {index + 1}: {suggestion}
              </div>
            ))}
          </div>
        )}
      </div>

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
