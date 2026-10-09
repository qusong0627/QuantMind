/**
 * 总览带（三段式第 1 段）：一行的整体读数 —— 等级分布堆叠条 + 图例 + 四个微统计。
 *
 * 「证据覆盖率」这一格是本带存在的理由：平均分会被缺维的薄卡拉高，所以「N 个对象里
 * 几个有 ≥3 维实证」必须与平均分并排出现，且说明文字固定带出统计口径（悬停可见）。
 *
 * 2026-10-09 改版：原四个大统计块（每块只为放一个数字）竖直吃掉约 200px，把榜单和
 * 详情挤出首屏；现在压成一条横带 —— 分布条（flex-1）+ 图例 + 内联统计，口径解释全在
 * title 悬停里，信息不减、高度约三分之一。
 */

import React, { useMemo } from 'react';
import { BarChart3 } from 'lucide-react';
import type { EvalScoreRow } from '../types/evalCenter';
import { gradeColor, gradeMeta } from './evalCenterModel';
import { overviewStats } from './evalInsightModel';

interface EvalOverviewBandProps {
  rows: EvalScoreRow[];
}

/** 内联微统计：标签 + 加粗数值（触发态给琥珀色） */
const MiniStat: React.FC<{
  label: string;
  value: React.ReactNode;
  title?: string;
  alert?: boolean;
}> = ({ label, value, title, alert = false }) => (
  <span className="inline-flex items-baseline gap-1 whitespace-nowrap" title={title}>
    <span className="text-slate-400">{label}</span>
    <span className={`font-bold tabular-nums ${alert ? 'text-amber-600' : 'text-slate-800'}`}>
      {value}
    </span>
  </span>
);

export const EvalOverviewBand: React.FC<EvalOverviewBandProps> = ({ rows }) => {
  const stats = useMemo(() => overviewStats(rows), [rows]);
  const coveragePct = Math.round(stats.coverageRate * 100);

  return (
    <section className="flex flex-wrap items-center gap-x-5 gap-y-2 rounded-2xl border border-slate-200/80 bg-white px-4 py-2.5 shadow-[0_1px_2px_rgba(15,23,42,0.04)]">
      {/* 等级分布堆叠条：一眼看出这一批是「高分多」还是「一堆 D」 */}
      <div className="flex min-w-[220px] flex-1 items-center gap-2.5">
        <BarChart3 className="h-4 w-4 shrink-0 text-blue-600" />
        <div className="flex h-2 flex-1 overflow-hidden rounded-full bg-slate-100" title={stats.note}>
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
      </div>

      {/* 图例：评级 × 数量 */}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
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

      <span className="hidden h-4 w-px bg-slate-200 sm:block" />

      {/* 内联统计（口径解释在悬停里） */}
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px]">
        <MiniStat label="对象" value={stats.total} title="本类评分卡对象数" />
        <MiniStat
          label="均分"
          value={stats.avg === null ? '—' : stats.avg.toFixed(1)}
          title="有效分数的算术平均（缺分对象不计入）"
        />
        <MiniStat
          label="触线"
          value={stats.redLineCount}
          alert={stats.redLineCount > 0}
          title="触发过红线的对象数（红线会把总分压到 60 以下）"
        />
        <MiniStat
          label="覆盖"
          value={`${coveragePct}%`}
          alert={stats.coverageRate < 0.6}
          title={stats.note}
        />
      </div>
    </section>
  );
};
