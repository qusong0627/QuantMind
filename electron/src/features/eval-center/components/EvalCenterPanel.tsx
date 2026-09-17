/**
 * 评估中心面板（FE-E / T-FE-14/15/16）
 *
 * 数据源：/api/v1/eval/*（eval_scores 表唯一读取面）。三类视图：
 * - 评分卡网格：五类卡（因子/模型/策略/账户/每日选股）最新快照，A/B/C/D 徽章 + 低置信 †；
 * - 评分详情：五维/多维雷达 + 历史分曲线 + 维度明细（缺省/红线如实展示）；
 * - 体检档案：strategy_health 对象 → 四分类结论 + **晋级门禁预演**（与执行点同源）+ 结论历史。
 *
 * 展示名：列表主标题优先 display_name（后端解析：因子词典/模型元数据/回测配置/账户用户名），
 * 原 object_id 降为副标题；无名字时按类型兜底（策略 → 「策略回测 <短id>…」）。
 */

import React, { useEffect, useMemo, useState } from 'react';
import ReactECharts from 'echarts-for-react';
import {
  AlertTriangle,
  Award,
  CalendarCheck,
  Cpu,
  HeartPulse,
  Inbox,
  RefreshCw,
  Sigma,
  Target,
  Wallet,
} from 'lucide-react';
import {
  getEvalObjectTypes,
  getScoreHistory,
  getStrategyHealth,
  listScores,
} from '../services/evalCenterService';
import type { EvalObjectType, EvalScoreRow, StrategyHealthArchive } from '../types/evalCenter';
import {
  averageScore,
  coverageSummary,
  dimensionViews,
  gradeColor,
  gradeCounts,
  gradeMeta,
  historySeries,
  radarEntries,
  rowLabels,
} from './evalCenterModel';
import { useUiMode } from '../../shared/useUiMode';
import { SelfHealthUpload } from './SelfHealthUpload';
import { TermTooltip } from '../../shared/TermTooltip';

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : '请求失败';
}

const TYPE_ICONS: Record<string, React.ComponentType<{ className?: string }>> = {
  factor: Sigma,
  model: Cpu,
  strategy: Target,
  account: Wallet,
  daily_selection: CalendarCheck,
  strategy_health: HeartPulse,
};

/** 评级字母（剥掉低置信标记；空 → —） */
function gradeLetter(grade: string | null | undefined): string {
  return String(grade || '').replace('†', '').trim() || '—';
}

