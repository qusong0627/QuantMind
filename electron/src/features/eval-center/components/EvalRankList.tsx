/**
 * 榜单（三段式第 2 段）：一行一个对象 —— 名次 / 分数条 / 等级 / 维度覆盖条 / 实测量。
 *
 * 「实测量」是这一行的第二读数：分数是加权合成，`insightFor` 给的是最能证伪它的那个
 * 原始量（模型→实测 RankIC、因子→ICIR、策略→年化/回撤、账户→利用率、每日选股→T+H 超额）。
 * 给不出数时保留位置并写原因（虚线态），而不是把这一列删掉——删掉就等于宣称它没问题。
 *
 * 2026-10-09 改版：卡片从 4–5 行压到 3 行 —— 长 id 只留短的（其余进悬停）、口径解释
 * 全部收进 title、红线从独立胶囊降为 ⚠ 标记；「反转可用」这类关键结论保留可见徽标
 * （见 `Insight.badge`），不因收悬停而丢失。
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

/** 内联副标题的可见长度上限：超过则只进悬停（模型长 id / 回测哈希不进视线） */
const SECONDARY_INLINE_MAX = 16;

export const EvalRankList: React.FC<EvalRankListProps> = ({ rows, selectedId, onSelect }) => (
  <div className="space-y-2">
    {rows.map((row, index) => {
      const meta = gradeMeta(row.grade, row.low_confidence);
      const labels = rowLabels(row);
      const color = gradeColor(row.grade);
      const insight = insightFor(row);
      const active = selectedId === row.object_id;
      const score = typeof row.score === 'number' ? row.score : null;
      const shortSecondary =
        labels.secondary && labels.secondary.length <= SECONDARY_INLINE_MAX ? labels.secondary : null;
      const redLines = row.red_line_failed || [];
      // 体检留档的「维度」不是评分维度（是结论字段），覆盖条在此处只会误报 0/N
      const showCoverage = row.object_type !== 'strategy_health';
      return (
        <button
          key={row.object_id}
          type="button"
          onClick={() => onSelect(row.object_id)}
          className={`w-full text-left rounded-2xl border p-2.5 transition-all ${
            active
              ? 'border-blue-300 bg-blue-50/50 ring-1 ring-blue-200'
              : 'border-slate-200/80 bg-white hover:border-slate-300 hover:shadow-[0_2px_8px_rgba(15,23,42,0.06)]'
          }`}
        >
          {/* 行 1：名次 · 名称 ·（短 id）· 红线 · 等级 · 日期 */}
          <div className="flex items-center gap-2">
            <span className="shrink-0 font-mono text-[11px] tabular-nums text-slate-300">
              {String(index + 1).padStart(2, '0')}
            </span>
            <span
              className="min-w-0 truncate text-[13px] font-semibold text-slate-800"
              title={labels.secondary ? `${labels.primary} · ${labels.secondary}` : labels.primary}
            >
              {labels.primary}
            </span>
            {shortSecondary && (
              <span className="hidden shrink-0 font-mono text-[10px] text-slate-400 xl:inline">
                {shortSecondary}
              </span>
            )}
            {redLines.length > 0 && (
              <span className="shrink-0" title={`红线：${redLines.join('、')}`}>
                <AlertTriangle className="h-3 w-3 text-amber-500" />
              </span>
            )}
            <span
              className={`shrink-0 rounded-lg border px-1.5 py-0.5 text-[11px] font-extrabold ${meta.className}`}
            >
              {gradeLetter(row.grade)}
              {meta.isLowConfidence ? '†' : ''}
            </span>
            <span className="ml-auto shrink-0 text-[10px] tabular-nums text-slate-400">
              {row.snapshot_date || ''}
            </span>
          </div>

          {/* 行 2：分数条 + 分数 */}
          <div className="mt-1.5 flex items-center gap-2">
            <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-slate-100">
              {score !== null && (
                <div
                  className="h-full rounded-full"
                  style={{ width: `${Math.max(0, Math.min(100, score))}%`, background: color }}
                />
              )}
            </div>
            <span
              className="w-9 shrink-0 text-right text-xs font-bold tabular-nums"
              style={{ color: score === null ? '#94a3b8' : color }}
            >
              {score === null ? '未评分' : score.toFixed(1)}
            </span>
          </div>

          {/* 行 3：维度覆盖条（有色=已算 / 斜纹=缺省，悬停看原因）+ 实测量 */}
          {(showCoverage || insight) && (
            <div className="mt-1.5 flex items-center gap-3">
              {showCoverage && (
                <div className="min-w-0 flex-1">
                  <DimensionCoverageBar row={row} />
                </div>
              )}
              {insight && (
                <span className="flex shrink-0 items-center gap-1 text-[10px] text-slate-400" title={insight.hint}>
                  <span className="hidden sm:inline">{insight.label}</span>
                  <span
                    className={`rounded-md border px-1.5 py-0.5 text-[11px] font-bold tabular-nums ${
                      insight.missing ? TONE_CHIP.pending : ''
                    } ${insight.missing ? '' : TONE_TEXT[insight.tone]}`}
                  >
                    {insight.value}
                  </span>
                  {insight.badge && (
                    <span className="rounded border border-amber-200 bg-amber-50 px-1 py-px text-[10px] font-bold text-amber-700">
                      {insight.badge}
                    </span>
                  )}
                </span>
              )}
            </div>
          )}
        </button>
      );
    })}
  </div>
);
