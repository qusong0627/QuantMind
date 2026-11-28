/**
 * 模型对比表（Excel 式）：
 * - 表头 + 指标列冻结（指标列 sticky left: 0），纵向滚动不丢表头；
 * - 模型列可「冻结」，冻结列排在模型列最前并在横向滚动时固定，
 *   每列宽度固定，表格放不下就横向滚动，绝不把列挤到看不清；
 * - 每行最优值高亮「最佳」，列头汇总「领先 N 项」，一眼看出各模型优势。
 */
import React from 'react';
import { Tooltip } from 'antd';
import { clsx } from 'clsx';
import {
  ArrowDownWideNarrow,
  ArrowUpWideNarrow,
  Pin,
  Snowflake,
  Star,
  Trophy,
} from 'lucide-react';
import type { UserModelRecord } from '../../services/modelTrainingService';
import { extractModelTypeShort, getMeta, modelDisplayName } from '../modelRegistryUtils';
import {
  GROUP_META,
  GROUP_ORDER,
  MAX_FROZEN_COLUMNS,
  METRICS,
  METRIC_COL_WIDTH,
  MODEL_COL_WIDTH,
  evalStatusLabel,
  fmtMetric,
  gradeBadge,
  metricTone,
  metricValue,
  reportOf,
  type EvalEntry,
  type MetricKey,
} from './modelCompareMetrics';

export interface ModelCompareTableProps {
  /** 已排好展示顺序：冻结列在前，其余按当前排序指标排列。 */
  models: UserModelRecord[];
  /** 前 N 列为冻结列（sticky 固定在指标列右侧）。 */
  frozenCount: number;
  evals: Record<string, EvalEntry>;
  favorites: string[];
  pinned: string[];
  sortKey: MetricKey;
  sortDir: 'desc' | 'asc';
  bestByMetric: Map<MetricKey, number>;
  leadCounts: Map<string, number>;
  onSort: (key: MetricKey) => void;
  onToggleFavorite: (modelId: string) => void;
  onTogglePin: (modelId: string) => void;
  onToggleFreeze: (modelId: string) => void;
}

const FROZEN_EDGE_SHADOW = 'inset -10px 0 8px -8px rgba(15, 23, 42, 0.28)';

