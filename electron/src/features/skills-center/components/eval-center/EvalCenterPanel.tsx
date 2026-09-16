/**
 * 评估中心面板（FE-E / T-FE-14/15/16）
 *
 * 数据源：/api/v1/eval/*（eval_scores 表唯一读取面）。三类视图：
 * - 评分卡网格：五类卡（因子/模型/策略/账户/每日选股）最新快照，A/B/C/D 徽章 + 低置信 †；
 * - 评分详情：五维/多维雷达 + 历史分曲线 + 维度明细（缺省/红线如实展示）；
 * - 体检档案：strategy_health 对象 → 四分类结论 + **晋级门禁预演**（与执行点同源）+ 结论历史。
 */

import React, { useEffect, useMemo, useState } from 'react';
import ReactECharts from 'echarts-for-react';
import { AlertTriangle, Award, RefreshCw } from 'lucide-react';
import {
  getEvalObjectTypes,
  getScoreHistory,
  getStrategyHealth,
  listScores,
} from '../../services/evalCenterService';
import type { EvalObjectType, EvalScoreRow, StrategyHealthArchive } from '../../types/evalCenter';
import {
  coverageSummary,
  dimensionViews,
  gradeMeta,
  historySeries,
  radarEntries,
} from './evalCenterModel';
import { useUiMode } from '../../../shared/useUiMode';
import { SelfHealthUpload } from './SelfHealthUpload';
import { TermTooltip } from '../../../shared/TermTooltip';

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : '请求失败';
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
      <div className="bg-white rounded-2xl border border-gray-200 p-4">
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

  const radar = useMemo(() => radarEntries(selectedRow), [selectedRow]);
  const historyData = useMemo(() => historySeries(history), [history]);

  const radarOption = useMemo(() => {
    if (!radar) return null;
    return {
      radar: {
        indicator: radar.map((entry) => ({ name: entry.name, max: 100 })),
        radius: '62%',
        splitArea: { areaStyle: { color: ['#fafafa', '#ffffff'] } },
      },
      series: [
        {
          type: 'radar',
          data: [{ value: radar.map((e) => e.value), name: '维度得分' }],
          areaStyle: { opacity: 0.25 },
          lineStyle: { width: 2 },
        },
      ],
      tooltip: {},
    };
  }, [radar]);

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
          itemStyle: { color: '#dc2626' },
          areaStyle: { opacity: 0.08, color: '#dc2626' },
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
  }, [historyData]);

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

      <div className="flex flex-wrap gap-2">
        {objectTypes.map((otype) => (
          <button
            key={otype.object_type}
            type="button"
            onClick={() => setActiveType(otype.object_type)}
            className={`px-3 py-1.5 text-xs rounded-xl border transition-colors ${
              activeType === otype.object_type
                ? 'border-blue-500 bg-blue-50 text-blue-700'
                : 'border-gray-200 bg-white text-slate-600 hover:bg-gray-50'
            }`}
          >
            {otype.label}
          </button>
        ))}
      </div>

      {error && (
        <div className="bg-amber-50 border border-amber-200 rounded-2xl p-3 text-xs text-amber-800 flex items-start gap-2">
          <AlertTriangle className="w-4 h-4 mt-0.5" />
          {error}
        </div>
      )}

      {loading ? (
        <div className="flex items-center justify-center h-48">
          <RefreshCw className="w-6 h-6 text-blue-500 animate-spin" />
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
          <div className="bg-gray-50 rounded-2xl border border-gray-200 p-10 text-center text-sm text-gray-500">
            暂无该类型评分记录（评分任务在 EOD 跑批/回测体检后自动写入）
          </div>
        )
      ) : (
        <div className="grid grid-cols-1 lg:grid-cols-5 gap-4">
          <div className="lg:col-span-2 space-y-2 max-h-[560px] overflow-y-auto pr-1">
            {rows.map((row) => {
              const meta = gradeMeta(row.grade, row.low_confidence);
              return (
                <button
                  key={row.object_id}
                  type="button"
                  onClick={() => setSelectedId(row.object_id)}
                  className={`w-full text-left rounded-2xl border p-3 transition-colors ${
                    selectedId === row.object_id
                      ? 'border-blue-500 bg-blue-50/60'
                      : 'border-gray-200 bg-white hover:bg-gray-50'
                  }`}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-sm font-medium text-slate-800 truncate">
                      {row.object_id}
                    </span>
                    <span className={`text-xs px-2 py-0.5 rounded-full border ${meta.className}`}>
                      {row.grade || '—'}
                      {meta.isLowConfidence ? ' †' : ''}
                    </span>
                  </div>
                  <div className="flex items-center justify-between mt-1 text-xs text-slate-500">
                    <span>
                      {row.score === null || row.score === undefined
                        ? '未评分'
                        : `${Number(row.score).toFixed(1)} 分`}
                    </span>
                    <span>{coverageSummary(row)}</span>
                  </div>
                  {row.red_line_failed?.length > 0 && (
                    <div className="text-xs text-amber-700 mt-1">
                      ⚠ 红线：{row.red_line_failed.join('、')}
                    </div>
                  )}
                </button>
              );
            })}
          </div>

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
            ) : selectedRow ? (
              isSimple ? (
                <div className="bg-white rounded-2xl border border-gray-200 p-6 text-center">
                  <div className="text-sm font-semibold text-slate-800">{selectedRow.object_id}</div>
                  <div className="mt-2 flex items-center justify-center gap-3 text-sm">
                    <span className={`px-2 py-0.5 rounded-full border text-xs ${gradeMeta(selectedRow.grade, selectedRow.low_confidence).className}`}>
                      {selectedRow.grade || '—'}{gradeMeta(selectedRow.grade, selectedRow.low_confidence).isLowConfidence ? ' †' : ''}
                    </span>
                    <span className="text-slate-700">
                      {selectedRow.score === null ? '未评分' : `${Number(selectedRow.score).toFixed(1)} 分`}
                    </span>
                  </div>
                  <p className="text-xs text-slate-500 mt-2">{coverageSummary(selectedRow)}</p>
                  {selectedRow.red_line_failed?.length > 0 && (
                    <p className="text-xs text-amber-700 mt-1">⚠ 红线：{selectedRow.red_line_failed.join('、')}</p>
                  )}
                  <p className="text-xs text-slate-400 mt-3">
                    简单模式仅展示结论；右上角切换「专业」查看<TermTooltip term="score_grade">评级</TermTooltip>雷达、历史曲线与维度明细。
                  </p>
                </div>
              ) : (
              <>
                <div className="bg-white rounded-2xl border border-gray-200 p-4">
                  <div className="flex items-center gap-2 mb-2">
                    <Award className="w-4 h-4 text-blue-600" />
                    <span className="text-sm font-semibold text-slate-800">{selectedRow.object_id}</span>
                    <span className="text-xs text-slate-400">
                      {selectedRow.snapshot_date} ·{' '}
                      {selectedRow.score === null ? '未评分' : `${selectedRow.score} 分`}
                    </span>
                  </div>
                  {radarOption ? (
                    <ReactECharts option={radarOption} style={{ height: 260 }} notMerge />
                  ) : (
                    <p className="text-xs text-gray-400">有效维度不足 3 个，雷达图不可用（明细见下）</p>
                  )}
                </div>

                <div className="bg-white rounded-2xl border border-gray-200 p-4">
                  <h4 className="text-sm font-semibold text-slate-800 mb-2">历史分数</h4>
                  {historyOption ? (
                    <ReactECharts option={historyOption} style={{ height: 180 }} notMerge />
                  ) : (
                    <p className="text-xs text-gray-400">历史快照不足 2 期，暂不画曲线</p>
                  )}
                </div>

                <div className="bg-white rounded-2xl border border-gray-200 p-4">
                  <h4 className="text-sm font-semibold text-slate-800 mb-2">维度明细</h4>
                  <div className="space-y-1.5">
                    {dims.map((dim) => (
                      <div
                        key={dim.key}
                        className="flex items-start justify-between gap-3 text-xs border-b border-gray-100 pb-1.5 last:border-0"
                      >
                        <div>
                          <span className="text-slate-700 font-medium">{dim.label}</span>
                          <span className="text-slate-400 ml-2">权重 {dim.weight}</span>
                          {dim.redLine && <span className="text-amber-700 ml-2">⚠ 红线</span>}
                          {dim.note && <div className="text-slate-400 mt-0.5">{dim.note}</div>}
                        </div>
                        <span className="text-slate-800 shrink-0">
                          {dim.score === null ? '缺省' : dim.score}
                        </span>
                      </div>
                    ))}
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
      )}
    </div>
  );
};
