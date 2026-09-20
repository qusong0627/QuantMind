/**
 * 详情（三段式第 3 段）：一屏把「这个分数凭什么」讲完。
 *
 * 顺序即论证顺序：实测量（最容易被独立核实的那一个数）→ 分维得分 → 换手成本 →
 * 一问一图（长序列）→ 历史曲线 → 页脚（来源/口径/时间戳）。
 * 简单模式只留第一屏，其余交给「专业」。
 */

import React, { useMemo } from 'react';
import { Info, TrendingUp } from 'lucide-react';
import type { EvalScoreRow } from '../types/evalCenter';
import { gradeColor, gradeMeta, historySeries, rowLabels } from './evalCenterModel';
import { evidenceFootnote, insightFor, orderedDimensionViews } from './evalInsightModel';
import { RED_LINE_BAR, TONE_CHIP, TONE_TEXT } from './evalTones';
import { DimensionCoverageBar } from './DimensionCoverageBar';
import { CostCompareBars } from './CostCompareBars';
import { EvalSeriesSection } from './EvalSeriesSection';
import { EChartsChart } from '../../../components/common/EChartsChart';
import { CARD, CardHeader } from '../../desk/components/cardKit';
import { TermTooltip } from '../../shared/TermTooltip';

interface EvalDetailProps {
  row: EvalScoreRow | null;
  objectType: string;
  history: EvalScoreRow[];
  isSimple: boolean;
}

const InsightChip: React.FC<{ row: EvalScoreRow }> = ({ row }) => {
  const insight = insightFor(row);
  if (!insight) return null;
  return (
    <div className="flex flex-wrap items-baseline gap-2">
      <span className="text-[10px] font-semibold tracking-wide text-slate-400">{insight.label}</span>
      <span
        className={`rounded-lg border px-2 py-0.5 text-base font-bold tabular-nums ${
          insight.missing ? TONE_CHIP.pending : TONE_TEXT[insight.tone]
        }`}
      >
        {insight.value}
      </span>
      <span className="text-[10px] leading-4 text-slate-400">{insight.hint}</span>
    </div>
  );
};

const HistoryCurve: React.FC<{ history: EvalScoreRow[]; color: string }> = ({ history, color }) => {
  const series = useMemo(() => historySeries(history), [history]);
  const option = useMemo(() => {
    if (series.dates.length < 2) return null;
    return {
      grid: { left: 40, right: 16, top: 16, bottom: 26 },
      xAxis: {
        type: 'category',
        data: series.dates,
        axisLabel: { color: '#94a3b8', fontSize: 10, hideOverlap: true },
        axisLine: { lineStyle: { color: '#e2e8f0' } },
      },
      yAxis: {
        type: 'value',
        min: 0,
        max: 100,
        axisLabel: { color: '#94a3b8', fontSize: 10 },
        splitLine: { lineStyle: { color: '#e2e8f0' } },
      },
      tooltip: {
        trigger: 'axis',
        formatter: (params: Array<{ dataIndex: number }>) => {
          const idx = params?.[0]?.dataIndex ?? 0;
          return `${series.dates[idx]}<br/>分数 ${series.scores[idx] ?? '—'}（${
            series.grades[idx] || '—'
          }）`;
        },
      },
      series: [
        {
          type: 'line',
          data: series.scores,
          connectNulls: true,
          symbolSize: 5,
          itemStyle: { color },
          lineStyle: { width: 2, color },
          areaStyle: { opacity: 0.08, color },
        },
      ],
    };
  }, [series, color]);

  if (!option) {
    return <p className="text-xs text-slate-400">历史快照不足 2 期，暂不画曲线（不拿单点连线充数）</p>;
  }
  return (
    <div className="h-[150px] min-w-0">
      <EChartsChart option={option} />
    </div>
  );
};

