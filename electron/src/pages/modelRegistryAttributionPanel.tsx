import React, { useEffect, useMemo, useState } from 'react';
import { Button, Empty, Input, Pagination, Space, Table, Tag, Tooltip, Typography, message } from 'antd';
import {
  Activity,
  BarChart3,
  Brain,
  Download,
  Info,
  Layers,
  RefreshCw,
  Search,
} from 'lucide-react';
import { clsx } from 'clsx';
import dayjs from 'dayjs';
import {
  modelTrainingService,
  type ModelShapSummaryItem,
  type ModelShapSummaryResponse,
  type ModelTrainingRunStatus,
  type UserModelRecord,
} from '../services/modelTrainingService';
import { getMeta, getMetrics, modelDisplayName, resolveMetricNumber } from './modelRegistryUtils';

const { Text } = Typography;

const MARKET_LABELS: Record<string, string> = {
  CN: 'A股',
  HK: '港股',
  US: '美股',
  CRYPTO: '区块链',
  FUTURES: '期货',
};

const fmtNum = (value: unknown, digits = 3): string => {
  const num = Number(value);
  return value === null || value === undefined || !Number.isFinite(num) ? '—' : num.toFixed(digits);
};

const fmtPct = (value: unknown, digits = 1): string => {
  const num = Number(value);
  return value === null || value === undefined || !Number.isFinite(num)
    ? '—'
    : `${(num * 100).toFixed(digits)}%`;
};

const fmtInt = (value: unknown): string => {
  const num = Number(value);
  return value === null || value === undefined || !Number.isFinite(num) ? '—' : num.toLocaleString();
};

const SectionTitle: React.FC<{ icon: React.ReactNode; title: string; desc?: string }> = ({
  icon,
  title,
  desc,
}) => (
  <div className="flex items-center justify-between gap-3">
    <div className="flex items-center gap-2">
      <span className="text-indigo-500">{icon}</span>
      <span className="text-[13px] font-black text-slate-800">{title}</span>
    </div>
    {desc ? <span className="text-[10px] text-slate-400">{desc}</span> : null}
  </div>
);

const Stat: React.FC<{ label: string; value: React.ReactNode; hint?: string; tone?: string }> = ({
  label,
  value,
  hint,
  tone,
}) => (
  <div className="rounded-xl border border-slate-100 bg-slate-50/70 px-3 py-2">
    <div className="text-[9px] font-bold uppercase tracking-wider text-slate-400">{label}</div>
    <div className={clsx('mt-0.5 truncate text-[12px] font-black', tone || 'text-slate-800')}>
      {value}
    </div>
    {hint ? <div className="truncate text-[9px] text-slate-400">{hint}</div> : null}
  </div>
);

interface FactorSelectionRow {
  name: string;
  ic?: number | null;
  icir?: number | null;
  ic_positive_rate?: number | null;
  coverage?: number | null;
  n_days?: number | null;
  status?: string;
  reason?: string;
  pfs?: number | null;
}

