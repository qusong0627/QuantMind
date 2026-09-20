/**
 * 榜单（三段式第 2 段）：一行一个对象 —— 名次 / 分数条 / 等级 / 维度覆盖条 / 实测量。
 *
 * 「实测量」是这一行的第二读数：分数是加权合成，`insightFor` 给的是最能证伪它的那个
 * 原始量（模型→实测 RankIC、因子→ICIR、策略→年化/回撤、账户→利用率、每日选股→T+H 超额）。
 * 给不出数时保留位置并写原因（虚线态），而不是把这一列删掉——删掉就等于宣称它没问题。
 */

import React from 'react';
import { AlertTriangle } from 'lucide-react';
import type { EvalScoreRow } from '../types/evalCenter';
import { gradeColor, gradeMeta, rowLabels } from './evalCenterModel';
import { insightFor } from './evalInsightModel';
import { TONE_CHIP, TONE_TEXT } from './evalTones';
import { DimensionCoverageBar } from './DimensionCoverageBar';

interface EvalRankListProps {
  rows: EvalScoreRow[];
  selectedId: string;
  onSelect: (objectId: string) => void;
}

function gradeLetter(grade: string | null | undefined): string {
  return String(grade || '').replace('†', '').trim() || '—';
}

export const EvalRankList: React.FC<EvalRankListProps> = ({ rows, selectedId, onSelect }) => (
  <div className="space-y-2">
    {rows.map((row, index) => {
      const meta = gradeMeta(row.grade, row.low_confidence);
      const labels = rowLabels(row);
      const color = gradeColor(row.grade);
      const insight = insightFor(row);
      const active = selectedId === row.object_id;
      const score = typeof row.score === 'number' ? row.score : null;
      return (
        <button
          key={row.object_id}
          type="button"
          onClick={() => onSelect(row.object_id)}
          className={`w-full text-left rounded-2xl border p-3 transition-all ${
            active
              ? 'border-blue-400 bg-blue-50/40 ring-1 ring-blue-200 shadow-sm'
              : 'border-slate-200/80 bg-white hover:border-slate-300 hover:shadow-[0_2px_8px_rgba(15,23,42,0.06)]'
          }`}
        >
          <div className="flex items-start gap-3">
            <span className="mt-0.5 shrink-0 font-mono text-[11px] tabular-nums text-slate-300">
              {String(index + 1).padStart(2, '0')}
            </span>

            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2">
                <span className="min-w-0 truncate text-[13px] font-semibold text-slate-800" title={labels.primary}>
                  {labels.primary}
                </span>
                <span
                  className={`shrink-0 rounded-lg border px-1.5 py-0.5 text-[11px] font-extrabold ${meta.className}`}
                >
                  {gradeLetter(row.grade)}
                  {meta.isLowConfidence ? '†' : ''}
                </span>
              </div>
              {labels.secondary && (
                <div className="truncate font-mono text-[10px] text-slate-400" title={labels.secondary}>
                  {labels.secondary}
                </div>
              )}

              {/* 分数条 + 分数 */}
              <div className="mt-2 flex items-center gap-2">
                <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-slate-100">
                  {score !== null && (
                    <div
                      className="h-full rounded-full"
                      style={{ width: `${Math.max(0, Math.min(100, score))}%`, background: color }}
                    />
                  )}
                </div>
                <span
                  className="shrink-0 text-xs font-bold tabular-nums"
                  style={{ color: score === null ? '#94a3b8' : color }}
                >
                  {score === null ? '未评分' : score.toFixed(1)}
                </span>
              </div>

              {/* 维度覆盖条（有色=已算 / 斜纹=缺省，悬停看原因） */}
              <DimensionCoverageBar row={row} className="mt-2" />

              {/* 实测量：给不出数也给位置与原因 */}
              {insight && (
                <div className="mt-2 flex flex-wrap items-center gap-1.5">
                  <span className="text-[10px] text-slate-400">{insight.label}</span>
                  <span
                    title={insight.hint}
                    className={`rounded-md border px-1.5 py-0.5 text-[11px] font-bold tabular-nums ${
                      insight.missing ? TONE_CHIP.pending : ''
                    } ${insight.missing ? '' : TONE_TEXT[insight.tone]}`}
                  >
                    {insight.value}
                  </span>
                  {!insight.missing && (
                    <span className="hidden truncate text-[10px] text-slate-400 sm:inline" title={insight.hint}>
                      {insight.hint}
                    </span>
                  )}
                </div>
              )}

              {row.red_line_failed?.length > 0 && (
                <div className="mt-1.5 inline-flex items-center gap-1 rounded-full bg-amber-50 px-2 py-0.5 text-[10px] text-amber-800 border border-amber-200">
                  <AlertTriangle className="h-3 w-3" />
                  红线：{row.red_line_failed.join('、')}
                </div>
              )}
            </div>

            <span className="shrink-0 pt-0.5 text-[10px] tabular-nums text-slate-400">
              {row.snapshot_date || ''}
            </span>
          </div>
        </button>
      );
    })}
  </div>
);