/** 维度明细行（权重 / 分数 / 红线 / 缺省原因，缺省不隐藏） */
const DimensionRows: React.FC<{ row: EvalScoreRow; color: string }> = ({ row, color }) => {
  const views = useMemo(() => orderedDimensionViews(row), [row]);
  return (
    <div>
      {views.map((view) => {
        const pct = view.score === null ? 0 : Math.max(0, Math.min(100, view.score));
        return (
          <div key={view.key} className="border-b border-slate-100 py-2 last:border-0">
            <div className="flex items-center justify-between gap-3">
              <div className="flex min-w-0 flex-wrap items-center gap-1.5">
                <span className="text-xs font-medium text-slate-700">{view.label}</span>
                <span className="rounded-full bg-slate-100 px-1.5 py-0.5 text-[10px] text-slate-500">
                  权重 {view.weight}
                </span>
                {view.redLine && (
                  <span className="rounded-full border border-amber-200 bg-amber-50 px-1.5 py-0.5 text-[10px] text-amber-700">
                    ⚠ 红线
                  </span>
                )}
              </div>
              <div className="flex shrink-0 items-center gap-2">
                <div className="h-1.5 w-24 overflow-hidden rounded-full bg-slate-100">
                  {view.score !== null && (
                    <div
                      className={`h-full rounded-full ${view.redLine ? RED_LINE_BAR : ''}`}
                      style={
                        view.redLine
                          ? { width: `${pct}%` }
                          : { width: `${pct}%`, background: color }
                      }
                    />
                  )}
                </div>
                <span
                  className={`w-10 text-right text-xs tabular-nums ${
                    view.score === null ? 'text-slate-400' : 'font-bold text-slate-800'
                  }`}
                >
                  {view.score === null ? '缺省' : view.score}
                </span>
              </div>
            </div>
            {view.note && <div className="mt-1 text-[10px] text-slate-400">{view.note}</div>}
          </div>
        );
      })}
    </div>
  );
};

export const EvalDetail: React.FC<EvalDetailProps> = ({ row, objectType, history, isSimple }) => {
  const labels = row ? rowLabels(row) : null;
  const meta = row ? gradeMeta(row.grade, row.low_confidence) : null;
  const color = gradeColor(row?.grade);
  const footnote = useMemo(() => evidenceFootnote(row), [row]);

  if (!row || !labels || !meta) {
    return (
      <div className="rounded-2xl border border-slate-200/80 bg-slate-50 p-8 text-center text-sm text-slate-500">
        从上方榜单选一个对象查看评分详情
      </div>
    );
  }

  const score = typeof row.score === 'number' ? row.score : null;

  return (
    <div className="space-y-3">
      <section className={CARD} style={{ borderLeft: `4px solid ${color}` }}>
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="truncate text-base font-bold text-slate-800">{labels.primary}</div>
            {labels.secondary && (
              <div className="truncate font-mono text-[10px] text-slate-400">{labels.secondary}</div>
            )}
            <div className="mt-1 text-[10px] text-slate-400">
              {row.snapshot_date || '—'} · {objectType}
            </div>
          </div>
          <div className="shrink-0 text-right">
            <span className={`inline-flex rounded-full border px-2.5 py-1 text-xs font-bold ${meta.className}`}>
              {meta.label}
              {meta.isLowConfidence ? ' †' : ''}
            </span>
            <div className="mt-1 text-3xl font-bold leading-none tabular-nums" style={{ color }}>
              {score === null ? '—' : score.toFixed(1)}
              <span className="ml-1 text-xs font-medium text-slate-400">分</span>
            </div>
          </div>
        </div>

        <div className="mt-3">
          <InsightChip row={row} />
        </div>

        <div className="mt-3">
          <DimensionCoverageBar row={row} />
          <p className="mt-1 text-[10px] text-slate-400">
            有色=该维已算 · 斜纹虚线=缺省（悬停看原因；缺省维不计入加权，权重已归一）
          </p>
        </div>

        {row.red_line_failed?.length > 0 && (
          <p className="mt-2 text-xs text-amber-700">
            ⚠ 触发红线：{row.red_line_failed.join('、')}（分数被压到 59 以下）
          </p>
        )}

        {isSimple && (
          <p className="mt-3 text-xs text-slate-400">
            简单模式只给结论与实测量。切到「专业」看分维得分、成本对比、长序列图与历史曲线。
          </p>
        )}
      </section>

      {!isSimple && (
        <>
          <section className={CARD}>
            <CardHeader icon={<Info className="h-4 w-4" />} title="分维得分" meta={
              <span className="text-[10px] text-slate-400">
                <TermTooltip term="score_grade">评级</TermTooltip>按加权合成
              </span>
            } />
            <DimensionRows row={row} color={color} />
          </section>

          <section className={CARD}>
            <CostCompareBars row={row} />
            <div className="mt-2 border-t border-slate-100 pt-2">
              <div className="mb-2 inline-flex items-center gap-1.5 text-[12px] font-semibold text-slate-700">
                <TrendingUp className="h-3.5 w-3.5 text-slate-400" />
                历史分数
              </div>
              <HistoryCurve history={history} color={color} />
            </div>
          </section>

          <EvalSeriesSection objectType={objectType} objectId={row.object_id} />
        </>
      )}

      <footer className="px-1 text-[10px] leading-4 text-slate-400">
        来源：{footnote.source} ｜ 口径：{footnote.caliber} ｜ {footnote.asOf} ｜ 缺省维：{footnote.missing}
      </footer>
    </div>
  );
};
