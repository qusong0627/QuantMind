/**
 * 维度覆盖条：一格一维，**有色=已算 / 斜纹虚线=缺省**（悬停看原因）。
 *
 * 为什么要有这条：一个 88 分的对象如果只算了 2 维，读数应该是「高分但薄」而不是
 * 和满维对象并列。把缺省从条上抹掉，分数就显得比实际厚。
 */

import React, { useMemo } from 'react';
import type { EvalScoreRow } from '../types/evalCenter';
import { dimensionCoverage } from './evalInsightModel';
import { NO_EVIDENCE_CARD, RED_LINE_BAR, SCORED_BAR } from './evalTones';

interface DimensionCoverageBarProps {
  row: EvalScoreRow | null | undefined;
  /** 是否显示 `scored/total` 文字（榜行紧凑时可关） */
  showCount?: boolean;
  className?: string;
}

export const DimensionCoverageBar: React.FC<DimensionCoverageBarProps> = ({
  row,
  showCount = true,
  className = '',
}) => {
  const coverage = useMemo(() => dimensionCoverage(row), [row]);
  if (coverage.total === 0) return null;

  return (
    <div className={`flex items-center gap-2 ${className}`}>
      <div className="flex flex-1 items-center gap-[3px] min-w-0">
        {coverage.cells.map((cell) => {
          const state = !cell.scored
            ? `缺省 · ${cell.note || '无原因'}`
            : `${cell.score}${cell.redLine ? '（红线）' : ''}${cell.note ? ` · ${cell.note}` : ''}`;
          const className = !cell.scored
            ? NO_EVIDENCE_CARD
            : cell.redLine
              ? RED_LINE_BAR
              : SCORED_BAR;
          return (
            <span
              key={cell.key}
              title={`${cell.label}：${state}`}
              className={`h-2 flex-1 min-w-[10px] rounded-[5px] ${className}`}
            />
          );
        })}
      </div>
      {showCount && (
        <span className="shrink-0 text-[10px] font-mono tabular-nums text-slate-400">
          {coverage.scored}/{coverage.total}
        </span>
      )}
    </div>
  );
};
