/**
 * 总览带（三段式第 1 段）：一类对象的整体读数 —— 等级分布堆叠条 + 四个微统计块。
 *
 * 「证据覆盖率」这一格是本带存在的理由：平均分会被缺维的薄卡拉高，所以「N 个对象里
 * 几个有 ≥3 维实证」必须与平均分并排出现，且说明文字固定带出统计口径。
 */

import React, { useMemo } from 'react';
import { BarChart3 } from 'lucide-react';
import type { EvalScoreRow } from '../types/evalCenter';
import { gradeColor, gradeMeta } from './evalCenterModel';
import { overviewStats } from './evalInsightModel';
import { CARD, CardHeader, StatTile } from '../../desk/components/cardKit';

interface EvalOverviewBandProps {
  rows: EvalScoreRow[];
}

export const EvalOverviewBand: React.FC<EvalOverviewBandProps> = ({ rows }) => {
  const stats = useMemo(() => overviewStats(rows), [rows]);

  return (
    <section className={CARD}>
      <CardHeader
        icon={<BarChart3 className="h-4 w-4" />}
        title="本类总览"
        meta={
          <span className="text-[10px] text-slate-400">{stats.note}</span>
        }
      />

      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <StatTile label="对象数" value={stats.total} tone="slate" />
        <StatTile
          label="平均分"
          value={stats.avg === null ? '—' : stats.avg.toFixed(1)}
          tone="blue"
        />
        <StatTile
          label="触红线对象"
          value={stats.redLineCount}
          tone={stats.redLineCount > 0 ? 'amber' : 'slate'}
        />
        <StatTile
          label="证据覆盖率"
          value={`${Math.round(stats.coverageRate * 100)}%`}
          tone={stats.coverageRate >= 0.6 ? 'blue' : 'amber'}
          className="col-span-1"
        />
      </div>

      {/* 等级分布堆叠条：一眼看出这一批是「高分多」还是「一堆 D」 */}
      <div className="mt-3">
        <div className="flex h-2.5 w-full overflow-hidden rounded-full bg-slate-100">
          {stats.counts.map((entry) => (
            <span
              key={entry.grade}
              title={`${gradeMeta(entry.grade === '?' ? null : entry.grade).label} × ${entry.count}`}
              style={{
                width: `${(entry.count / Math.max(1, stats.total)) * 100}%`,
                background: gradeColor(entry.grade === '?' ? null : entry.grade),
              }}
            />
          ))}
        </div>
        <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1">
          {stats.counts.map((entry) => {
            const meta = gradeMeta(entry.grade === '?' ? null : entry.grade);
            return (
              <span key={entry.grade} className="inline-flex items-center gap-1.5 text-[11px]">
                <span
                  className="h-1.5 w-1.5 rounded-full shrink-0"
                  style={{ background: gradeColor(entry.grade === '?' ? null : entry.grade) }}
                />
                <span className={`font-bold ${meta.className.split(' ').find((t) => t.startsWith('text-'))}`}>
                  {entry.grade === '?' ? '未评级' : entry.grade}
                </span>
                <span className="text-slate-400 tabular-nums">× {entry.count}</span>
              </span>
            );
          })}
        </div>
      </div>
    </section>
  );
};