export const ModelCompareTable: React.FC<ModelCompareTableProps> = ({
  models,
  frozenCount,
  evals,
  favorites,
  pinned,
  sortKey,
  sortDir,
  bestByMetric,
  leadCounts,
  onSort,
  onToggleFavorite,
  onTogglePin,
  onToggleFreeze,
}) => {
  const freezeCapReached = frozenCount >= MAX_FROZEN_COLUMNS;

  return (
    <div
      className="model-compare-scroll min-h-0 flex-1 overflow-auto overscroll-contain outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-indigo-400/70"
      role="region"
      aria-label="模型对比数据表（可横向滚动；冻结列固定显示）"
      tabIndex={0}
      data-testid="model-compare-scroll"
    >
      <table
        className="border-separate border-spacing-0 text-[11px]"
        style={{ width: '100%', tableLayout: 'fixed' }}
      >
        <colgroup>
          <col style={{ width: METRIC_COL_WIDTH }} />
          {models.map((model) => (
            <col key={model.model_id} style={{ width: MODEL_COL_WIDTH }} />
          ))}
          <col />
        </colgroup>
        <thead>
          <tr>
            <th
              scope="col"
              className="sticky left-0 top-0 z-50 border-b border-slate-200 bg-slate-50 px-4 py-3 text-left text-[10px] font-black tracking-wider text-slate-500"
            >
              指标
            </th>
            {models.map((model, index) => {
              const meta = getMeta(model);
              const entry = evals[model.model_id];
              const grade = gradeBadge(entry);
              const report = reportOf(entry?.state ?? null);
              const type =
                extractModelTypeShort(model) || String(meta.model_type ?? '模型').toUpperCase();
              const horizon = Number(meta.target_horizon_days ?? meta.horizon_days ?? 0);
              const execPrice = report?.execution?.price;
              const id = model.model_id;
              const isFrozen = index < frozenCount;
              const isLastFrozen = isFrozen && index === frozenCount - 1;
              const lead = leadCounts.get(id) ?? 0;
              return (
                <th
                  key={id}
                  scope="col"
                  data-compare-col={id}
                  className={clsx(
                    'border-b border-l border-slate-200 bg-slate-50 px-3 py-2.5 text-left align-top',
                    'sticky top-0',
                    isFrozen ? 'z-40' : 'z-30',
                    isLastFrozen && 'border-r-2 border-r-indigo-300',
                  )}
                  style={
                    isFrozen
                      ? {
                          left: METRIC_COL_WIDTH + index * MODEL_COL_WIDTH,
                          boxShadow: isLastFrozen ? FROZEN_EDGE_SHADOW : undefined,
                        }
                      : undefined
                  }
                >
                  <div
                    className="line-clamp-2 text-[11px] font-black leading-tight text-slate-900"
                    title={modelDisplayName(model)}
                  >
                    {modelDisplayName(model)}
                  </div>
                  <div className="mt-1.5 flex items-center justify-between gap-1">
                    <div className="flex min-w-0 flex-wrap items-center gap-1 text-[9px] text-slate-500">
                      <span className="rounded bg-white px-1 py-0.5 font-mono ring-1 ring-slate-200">
                        {type}
                      </span>
                      {horizon > 0 && <span>T+{horizon}</span>}
                      <span>{String(meta.market || 'CN').toUpperCase()}</span>
                      {execPrice && (
                        <span
                          className="rounded bg-slate-100 px-1 py-0.5 text-slate-600"
                          title={`TopN / IC 口径跟随训练 execution_price：T+1 ${
                            execPrice === 'open' ? '开盘' : '收盘'
                          }`}
                        >
                          {execPrice === 'open' ? '开盘' : '收盘'}
                        </span>
                      )}
                    </div>
                    <div className="flex shrink-0 items-center gap-0.5">
                      <Tooltip
                        title={
                          isFrozen
                            ? '取消冻结（横向滚动不再固定）'
                            : freezeCapReached
                              ? `最多冻结 ${MAX_FROZEN_COLUMNS} 列，先取消一列`
                              : '冻结此列（横向滚动时固定在左侧）'
                        }
                      >
                        <button
                          type="button"
                          aria-label={`${isFrozen ? '取消冻结' : '冻结'} ${modelDisplayName(model)}`}
                          aria-pressed={isFrozen}
                          disabled={!isFrozen && freezeCapReached}
                          onClick={() => onToggleFreeze(id)}
                          className={clsx(
                            'flex h-5 w-5 items-center justify-center rounded-md transition-colors',
                            isFrozen
                              ? 'bg-sky-100 text-sky-600'
                              : freezeCapReached
                                ? 'cursor-not-allowed text-slate-200'
                                : 'text-slate-300 hover:bg-sky-50 hover:text-sky-500',
                          )}
                        >
                          <Snowflake size={11} />
                        </button>
                      </Tooltip>
                      <Tooltip title={pinned.includes(id) ? '取消置顶' : '置顶'}>
                        <button
                          type="button"
                          aria-label={pinned.includes(id) ? '取消置顶' : '置顶'}
                          onClick={() => onTogglePin(id)}
                          className={clsx(
                            'flex h-5 w-5 items-center justify-center rounded-md transition-colors',
                            pinned.includes(id)
                              ? 'bg-blue-100 text-blue-600'
                              : 'text-slate-300 hover:bg-blue-50 hover:text-blue-500',
                          )}
                        >
                          <Pin size={11} />
                        </button>
                      </Tooltip>
                      <Tooltip title={favorites.includes(id) ? '取消收藏' : '收藏'}>
                        <button
                          type="button"
                          aria-label={favorites.includes(id) ? '取消收藏' : '收藏'}
                          onClick={() => onToggleFavorite(id)}
                          className={clsx(
                            'flex h-5 w-5 items-center justify-center rounded-md transition-colors',
                            favorites.includes(id)
                              ? 'bg-amber-100 text-amber-500'
                              : 'text-slate-300 hover:bg-amber-50 hover:text-amber-500',
                          )}
                        >
                          <Star size={11} fill={favorites.includes(id) ? 'currentColor' : 'none'} />
                        </button>
                      </Tooltip>
                    </div>
                  </div>
                  <div className="mt-1.5 flex items-center gap-1">
                    <span
                      className={clsx(
                        'shrink-0 rounded-full px-1.5 py-0.5 text-[9px] font-bold',
                        grade.className,
                      )}
                      title={
                        report?.verdict.summary ||
                        (entry?.state && 'admission' in entry.state
                          ? entry.state.admission.reasons.join('；')
                          : undefined)
                      }
                    >
                      {grade.text}
                    </span>
                    <span
                      className={clsx(
                        'inline-flex shrink-0 items-center gap-1 rounded px-1 py-0.5 text-[9px] font-bold',
                        lead > 0
                          ? 'bg-amber-100/80 text-amber-700'
                          : 'bg-slate-100 text-slate-400',
                      )}
                      title={`在 ${METRICS.length} 项指标中领先（并列最优）的项数`}
                    >
                      <Trophy size={9} />
                      领先 {lead} 项
                    </span>
                    <span className="ml-auto truncate text-[9px] text-slate-400">
                      {report?.window?.end ? `至 ${report.window.end}` : evalStatusLabel(entry)}
                    </span>
                  </div>
                </th>
              );
            })}
            <th
              scope="col"
              aria-hidden="true"
              className="sticky top-0 z-20 border-b border-l border-slate-200 bg-slate-50"
            />
          </tr>
        </thead>
        <tbody>
          {GROUP_ORDER.map((group) => {
            const groupMeta = GROUP_META[group];
            const metrics = METRICS.filter((metric) => metric.group === group);
            return (
              <React.Fragment key={group}>
                <tr>
                  <td
                    title={groupMeta.hint}
                    className={clsx(
                      'sticky left-0 z-20 border-b border-t border-slate-200 px-4 py-1.5 text-[10px] font-black tracking-wider text-slate-600',
                      groupMeta.solidClassName,
                    )}
                  >
                    {groupMeta.label}
                  </td>
                  <td
                    aria-hidden="true"
                    colSpan={models.length + 1}
                    className={clsx(
                      'border-b border-t border-slate-200 bg-gradient-to-r',
                      groupMeta.className,
                    )}
                  />
                </tr>
                {metrics.map((metric) => {
                  const best = bestByMetric.get(metric.key);
                  const active = sortKey === metric.key;
                  return (
                    <tr
                      key={metric.key}
                      className={clsx(
                        'group/row transition-colors hover:bg-slate-50/80',
                        active && 'bg-indigo-50/40',
                      )}
                    >
                      <td
                        className={clsx(
                          'sticky left-0 z-30 border-b border-slate-100 bg-white px-2 py-1 text-[11px] font-bold text-slate-600 group-hover/row:bg-slate-50',
                          active && '!bg-indigo-50/60 text-indigo-700',
                        )}
                      >
                        <button
                          type="button"
                          onClick={() => onSort(metric.key)}
                          aria-pressed={active}
                          title={`${metric.hint ?? ''} 点击按此指标给模型列排序`}
                          className="flex w-full items-center gap-1.5 rounded px-2 py-1 text-left hover:bg-indigo-50/70 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-400"
                        >
                          <span className="min-w-0 flex-1">{metric.label}</span>
                          {active ? (
                            sortDir === 'desc' ? (
                              <ArrowDownWideNarrow size={11} className="shrink-0 text-indigo-500" />
                            ) : (
                              <ArrowUpWideNarrow size={11} className="shrink-0 text-indigo-500" />
                            )
                          ) : null}
                        </button>
                      </td>
                      {models.map((model, index) => {
                        const value = metricValue(model, evals[model.model_id], metric.key);
                        const isBest = value !== null && best !== undefined && value === best;
                        const isFrozen = index < frozenCount;
                        const isLastFrozen = isFrozen && index === frozenCount - 1;
                        return (
                          <td
                            key={model.model_id}
                            data-compare-cell={`${model.model_id}:${metric.key}`}
                            className={clsx(
                              'border-b border-l border-slate-100 px-3 py-1.5 text-right font-mono tabular-nums',
                              metricTone(metric.key, value),
                              isBest && 'bg-emerald-50/80 font-black',
                              isFrozen && 'sticky z-20',
                              isFrozen && !isBest && 'bg-white group-hover/row:bg-slate-50',
                              isLastFrozen && 'border-r-2 border-r-indigo-300',
                            )}
                            style={
                              isFrozen
                                ? {
                                    left: METRIC_COL_WIDTH + index * MODEL_COL_WIDTH,
                                    boxShadow: isLastFrozen ? FROZEN_EDGE_SHADOW : undefined,
                                  }
                                : undefined
                            }
                          >
                            <span className="inline-flex items-center gap-1">
                              {isBest && (
                                <span className="rounded bg-emerald-100 px-1 py-0.5 text-[8px] font-black text-emerald-700">
                                  最佳
                                </span>
                              )}
                              {fmtMetric(value, metric.kind)}
                            </span>
                          </td>
                        );
                      })}
                      <td aria-hidden="true" className="border-b border-l border-slate-100" />
                    </tr>
                  );
                })}
              </React.Fragment>
            );
          })}
        </tbody>
      </table>
    </div>
  );
};