export const AttributionAnalysisPanel: React.FC<{
  model: UserModelRecord;
  shapSummary: ModelShapSummaryResponse | null;
  loading: boolean;
  error?: string;
  featureLabelMap?: Record<string, string>;
  onRefresh: () => void;
}> = ({ model, shapSummary, loading, error, featureLabelMap = {}, onRefresh }) => {
  const meta = getMeta(model);
  const metrics = getMetrics(model);
  const shapMeta = meta.shap && typeof meta.shap === 'object' ? (meta.shap as Record<string, any>) : {};
  const rows = ((shapSummary?.items || shapMeta.items || []) as ModelShapSummaryItem[]).slice();
  const status = String(shapSummary?.status || shapMeta.status || 'missing').toLowerCase();
  const split = String(shapSummary?.split || shapMeta.split || '—');
  const rowsUsed = Number(shapSummary?.rows_used ?? shapMeta.rows_used ?? 0);

  const [runDetail, setRunDetail] = useState<ModelTrainingRunStatus | null>(null);
  const [runRequest, setRunRequest] = useState<Record<string, any> | null>(null);

  useEffect(() => {
    const runId = model.source_run_id;
    if (!runId) return;
    let alive = true;
    Promise.all([
      modelTrainingService.getTrainingRun(runId).catch(() => null),
      modelTrainingService.getTrainingRunRequest(runId).catch(() => null),
    ]).then(([detail, request]) => {
      if (!alive) return;
      setRunDetail(detail);
      setRunRequest((request?.request || null) as Record<string, any> | null);
    });
    return () => {
      alive = false;
    };
  }, [model.source_run_id]);

  const runMeta = (runDetail?.result?.metadata || {}) as Record<string, any>;
  const factorSelection = (runMeta.factor_selection || meta.factor_selection || null) as
    | Record<string, any>
    | null;
  const selectionFeatures: FactorSelectionRow[] = Array.isArray(factorSelection?.features)
    ? (factorSelection!.features as FactorSelectionRow[])
    : [];
  const selectedFactors = selectionFeatures.filter((f) => f.status === 'selected');
  const stageCounts = (factorSelection?.stage_counts || {}) as Record<string, any>;

  const modelType = String(meta.model_type || runRequest?.model_type || '—');
  const market = String(meta.market || runRequest?.context?.market || 'CN').toUpperCase();
  const horizon = Number(meta.target_horizon_days ?? runRequest?.target_horizon_days ?? 0) || null;
  const targetMode = String(meta.target_mode || runRequest?.target_mode || '');
  const labelFormula = String(meta.label_formula || runRequest?.label_formula || '');
  const trainingWindow = String(meta.training_window || runRequest?.training_window || '');
  const trainedCount = Number(meta.feature_count ?? runMeta.feature_count ?? 0) || null;
  const requestedCount =
    Number(
      meta.requested_feature_count_original ??
        meta.requested_features_original?.length ??
        runRequest?.features?.length ??
        trainedCount,
    ) || null;

  const trainRankIc = resolveMetricNumber(metrics, ['train_rank_ic', 'train_mean_ic', 'train_ic']);
  const valRankIc = resolveMetricNumber(metrics, ['val_rank_ic', 'val_mean_ic', 'valid_rank_ic']);
  const testRankIc = resolveMetricNumber(metrics, ['test_rank_ic', 'test_mean_ic', 'test_ic']);
  const testIcir = resolveMetricNumber(metrics, ['test_icir', 'test_rank_icir']);

  const hyperParams = useMemo<Array<[string, string]>>(() => {
    const out: Array<[string, string]> = [];
    ['lgb_params', 'xgb_params', 'catboost_params', 'dl_params', 'params'].forEach((key) => {
      const src = runRequest?.[key];
      if (!src || typeof src !== 'object') return;
      Object.entries(src as Record<string, unknown>).forEach(([k, v]) => {
        if (v === null || v === undefined || typeof v === 'object') return;
        out.push([k, String(v)]);
      });
    });
    ['num_boost_round', 'early_stopping_rounds', 'target_horizon_days', 'execution_price'].forEach(
      (key) => {
        const value = runRequest?.[key];
        if (value === null || value === undefined || typeof value === 'object') return;
        out.push([key, String(value)]);
      },
    );
    return out.slice(0, 14);
  }, [runRequest]);

  const shapRows = useMemo(
    () =>
      rows
        .slice()
        .sort((a, b) => Number(b.mean_abs_shap || 0) - Number(a.mean_abs_shap || 0)),
    [rows],
  );
  const totalAbs = shapRows.reduce((sum, row) => sum + Math.max(0, Number(row.mean_abs_shap || 0)), 0);
  const top1Share = totalAbs > 0 ? Number(shapRows[0]?.mean_abs_shap || 0) / totalAbs : null;
  const top10Share =
    totalAbs > 0
      ? shapRows.slice(0, 10).reduce((sum, row) => sum + Math.max(0, Number(row.mean_abs_shap || 0)), 0) /
        totalAbs
      : null;
  const positiveCount = shapRows.filter((row) => Number(row.mean_shap || 0) >= 0).length;
  const maxAbsShap = Math.max(Number(shapRows[0]?.mean_abs_shap || 0), 1e-12);

  const labelOf = (feature: string): string => featureLabelMap[feature] || feature;
  const topLabels = shapRows.slice(0, 3).map((row) => labelOf(row.feature));

  const funnel = [
    { label: '输入候选', value: Number(stageCounts.input) || requestedCount || 0, color: 'bg-slate-400' },
    { label: 'IC / ICIR 通过', value: Number(stageCounts.ic_pass) || 0, color: 'bg-indigo-400' },
    { label: '相关性 + 多样性', value: Number(stageCounts.corr_pass) || 0, color: 'bg-violet-400' },
    { label: '稳定性通过', value: Number(stageCounts.stable) || 0, color: 'bg-emerald-400' },
    ...(factorSelection?.thresholds?.pfs_threshold != null
      ? [{ label: 'PFS 扰动保真', value: Number(stageCounts.pfs_pass) || 0, color: 'bg-teal-400' }]
      : []),
    { label: '最终入选', value: Number(stageCounts.selected) || selectedFactors.length, color: 'bg-emerald-600' },
  ];
  const funnelMax = Math.max(1, ...funnel.map((step) => step.value));

  const handleExport = () => {
    if (rows.length === 0) {
      message.warning('暂无数据可导出');
      return;
    }
    try {
      const headers = ['因子名称', '原始代码', '平均绝对贡献(SHAP)', '平均贡献(方向)', '正向比'];
      const csvRows = rows.map((r) => [
        `"${labelOf(r.feature)}"`,
        `"${r.feature}"`,
        r.mean_abs_shap?.toFixed(8) || '0',
        r.mean_shap?.toFixed(8) || '0',
        ((r.positive_ratio || 0) * 100).toFixed(2) + '%',
      ]);
      const csvContent = [headers.join(','), ...csvRows.map((row) => row.join(','))].join('\n');
      const blob = new Blob(['﻿' + csvContent], { type: 'text/csv;charset=utf-8;' });
      const link = document.createElement('a');
      const url = URL.createObjectURL(blob);
      link.setAttribute('href', url);
      link.setAttribute('download', `attribution_${modelDisplayName(model)}_${dayjs().format('YYYYMMDD')}.csv`);
      link.style.visibility = 'hidden';
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      message.success('导出成功');
    } catch {
      message.error('导出失败');
    }
  };

  const [searchText, setSearchText] = useState('');
  const [currentPage, setCurrentPage] = useState(1);
  const pageSize = 12;
  const filteredRows = shapRows.filter(
    (row) =>
      row.feature.toLowerCase().includes(searchText.toLowerCase()) ||
      labelOf(row.feature).toLowerCase().includes(searchText.toLowerCase()),
  );

  const summaryLine = [
    modelType,
    MARKET_LABELS[market] || market,
    horizon ? `T+${horizon}` : null,
    trainedCount ? `${trainedCount} 因子` : null,
    testRankIc != null ? `测试 RankIC ${fmtNum(testRankIc)}` : null,
    testIcir != null ? `ICIR ${fmtNum(testIcir)}` : null,
  ]
    .filter(Boolean)
    .join(' · ');

  const profileConclusion = [
    `该模型是${modelType}的${horizon ? ` T+${horizon} ` : ''}收益预测模型`,
    trainingWindow ? `训练窗口 ${trainingWindow}` : null,
    requestedCount && trainedCount
      ? `从 ${requestedCount.toLocaleString()} 个候选因子中入模 ${trainedCount.toLocaleString()} 个`
      : trainedCount
        ? `入模 ${trainedCount.toLocaleString()} 个因子`
        : null,
    testRankIc != null ? `测试集 RankIC ${fmtNum(testRankIc)}（ICIR ${fmtNum(testIcir)}）` : null,
    topLabels.length ? `SHAP 贡献集中在 ${topLabels.join(' / ')}` : null,
    top10Share != null ? `前 10 个因子占 ${fmtPct(top10Share)}` : null,
  ]
    .filter(Boolean)
    .join('；');

  return (
    <div className="h-[calc(var(--app-h)-340px)] flex flex-col gap-3 overflow-hidden pt-2">
      <div className="glass-panel mx-1 flex shrink-0 items-center justify-between rounded-2xl border border-slate-200 bg-white px-5 py-3 shadow-sm">
        <div className="flex min-w-0 items-center gap-3">
          <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-gradient-to-br from-violet-500 to-indigo-600 text-white">
            <Brain size={16} />
          </span>
          <div className="min-w-0">
            <div className="text-sm font-black text-slate-800">模型画像与归因</div>
            <div className="truncate text-[11px] text-slate-400">{summaryLine || '等待模型元数据'}</div>
          </div>
        </div>
        <Space>
          <Tag
            bordered={false}
            color={status === 'completed' ? 'green' : status === 'missing' ? 'default' : 'blue'}
            className="!m-0 !rounded-full !px-2.5 !text-[10px] !font-bold"
          >
            SHAP {status === 'completed' ? '就绪' : status === 'missing' ? '未生成' : status}
          </Tag>
          <Button size="small" onClick={onRefresh} loading={loading} className="!rounded-lg !text-[11px]">
            刷新
          </Button>
          <Button
            size="small"
            icon={<Download size={13} />}
            onClick={handleExport}
            className="!rounded-lg !text-[11px]"
          >
            导出
          </Button>
        </Space>
      </div>

      <div className="flex-1 overflow-y-auto custom-scrollbar px-1 pb-2">
        <div className="space-y-3">
          {/* ── 模型概况 ── */}
          <section className="rounded-2xl border border-slate-200 bg-white p-4 shadow-sm">
            <SectionTitle
              icon={<Layers size={15} />}
              title="模型概况"
              desc="训练参数与数据口径（来自训练入库元数据）"
            />
            <div className="mt-3 grid grid-cols-2 gap-2 lg:grid-cols-4">
              <Stat label="模型类型" value={modelType} />
              <Stat label="市场 / 预测周期" value={`${MARKET_LABELS[market] || market} · ${horizon ? `T+${horizon}` : '—'}`} />
              <Stat
                label="入模特征"
                value={`${fmtInt(trainedCount)} / ${fmtInt(requestedCount)}`}
                hint="入模 / 候选"
              />
              <Stat label="训练窗口" value={trainingWindow || '—'} />
            </div>
            <div className="mt-2 grid grid-cols-2 gap-2 lg:grid-cols-4">
              <Stat label="训练 RankIC" value={fmtNum(trainRankIc)} />
              <Stat label="验证 RankIC" value={fmtNum(valRankIc)} />
              <Stat
                label="测试 RankIC"
                value={fmtNum(testRankIc)}
                tone={testRankIc == null ? undefined : testRankIc >= 0.05 ? 'text-emerald-600' : testRankIc >= 0 ? 'text-amber-600' : 'text-rose-500'}
              />
              <Stat label="测试 ICIR" value={fmtNum(testIcir)} />
            </div>
            <div className="mt-3 rounded-xl border border-slate-100 bg-slate-50/70 p-3 text-[11px] leading-relaxed text-slate-600">
              <span className="font-bold text-slate-700">模型结论：</span>
              {profileConclusion}
            </div>
            <div className="mt-2 rounded-xl border border-slate-100 px-3 py-2 text-[11px] text-slate-500">
              <span className="font-bold text-slate-600">标签口径：</span>
              {targetMode || '—'}
              {labelFormula ? ` · ${labelFormula}` : ''}
            </div>
            {hyperParams.length > 0 ? (
              <div className="mt-3 flex flex-wrap items-center gap-1.5">
                <span className="text-[9px] font-bold uppercase tracking-wider text-slate-400">超参数</span>
                {hyperParams.map(([key, value]) => (
                  <span
                    key={key}
                    className="rounded-md border border-indigo-100 bg-indigo-50/70 px-2 py-0.5 font-mono text-[10px] text-indigo-600"
                  >
                    {key}={value}
                  </span>
                ))}
              </div>
            ) : null}
          </section>

          {/* ── 因子筛选 ── */}
          <section className="rounded-2xl border border-slate-200 bg-white p-4 shadow-sm">
            <SectionTitle
              icon={<Activity size={15} />}
              title="因子筛选与入模清单"
              desc={
                factorSelection
                  ? `输入 ${fmtInt(funnel[0].value)} → 入选 ${fmtInt(funnel[funnel.length - 1].value)}`
                  : '训练入库元数据未保存筛选报告'
              }
            />
            {factorSelection ? (
              <>
                <div className="mt-3 flex h-2.5 overflow-hidden rounded-full bg-slate-100">
                  {funnel.map((step) => (
                    <Tooltip key={step.label} title={`${step.label}：${fmtInt(step.value)}`}>
                      <div
                        className={clsx('h-full transition-all', step.color)}
                        style={{ width: `${(step.value / funnelMax) * 100}%` }}
                      />
                    </Tooltip>
                  ))}
                </div>
                <div className="mt-2 grid grid-cols-3 gap-2 lg:grid-cols-6">
                  {funnel.map((step) => (
                    <Stat key={step.label} label={step.label} value={fmtInt(step.value)} />
                  ))}
                </div>
                {selectedFactors.length > 0 ? (
                  <div className="mt-3">
                    <Table<FactorSelectionRow>
                      size="small"
                      rowKey="name"
                      pagination={false}
                      dataSource={selectedFactors.slice(0, 12)}
                      columns={[
                        {
                          title: '入选因子',
                          dataIndex: 'name',
                          ellipsis: true,
                          render: (name: string) => (
                            <div className="min-w-0">
                              <div className="truncate text-[11px] font-bold text-slate-700">
                                {labelOf(name)}
                              </div>
                              <div className="truncate font-mono text-[9px] text-slate-400">{name}</div>
                            </div>
                          ),
                        },
                        {
                          title: 'IC',
                          dataIndex: 'ic',
                          width: 72,
                          align: 'right',
                          render: (v: number | null) => (
                            <span className="font-mono text-[10px] text-slate-600">{fmtNum(v, 4)}</span>
                          ),
                        },
                        {
                          title: 'ICIR',
                          dataIndex: 'icir',
                          width: 68,
                          align: 'right',
                          render: (v: number | null) => (
                            <span className="font-mono text-[10px] text-slate-600">{fmtNum(v, 2)}</span>
                          ),
                        },
                        {
                          title: 'IC 胜率',
                          dataIndex: 'ic_positive_rate',
                          width: 72,
                          align: 'right',
                          render: (v: number | null) => (
                            <span className="font-mono text-[10px] text-slate-500">{fmtPct(v)}</span>
                          ),
                        },
                        {
                          title: '覆盖',
                          dataIndex: 'coverage',
                          width: 68,
                          align: 'right',
                          render: (v: number | null) => (
                            <span className="font-mono text-[10px] text-slate-500">{fmtPct(v)}</span>
                          ),
                        },
                        {
                          title: 'PFS',
                          dataIndex: 'pfs',
                          width: 64,
                          align: 'right',
                          render: (v: number | null) => (
                            <span className="font-mono text-[10px] text-slate-500">{fmtNum(v, 2)}</span>
                          ),
                        },
                      ]}
                    />
                    {selectedFactors.length > 12 ? (
                      <div className="mt-1 text-[10px] text-slate-400">
                        仅展示前 12 个入选因子（共 {selectedFactors.length} 个），完整清单见训练结果页「因子筛选报告」。
                      </div>
                    ) : null}
                  </div>
                ) : null}
              </>
            ) : (
              <div className="mt-3 rounded-xl border border-dashed border-slate-200 bg-slate-50/60 px-3 py-3 text-[11px] text-slate-500">
                该模型训练时未开启因子筛选（或旧模型未保存筛选报告）
                {requestedCount ? `，入模使用请求的全部 ${requestedCount.toLocaleString()} 个特征` : ''}。
              </div>
            )}
          </section>

          {/* ── SHAP 归因 ── */}
          <section className="rounded-2xl border border-slate-200 bg-white p-4 shadow-sm">
            <SectionTitle
              icon={<BarChart3 size={15} />}
              title="SHAP 因子贡献"
              desc={`${split} 数据集 · ${fmtInt(rowsUsed)} 个样本`}
            />
            {rows.length === 0 ? (
              <div className="mt-3">
                {error ? (
                  <div className="rounded-xl border border-rose-100 bg-rose-50/70 px-3 py-3 text-[11px] text-rose-600">
                    {error}
                  </div>
                ) : (
                  <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="尚未生成 SHAP 归因（可在训练结果页或后台重新计算）" />
                )}
              </div>
            ) : (
              <>
                <div className="mt-3 grid grid-cols-2 gap-2 lg:grid-cols-4">
                  <Stat label="最强因子" value={topLabels[0] || '—'} hint={shapRows[0]?.feature} />
                  <Stat label="Top1 贡献占比" value={fmtPct(top1Share)} />
                  <Stat label="Top10 贡献占比" value={fmtPct(top10Share)} />
                  <Stat label="方向为正因子" value={`${positiveCount} / ${rows.length}`} />
                </div>

                <div className="mt-3 space-y-1.5">
                  {shapRows.slice(0, 12).map((row) => (
                    <div key={row.feature} className="flex items-center gap-2">
                      <span
                        className="w-36 shrink-0 truncate text-[11px] font-semibold text-slate-700"
                        title={`${labelOf(row.feature)}（${row.feature}）`}
                      >
                        {labelOf(row.feature)}
                      </span>
                      <div className="h-2 flex-1 overflow-hidden rounded-full bg-slate-100">
                        <div
                          className="h-full rounded-full bg-gradient-to-r from-violet-400 to-indigo-500"
                          style={{
                            width: `${(Number(row.mean_abs_shap || 0) / maxAbsShap) * 100}%`,
                          }}
                        />
                      </div>
                      <span className="w-[62px] shrink-0 text-right font-mono text-[10px] text-violet-600">
                        {fmtNum(row.mean_abs_shap, 6)}
                      </span>
                      <span
                        className={clsx(
                          'w-[58px] shrink-0 text-right font-mono text-[10px] font-bold',
                          Number(row.mean_shap || 0) >= 0 ? 'text-rose-500' : 'text-emerald-600',
                        )}
                      >
                        {Number(row.mean_shap || 0) >= 0 ? '+' : ''}
                        {fmtNum(row.mean_shap, 4)}
                      </span>
                    </div>
                  ))}
                </div>

                <div className="mt-3 flex items-center justify-between">
                  <span className="text-[10px] font-black uppercase tracking-widest text-slate-400">
                    完整因子贡献表
                  </span>
                  <Input
                    size="small"
                    allowClear
                    placeholder="按因子名搜索"
                    prefix={<Search size={12} className="text-slate-400" />}
                    className="h-7 w-52 rounded-lg text-[11px]"
                    value={searchText}
                    onChange={(event) => {
                      setSearchText(event.target.value);
                      setCurrentPage(1);
                    }}
                  />
                </div>
                <Table<ModelShapSummaryItem>
                  className="mt-2"
                  size="small"
                  rowKey="feature"
                  tableLayout="fixed"
                  dataSource={filteredRows.slice((currentPage - 1) * pageSize, currentPage * pageSize)}
                  pagination={false}
                  columns={[
                    {
                      title: '因子',
                      dataIndex: 'feature',
                      render: (feature: string) => (
                        <div className="min-w-0">
                          <div className="truncate text-[11px] font-bold text-slate-700">{labelOf(feature)}</div>
                          <div className="truncate font-mono text-[9px] text-slate-400">{feature}</div>
                        </div>
                      ),
                    },
                    {
                      title: '平均绝对贡献',
                      dataIndex: 'mean_abs_shap',
                      width: 190,
                      align: 'right',
                      sorter: (a: ModelShapSummaryItem, b: ModelShapSummaryItem) =>
                        Number(a.mean_abs_shap || 0) - Number(b.mean_abs_shap || 0),
                      render: (value: number) => (
                        <span className="font-mono text-[10px] font-bold text-violet-600">
                          {fmtNum(value, 6)}
                        </span>
                      ),
                    },
                    {
                      title: '方向',
                      dataIndex: 'mean_shap',
                      width: 90,
                      align: 'right',
                      sorter: (a: ModelShapSummaryItem, b: ModelShapSummaryItem) =>
                        Number(a.mean_shap || 0) - Number(b.mean_shap || 0),
                      render: (value: number) => (
                        <span
                          className={clsx(
                            'rounded px-1.5 py-0.5 font-mono text-[10px] font-bold',
                            Number(value || 0) >= 0 ? 'bg-rose-50 text-rose-600' : 'bg-emerald-50 text-emerald-600',
                          )}
                        >
                          {Number(value || 0) >= 0 ? '+' : ''}
                          {fmtNum(value, 6)}
                        </span>
                      ),
                    },
                    {
                      title: '正向比',
                      dataIndex: 'positive_ratio',
                      width: 76,
                      align: 'right',
                      render: (value: number) => (
                        <span className="font-mono text-[10px] text-slate-600">{fmtPct(value)}</span>
                      ),
                    },
                  ]}
                />
                <div className="mt-2 flex items-center justify-between">
                  <span className="flex items-center gap-1 text-[10px] text-slate-400">
                    <Info size={11} /> 正向=因子越大模型越看好；正向比=跨样本方向一致性
                  </span>
                  <Pagination
                    size="small"
                    current={currentPage}
                    pageSize={pageSize}
                    total={filteredRows.length}
                    showSizeChanger={false}
                    onChange={setCurrentPage}
                  />
                </div>
              </>
            )}
          </section>

          {loading ? (
            <div className="flex items-center justify-center py-6 text-[11px] text-slate-400">
              <RefreshCw size={13} className="mr-2 animate-spin" /> 正在刷新归因数据…
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
};

export default AttributionAnalysisPanel;