const HealthArchive: React.FC<{ archive: StrategyHealthArchive }> = ({ archive }) => {
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
              <div className="text-xs opacity-80 mt-0.5">
                可信度 {Math.round(latest.confidence ?? 0)}/100
                {latest.evidence_source ? ` · 证据源 ${latest.evidence_source}` : ''}
                {latest.snapshot_date ? ` · ${latest.snapshot_date}` : ''}
              </div>
            )}
          </div>
          <span
            className={`text-xs px-2 py-1 rounded-full border ${
              archive.gate.allowed
                ? 'bg-white/70 border-current'
                : 'bg-white/70 border-current'
            }`}
          >
            晋级门禁：{archive.gate.allowed ? '放行' : '拦截'}
          </span>
        </div>
        <div className="text-xs mt-2 opacity-90">{archive.gate.note}</div>
        {latest && (latest.reasons?.length > 0 || latest.suggestions?.length > 0) && (
          <div className="text-xs mt-2 space-y-0.5">
            {latest.reasons?.map((r, i) => (
              <div key={`r-${i}`}>· {r}</div>
            ))}
            {latest.suggestions?.map((s, i) => (
              <div key={`s-${i}`} className="opacity-90">
                建议 {i + 1}: {s}
              </div>
            ))}
          </div>
        )}
      </div>
      <div className="bg-white rounded-2xl border border-gray-200 p-4 shadow-sm">
        <h4 className="text-sm font-semibold text-gray-800 mb-2">结论历史</h4>
        {archive.history.length === 0 ? (
          <p className="text-xs text-gray-400">暂无历史（体检在回测完成后自动生成；月度复检每月留档）</p>
        ) : (
          <div className="space-y-1.5">
            {archive.history.map((point, index) => {
              const pointMeta = gradeMeta(point.verdict, false);
              return (
                <div key={index} className="flex items-center gap-2 text-xs">
                  <span className="text-gray-500 w-24">{point.snapshot_date || '—'}</span>
                  <span className={`px-1.5 py-0.5 rounded border ${pointMeta.className}`}>
                    {point.verdict || '—'}
                  </span>
                  <span className="text-gray-600">可信度 {Math.round(point.confidence ?? 0)}</span>
                  {point.evidence_source && (
                    <span className="text-gray-400">（{point.evidence_source}）</span>
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

export const EvalCenterPanel: React.FC = () => {
  const { isSimple } = useUiMode();
  const [objectTypes, setObjectTypes] = useState<EvalObjectType[]>([]);
  const [activeType, setActiveType] = useState<string>('factor');
  const [rows, setRows] = useState<EvalScoreRow[]>([]);
  const [selectedId, setSelectedId] = useState<string>('');
  const [history, setHistory] = useState<EvalScoreRow[]>([]);
  const [archive, setArchive] = useState<StrategyHealthArchive | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    void (async () => {
      try {
        const resp = await getEvalObjectTypes();
        const types = resp?.data || [];
        setObjectTypes(types);
        if (types.length > 0 && !types.some((t) => t.object_type === activeType)) {
          setActiveType(types[0].object_type);
        }
      } catch (err: unknown) {
        setError(errorText(err));
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    void loadRows(activeType);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeType]);

  useEffect(() => {
    // 守卫：选中项必须属于当前类型（切页签瞬间旧 selectedId 未清，直查体检接口会 400）
    const row = rows.find((r) => r.object_id === selectedId);
    if (!selectedId || !row || row.object_type !== activeType) {
      setHistory([]);
      setArchive(null);
      return;
    }
    void loadDetail(activeType, selectedId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedId, activeType, rows]);

  const loadRows = async (type: string) => {
    setLoading(true);
    setError('');
    setSelectedId('');
    setArchive(null);
    setHistory([]);
    try {
      const resp = await listScores({ objectType: type, latestOnly: true, limit: 200 });
      const data = resp?.data || [];
      setRows(data);
      if (data.length > 0) setSelectedId(data[0].object_id);
    } catch (err: unknown) {
      setError(errorText(err));
      setRows([]);
    } finally {
      setLoading(false);
    }
  };

  const loadDetail = async (type: string, objectId: string) => {
    try {
      if (type === 'strategy_health') {
        if (!/^\d+$/.test(objectId)) {
          // 策略体检仅数字策略 id（后端契约）；非数字直接空态，不发请求
          setArchive(null);
          setHistory([]);
          return;
        }
        const resp = await getStrategyHealth(objectId);
        setArchive(resp?.data || null);
        setHistory([]);
      } else {
        const resp = await getScoreHistory(type, objectId);
        setHistory(resp?.data || []);
        setArchive(null);
      }
    } catch (err: unknown) {
      setError(errorText(err));
    }
  };

  const selectedRow = useMemo(
    () => rows.find((r) => r.object_id === selectedId) || null,
    [rows, selectedId]
  );

  const summary = useMemo(
    () => ({ avg: averageScore(rows), counts: gradeCounts(rows) }),
    [rows]
  );
  const selLabels = useMemo(() => (selectedRow ? rowLabels(selectedRow) : null), [selectedRow]);
  const selMeta = selectedRow ? gradeMeta(selectedRow.grade, selectedRow.low_confidence) : null;
  const selColor = gradeColor(selectedRow?.grade);
  const radar = useMemo(() => radarEntries(selectedRow), [selectedRow]);
  const historyData = useMemo(() => historySeries(history), [history]);

  const radarOption = useMemo(() => {
    if (!radar) return null;
    return {
      radar: {
        indicator: radar.map((entry) => ({ name: entry.name, max: 100 })),
        radius: '62%',
        axisName: { color: '#64748b', fontSize: 11 },
        axisLine: { lineStyle: { color: '#e2e8f0' } },
        splitLine: { lineStyle: { color: '#e2e8f0' } },
        splitArea: { areaStyle: { color: ['#f8fafc', '#ffffff'] } },
      },
      series: [
        {
          type: 'radar',
          symbolSize: 4,
          data: [
            {
              value: radar.map((e) => e.value),
              name: '维度得分',
              areaStyle: { opacity: 0.16, color: selColor },
              lineStyle: { width: 2, color: selColor },
              itemStyle: { color: selColor },
            },
          ],
        },
      ],
      tooltip: {},
    };
  }, [radar, selColor]);

  const historyOption = useMemo(() => {
    if (historyData.dates.length < 2) return null;
    return {
      grid: { left: 40, right: 16, top: 24, bottom: 28 },
      xAxis: { type: 'category', data: historyData.dates },
      yAxis: { type: 'value', min: 0, max: 100 },
      series: [
        {
          type: 'line',
          smooth: true,
          data: historyData.scores,
          connectNulls: true,
          symbolSize: 5,
          itemStyle: { color: selColor },
          lineStyle: { width: 2, color: selColor },
          areaStyle: { opacity: 0.08, color: selColor },
        },
      ],
      tooltip: {
        trigger: 'axis',
        formatter: (params: Array<{ dataIndex: number }>) => {
          const idx = params?.[0]?.dataIndex ?? 0;
          return `${historyData.dates[idx]}<br/>分数 ${historyData.scores[idx] ?? '—'}（${historyData.grades[idx] || '—'}）`;
        },
      },
    };
  }, [historyData, selColor]);

  const dims = useMemo(() => dimensionViews(selectedRow), [selectedRow]);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-xl font-bold text-slate-800">评估中心</h2>
          <p className="text-xs text-slate-500 mt-0.5">
            五类评分卡与体检档案（数据源 eval_scores；评分由每日 EOD 任务与回测体检自动生成）
          </p>
        </div>
        <button
          type="button"
          onClick={() => void loadRows(activeType)}
          disabled={loading}
          className="px-3 py-1.5 text-xs rounded-xl border border-gray-200 bg-white hover:bg-gray-100 text-gray-700 disabled:opacity-50"
        >
          <span className="inline-flex items-center gap-1">
            <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} />
            刷新
          </span>
        </button>
      </div>

      {/* 类型页签（图标 + 胶囊） */}
      <div className="flex flex-wrap items-center gap-1 rounded-full bg-slate-100 border border-slate-200 p-0.5 w-fit max-w-full">
        {objectTypes.map((otype) => {
          const Icon = TYPE_ICONS[otype.object_type] || Award;
          const active = activeType === otype.object_type;
          return (
            <button
              key={otype.object_type}
              type="button"
              onClick={() => setActiveType(otype.object_type)}
              className={`flex items-center gap-1.5 rounded-full px-3 py-1.5 text-[11px] font-bold transition-colors ${
                active ? 'bg-white text-slate-800 shadow-sm' : 'text-slate-500 hover:text-slate-700'
              }`}
            >
              <Icon className="w-3 h-3" />
              {otype.label}
            </button>
          );
        })}
      </div>

      {error && (
        <div className="bg-amber-50 border border-amber-200 rounded-2xl p-3 text-xs text-amber-800 flex items-start gap-2">
          <AlertTriangle className="w-4 h-4 mt-0.5" />
          {error}
        </div>
      )}

      {loading ? (
        <div className="flex flex-col items-center justify-center h-48 gap-3">
          <RefreshCw className="w-6 h-6 text-blue-500 animate-spin" />
          <span className="text-xs text-slate-400">正在读取评分卡…</span>
        </div>
      ) : rows.length === 0 ? (
        activeType === 'strategy_health' ? (
          <div className="space-y-4">
            <div className="bg-gray-50 rounded-2xl border border-gray-200 p-6 text-center text-sm text-gray-500">
              暂无体检留档（回测完成后自动生成；月度复检每月留档）——下方可直接自助体检：
            </div>
            <SelfHealthUpload />
          </div>
        ) : (
          <div className="bg-gray-50 rounded-2xl border border-gray-200 p-10 flex flex-col items-center gap-2 text-sm text-gray-500">
            <Inbox className="w-8 h-8 text-slate-300" />
            暂无该类型评分记录（评分任务在 EOD 跑批/回测体检后自动写入）
          </div>
        )
      ) : (
        <>
          {/* 概览条：数量 / 均分 / 评级分布 */}
          <div className="flex flex-wrap items-center gap-2">
            <span className="inline-flex items-center gap-1.5 rounded-full bg-slate-100 border border-slate-200 px-3 py-1 text-[11px] font-bold text-slate-500">
              <span className="h-1.5 w-1.5 rounded-full bg-indigo-500" />
              {rows.length} 个对象
            </span>
            {summary.avg !== null && (
              <span className="inline-flex items-center rounded-full bg-indigo-50 border border-indigo-100 px-2.5 py-1 text-[11px] font-bold text-indigo-600">
                {activeType === 'strategy_health' ? '平均可信度' : '平均分'} {summary.avg.toFixed(1)}
              </span>
            )}
            {summary.counts.map((g) => (
              <span
                key={g.grade}
                className={`inline-flex items-center rounded-full border px-2 py-0.5 text-[11px] font-bold ${
                  gradeMeta(g.grade === '?' ? null : g.grade).className
                }`}
              >
                {g.grade} × {g.count}
              </span>
            ))}
          </div>

          <div className="grid grid-cols-1 lg:grid-cols-5 gap-4">
            {/* 左：对象列表 */}
            <div className="lg:col-span-2 space-y-2 max-h-[600px] overflow-y-auto pr-1">
              {rows.map((row) => {
                const meta = gradeMeta(row.grade, row.low_confidence);
                const labels = rowLabels(row);
                const color = gradeColor(row.grade);
                const hasScore = typeof row.score === 'number';
                const pct = hasScore ? Math.max(0, Math.min(100, row.score as number)) : null;
                const active = selectedId === row.object_id;
                return (
                  <button
                    key={row.object_id}
                    type="button"
                    onClick={() => setSelectedId(row.object_id)}
                    className={`w-full text-left rounded-2xl border p-3 shadow-sm transition-all ${
                      active
                        ? 'border-indigo-400 bg-indigo-50/50 ring-1 ring-indigo-200'
                        : 'border-gray-200 bg-white hover:border-indigo-200 hover:shadow'
                    }`}
                  >
                    <div className="flex items-start justify-between gap-2">
                      <div className="min-w-0">
                        <div className="text-sm font-semibold text-slate-800 truncate" title={labels.primary}>
                          {labels.primary}
                        </div>
                        {labels.secondary && (
                          <div className="text-[10px] font-mono text-slate-400 truncate" title={labels.secondary}>
                            {labels.secondary}
                          </div>
                        )}
                      </div>
                      <span
                        className={`shrink-0 h-8 w-8 rounded-xl border flex items-center justify-center text-sm font-extrabold ${meta.className}`}
                      >
                        {gradeLetter(row.grade)}
                        {meta.isLowConfidence ? '†' : ''}
                      </span>
                    </div>
                    <div className="mt-2 flex items-center gap-2">
                      <div className="flex-1 h-1.5 rounded-full bg-slate-100 overflow-hidden">
                        {pct !== null && (
                          <div className="h-full rounded-full" style={{ width: `${pct}%`, background: color }} />
                        )}
                      </div>
                      <span className="text-xs font-bold tabular-nums shrink-0" style={{ color }}>
                        {hasScore ? (row.score as number).toFixed(1) : '未评分'}
                      </span>
                    </div>
                    <div className="mt-1.5 flex items-center justify-between text-[10px] text-slate-400">
                      <span>{coverageSummary(row)}</span>
                      <span>{row.snapshot_date || ''}</span>
                    </div>
                    {row.red_line_failed?.length > 0 && (
                      <div className="mt-1.5 inline-flex items-center rounded-full bg-amber-50 border border-amber-200 px-2 py-0.5 text-[10px] text-amber-800">
                        ⚠ 红线：{row.red_line_failed.join('、')}
                      </div>
                    )}
                  </button>
                );
              })}
            </div>

            {/* 右：详情 */}
            <div className="lg:col-span-3 space-y-4">
              {activeType === 'strategy_health' ? (
                <div className="space-y-4">
                  {archive ? (
                    <HealthArchive archive={archive} />
                  ) : (
                    <div className="bg-gray-50 rounded-2xl border border-gray-200 p-8 text-center text-sm text-gray-500">
                      选择左侧策略查看体检档案
                    </div>
                  )}
                  <SelfHealthUpload />
                </div>
              ) : selectedRow && selLabels && selMeta ? (
                isSimple ? (
                  <div className="bg-white rounded-2xl border border-gray-200 p-6 text-center shadow-sm">
                    <div className="text-base font-bold text-slate-800">{selLabels.primary}</div>
                    {selLabels.secondary && (
                      <div className="text-[10px] font-mono text-slate-400 mt-0.5">{selLabels.secondary}</div>
                    )}
                    <div className="mt-4 flex items-center justify-center gap-4">
                      <span
                        className={`h-12 w-12 rounded-2xl border flex items-center justify-center text-xl font-extrabold ${selMeta.className}`}
                      >
                        {gradeLetter(selectedRow.grade)}
                        {selMeta.isLowConfidence ? '†' : ''}
                      </span>
                      <span className="text-4xl font-bold tabular-nums" style={{ color: selColor }}>
                        {selectedRow.score === null ? '—' : Number(selectedRow.score).toFixed(1)}
                      </span>
                      <span className="text-xs text-slate-400">
                        {activeType === 'strategy_health' ? '可信度' : '分'}
                      </span>
                    </div>
                    <p className="text-xs text-slate-500 mt-3">{coverageSummary(selectedRow)}</p>
                    {selectedRow.red_line_failed?.length > 0 && (
                      <p className="text-xs text-amber-700 mt-1">⚠ 红线：{selectedRow.red_line_failed.join('、')}</p>
                    )}
                    <p className="text-xs text-slate-400 mt-3">
                      简单模式仅展示结论；右上角切换「专业」查看<TermTooltip term="score_grade">评级</TermTooltip>雷达、历史曲线与维度明细。
                    </p>
                  </div>
                ) : (
                  <>
                    <div
                      className="bg-white rounded-2xl border border-gray-200 p-4 shadow-sm"
                      style={{ borderLeft: `4px solid ${selColor}` }}
                    >
                      <div className="flex items-start justify-between gap-3">
                        <div className="min-w-0">
                          <div className="text-base font-bold text-slate-800 truncate">{selLabels.primary}</div>
                          {selLabels.secondary && (
                            <div className="text-[10px] font-mono text-slate-400 truncate">{selLabels.secondary}</div>
                          )}
                          <div className="text-[10px] text-slate-400 mt-1">
                            {selectedRow.snapshot_date} · {coverageSummary(selectedRow)}
                          </div>
                        </div>
                        <div className="text-right shrink-0">
                          <span
                            className={`inline-flex px-2.5 py-1 rounded-full border text-xs font-bold ${selMeta.className}`}
                          >
                            {selMeta.label}
                            {selMeta.isLowConfidence ? ' †' : ''}
                          </span>
                          <div className="mt-1 text-3xl font-bold tabular-nums leading-none" style={{ color: selColor }}>
                            {selectedRow.score === null ? '—' : Number(selectedRow.score).toFixed(1)}
                            <span className="text-xs font-medium text-slate-400 ml-1">
                              {activeType === 'strategy_health' ? '可信度' : '分'}
                            </span>
                          </div>
                        </div>
                      </div>
                      {radarOption ? (
                        <ReactECharts option={radarOption} style={{ height: 250, marginTop: 4 }} notMerge />
                      ) : (
                        <p className="text-xs text-gray-400 mt-3">有效维度不足 3 个，雷达图不可用（明细见下）</p>
                      )}
                    </div>

                    <div className="bg-white rounded-2xl border border-gray-200 p-4 shadow-sm">
                      <h4 className="text-sm font-semibold text-slate-800 mb-2">历史分数</h4>
                      {historyOption ? (
                        <ReactECharts option={historyOption} style={{ height: 180 }} notMerge />
                      ) : (
                        <p className="text-xs text-gray-400">历史快照不足 2 期，暂不画曲线</p>
                      )}
                    </div>

                    <div className="bg-white rounded-2xl border border-gray-200 p-4 shadow-sm">
                      <h4 className="text-sm font-semibold text-slate-800 mb-1">维度明细</h4>
                      <div>
                        {dims.map((dim) => {
                          const pct =
                            dim.score === null ? null : Math.max(0, Math.min(100, dim.score));
                          return (
                            <div key={dim.key} className="py-2 border-b border-gray-100 last:border-0">
                              <div className="flex items-center justify-between gap-3">
                                <div className="min-w-0 flex items-center gap-1.5 flex-wrap">
                                  <span className="text-xs font-medium text-slate-700">{dim.label}</span>
                                  <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-slate-100 text-slate-500">
                                    权重 {dim.weight}
                                  </span>
                                  {dim.redLine && (
                                    <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-amber-50 border border-amber-200 text-amber-700">
                                      ⚠ 红线
                                    </span>
                                  )}
                                </div>
                                <div className="shrink-0 flex items-center gap-2">
                                  <div className="w-24 h-1.5 rounded-full bg-slate-100 overflow-hidden">
                                    {pct !== null && (
                                      <div className="h-full rounded-full bg-indigo-500" style={{ width: `${pct}%` }} />
                                    )}
                                  </div>
                                  <span
                                    className={`w-10 text-right text-xs tabular-nums ${
                                      dim.score === null ? 'text-slate-400' : 'font-bold text-slate-800'
                                    }`}
                                  >
                                    {dim.score === null ? '缺省' : dim.score}
                                  </span>
                                </div>
                              </div>
                              {dim.note && <div className="text-[10px] text-slate-400 mt-1">{dim.note}</div>}
                            </div>
                          );
                        })}
                      </div>
                    </div>
                  </>
                )
              ) : (
                <div className="bg-gray-50 rounded-2xl border border-gray-200 p-8 text-center text-sm text-gray-500">
                  选择左侧对象查看评分详情
                </div>
              )}
            </div>
          </div>
        </>
      )}
    </div>
  );
};
