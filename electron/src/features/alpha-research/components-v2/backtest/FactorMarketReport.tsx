/**
 * 单因子 × 市场报告抽屉（T-FB-14）——矩阵格/台账行的下钻面。
 *
 * 页签按**可用性降级**（不画空图）：
 * - 降级终态（data_unsupported / insufficient / unavailable / failed / cancelled）
 *   只出「结论卡」（状态 + 原因 + 池/区间），图表页签禁用；
 * - 完成但无序列（老行未落盘序列）→ 概览标量照出，曲线页签给出诚实说明；
 * - 曲线全部来自 `/runs/{run_id}/series`（口径由后端 build_series_payload 单源，
 *   前端不重算净值）。
 *
 * 口径提示：
 * - 超额基准：series.bench='equal_weight' 时是**等权兜底**（该市场无基准指数），
 *   界面必须显著标注，不能冒充指数超额；
 * - CN 列是样本内（挖掘原始市场），非 CN 为样本外重算。
 *
 * 机构报告标量块（T-FB-16，`/report/{run_id}`）与曲线并行拉取（仅完成态）：
 * 概览页签增出「显著性 / 成本敏感性网格 / 多空腿头部 / 暂缺清单」；
 * 降级终态与序列缺失由报告端点自述（available=false + reason），照实陈列。
 */
import React, { useEffect, useMemo, useState } from 'react';
import { X, BarChart3, AlertCircle, FileDown, Loader2, Table2, TrendingUp, Layers, Scale } from 'lucide-react';
import { EChartsChart } from '../../../../components/common/EChartsChart';
import {
  downloadRunReportPdf,
  getRunReport,
  getRunSeries,
} from '../../services-v2/factorBacktestApi';
import type { DrillTarget, RunReport, RunSeriesResult } from '../../types-v2/backtestCenter';
import {
  BACKTEST_STATUS_LABELS,
  REPORT_BLOCK_LABELS,
  matrixMetricSpec,
} from '../../types-v2/backtestCenter';
import { cn, formatNumber, formatPercent } from '../../utils-v2';
import { ComparisonOverlay } from './ComparisonOverlay';

type TabId = 'overview' | 'ic' | 'group' | 'excess' | 'robust' | 'cross';

const TABS: { id: TabId; label: string; icon: React.ComponentType<{ className?: string }>; needsSeries: boolean }[] = [
  { id: 'overview', label: '概览', icon: BarChart3, needsSeries: false },
  { id: 'ic', label: 'IC', icon: TrendingUp, needsSeries: true },
  { id: 'group', label: '分组', icon: Layers, needsSeries: true },
  { id: 'excess', label: '超额', icon: Scale, needsSeries: true },
  { id: 'robust', label: '稳健性', icon: Table2, needsSeries: true },
  { id: 'cross', label: '跨市场对比', icon: BarChart3, needsSeries: false },
];

/** 概览标量卡（顺序即阅读序；缺键不渲染，值 null 显「—」） */
const OVERVIEW_METRICS: { key: string; label: string; fmt: (v: number) => string }[] = [
  { key: 'ic', label: 'IC', fmt: (v) => formatNumber(v, 4) },
  { key: 'rank_ic', label: 'Rank IC', fmt: (v) => formatNumber(v, 4) },
  { key: 'icir', label: 'ICIR', fmt: (v) => formatNumber(v, 3) },
  { key: 'rank_icir', label: 'Rank ICIR', fmt: (v) => formatNumber(v, 3) },
  { key: 'ic_nw_t', label: 'NW t 值', fmt: (v) => formatNumber(v, 2) },
  { key: 'sharpe_net', label: '扣费夏普', fmt: (v) => formatNumber(v, 2) },
  { key: 'ann_return_net', label: '扣费年化', fmt: (v) => formatPercent(v) },
  { key: 'max_drawdown', label: '最大回撤', fmt: (v) => formatPercent(v) },
  { key: 'ann_turnover', label: '年化换手', fmt: (v) => formatNumber(v, 1) },
  { key: 'ls_sharpe', label: '多空夏普', fmt: (v) => formatNumber(v, 2) },
  { key: 'ann_vol', label: '年化波动', fmt: (v) => formatPercent(v) },
  { key: 'n_days', label: '有效天数', fmt: (v) => formatNumber(v, 0) },
];

/** 通用折线图 option（time 轴 + dataZoom + 缺失断线） */
function lineOption(
  dates: string[],
  series: {
    name: string;
    values: (number | null)[];
    color: string;
    yAxisIndex?: number;
  }[],
  opts?: { yAxisFormatter?: string; showBars?: boolean; barValues?: (number | null)[] },
) {
  const lines = series.map((s) => ({
    name: s.name,
    type: (opts?.showBars && s.yAxisIndex === 1 ? 'bar' : 'line') as 'line' | 'bar',
    showSymbol: false,
    data: dates.map((d, i) => [d, s.values[i] ?? null]),
    lineStyle: { width: 2, color: s.color },
    itemStyle: { color: s.color },
    yAxisIndex: s.yAxisIndex ?? 0,
    connectNulls: false,
    ...(opts?.showBars && s.yAxisIndex === 1
      ? { barMaxWidth: 3, opacity: 0.35 }
      : {}),
  }));
  return {
    animation: false,
    grid: { top: 30, left: 56, right: opts?.showBars ? 56 : 16, bottom: 46 },
    legend: {
      top: 0,
      textStyle: { fontSize: 11, color: '#64748b' },
      itemWidth: 12,
      itemHeight: 8,
    },
    tooltip: { trigger: 'axis', textStyle: { fontSize: 11 } },
    xAxis: {
      type: 'time',
      axisLine: { lineStyle: { color: '#e2e8f0' } },
      axisLabel: { fontSize: 10, color: '#94a3b8' },
    },
    yAxis: opts?.showBars
      ? [
          {
            type: 'value',
            scale: true,
            axisLabel: { fontSize: 10, color: '#94a3b8' },
            splitLine: { lineStyle: { color: '#f1f5f9' } },
          },
          {
            type: 'value',
            scale: true,
            axisLabel: {
              fontSize: 10,
              color: '#cbd5e1',
              formatter: opts.yAxisFormatter,
            },
            splitLine: { show: false },
          },
        ]
      : {
          type: 'value',
          scale: true,
          axisLabel: {
            fontSize: 10,
            color: '#94a3b8',
            formatter: opts?.yAxisFormatter,
          },
          splitLine: { lineStyle: { color: '#f1f5f9' } },
        },
    dataZoom: [
      { type: 'inside' },
      { type: 'slider', height: 16, bottom: 6, borderColor: 'transparent' },
    ],
    series: lines,
  };
}

const DEGRADED_HINTS: Record<string, string> = {
  data_unsupported: '该因子的代码依赖本市场不存在的列（诚实降级，不做近似替代）。',
  insufficient: '有效交易日不足该市场最小窗口，统计量不可靠，未出指标。',
  unavailable: '该市场数据面缺失（provider 未就绪或日历为空）。',
  failed: '运行异常，原因见下。',
  cancelled: '运行被取消，未完成求值。',
};

/** n_trials 来源 → 中文标签（词表与后端 report.py 的 n_trials_source 对齐） */
const N_TRIALS_SOURCE_LABELS: Record<string, string> = {
  batch_completed_units: '批内完成单元数',
  param: '查询参数指定',
  default_single: '默认 1（单运行）',
};

const fmtNum = (v: number | null | undefined, digits: number): string =>
  typeof v === 'number' && Number.isFinite(v) ? formatNumber(v, digits) : '—';

const fmtPct = (v: number | null | undefined): string =>
  typeof v === 'number' && Number.isFinite(v) ? formatPercent(v) : '—';

/** 机构报告块迷你标量卡（值缺失一律「—」，绝不显示成 0） */
const StatCell: React.FC<{ label: string; value: string; hint?: string | null }> = ({
  label,
  value,
  hint,
}) => (
  <div className="rounded-lg border border-border/50 bg-muted/20 p-2.5">
    <div className="text-[10px] text-muted-foreground">{label}</div>
    <div className="mt-0.5 font-mono text-sm font-semibold text-slate-800">{value}</div>
    {hint && <div className="mt-0.5 text-[10px] text-muted-foreground">{hint}</div>}
  </div>
);

export interface FactorMarketReportProps {
  target: DrillTarget | null;
  onClose: () => void;
}

export const FactorMarketReport: React.FC<FactorMarketReportProps> = ({ target, onClose }) => {
  const [seriesResult, setSeriesResult] = useState<RunSeriesResult | null>(null);
  const [seriesError, setSeriesError] = useState<string | null>(null);
  const [seriesLoading, setSeriesLoading] = useState(false);
  const [report, setReport] = useState<RunReport | null>(null);
  const [reportError, setReportError] = useState<string | null>(null);
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<TabId>('overview');

  // Esc 关闭
  useEffect(() => {
    if (!target) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [target, onClose]);

  // 目标切换：重置页签并按可用性并行拉 序列 + 机构报告（仅完成态）
  useEffect(() => {
    setActiveTab('overview');
    setSeriesResult(null);
    setSeriesError(null);
    setReport(null);
    setReportError(null);
    setExportError(null);
    if (!target?.runId) return;
    if (target.status && target.status !== 'completed') return; // 降级：无序列可拉
    let alive = true;
    setSeriesLoading(true);
    (async () => {
      const [seriesResp, reportResp] = await Promise.all([
        getRunSeries(target.runId as string),
        getRunReport(target.runId as string),
      ]);
      if (!alive) return;
      if (seriesResp.success && seriesResp.data) {
        setSeriesResult(seriesResp.data);
        setSeriesError(null);
      } else {
        setSeriesError(seriesResp.error ?? '加载曲线失败');
      }
      if (reportResp.success && reportResp.data) {
        setReport(reportResp.data.report);
        setReportError(null);
      } else {
        setReportError(reportResp.error ?? '加载机构报告失败');
      }
      setSeriesLoading(false);
    })();
    return () => {
      alive = false;
    };
  }, [target]);

  const series = seriesResult?.series ?? null;
  const metrics = seriesResult?.run.metrics ?? target?.metrics ?? {};
  const degraded =
    !!target?.status && target.status !== 'completed' && target.status !== 'not_run';

  const tabEnabled = (tab: (typeof TABS)[number]): boolean => {
    if (!tab.needsSeries) return true;
    if (degraded) return false;
    if (tab.id === 'group') {
      return !!series && Object.keys(series.qCurves).length > 0;
    }
    return !!series;
  };

  // 当前页签被降级禁用时回退概览
  useEffect(() => {
    const tab = TABS.find((t) => t.id === activeTab);
    if (tab && !tabEnabled(tab)) setActiveTab('overview');
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [series, degraded, activeTab]);

  const overviewMetrics = useMemo(
    () =>
      OVERVIEW_METRICS.map((m) => ({ ...m, value: metrics[m.key] })).filter(
        (m) => m.key in metrics,
      ),
    [metrics],
  );

  // 报告块三件套（available=false 时全为 null，只出原因文本）
  const sig = report?.available ? (report.significance ?? null) : null;
  const headline = report?.available ? (report.headline ?? null) : null;
  const costGrid = report?.available ? (report.costGrid ?? null) : null;

  // 导出 PDF：后端渲染并落「报告档案 → 因子研究」（同 run 幂等覆盖），前端触发下载
  const handleExportPdf = async () => {
    if (!target?.runId || exporting) return;
    setExporting(true);
    setExportError(null);
    try {
      const resp = await downloadRunReportPdf(target.runId);
      if (resp.success && resp.data) {
        const url = URL.createObjectURL(resp.data.blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = resp.data.filename;
        document.body.appendChild(a);
        a.click();
        a.remove();
        // 延迟回收：部分浏览器在 click() 同步返回后仍需片刻才能发起下载
        window.setTimeout(() => URL.revokeObjectURL(url), 1000);
      } else {
        setExportError(resp.error ?? '未知错误');
      }
    } finally {
      setExporting(false);
    }
  };

  if (!target) return null;

  return (
    <div className="fixed inset-0 z-50" data-testid="factor-report">
      <div
        className="absolute inset-0 bg-slate-900/25 backdrop-blur-[2px]"
        onClick={onClose}
        aria-hidden
      />
      <aside className="absolute inset-y-0 right-0 flex w-full max-w-[900px] flex-col border-l border-border bg-white/95 shadow-2xl backdrop-blur-xl">
        {/* 头部 */}
        <header className="flex items-start justify-between gap-3 border-b border-border/60 px-5 py-3">
          <div className="min-w-0">
            <h3 className="m-0 truncate text-sm font-black text-slate-800">
              {target.factorName ?? target.factorId}
            </h3>
            <div className="mt-1 flex flex-wrap items-center gap-2 text-[11px] text-muted-foreground">
              <span className="rounded-md bg-indigo-50 px-1.5 py-0.5 font-medium text-indigo-600">
                {target.marketLabel ?? target.market}
              </span>
              {target.status && (
                <span
                  className={cn(
                    'rounded-md border px-1.5 py-0.5 font-medium',
                    target.status === 'completed'
                      ? 'border-emerald-200 bg-emerald-50 text-emerald-600'
                      : target.status === 'running'
                        ? 'border-indigo-200 bg-indigo-50 text-indigo-600'
                        : 'border-slate-200 bg-slate-50 text-slate-500',
                  )}
                >
                  {BACKTEST_STATUS_LABELS[target.status] ?? target.status}
                </span>
              )}
              {target.dateRange && <span>区间 {target.dateRange}</span>}
              {target.universe && <span>池 {target.universe}</span>}
              {target.runId && <span className="font-mono">{target.runId}</span>}
            </div>
          </div>
          <div className="flex shrink-0 items-center gap-1.5">
            {report?.available && (
              <button
                type="button"
                onClick={handleExportPdf}
                disabled={exporting}
                data-testid="export-report-pdf"
                title="导出机构报告 PDF（同时留档到「报告档案 → 因子研究」）"
                className="inline-flex items-center gap-1.5 rounded-md border border-border/60 px-2.5 py-1.5 text-xs font-medium text-slate-600 transition-colors hover:bg-muted/60 disabled:cursor-not-allowed disabled:opacity-60"
              >
                {exporting ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                ) : (
                  <FileDown className="h-3.5 w-3.5" />
                )}
                {exporting ? '导出中…' : '导出 PDF'}
              </button>
            )}
            <button
              type="button"
              onClick={onClose}
              className="rounded-md p-1.5 text-muted-foreground hover:bg-muted/60"
              aria-label="关闭报告"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
        </header>

        {/* 页签 */}
        <nav className="flex flex-wrap gap-1 border-b border-border/60 px-4 pt-2">
          {TABS.map((tab) => {
            const enabled = tabEnabled(tab);
            const Icon = tab.icon;
            return (
              <button
                key={tab.id}
                type="button"
                disabled={!enabled}
                onClick={() => setActiveTab(tab.id)}
                title={enabled ? '' : '该运行没有曲线数据'}
                className={cn(
                  'inline-flex items-center gap-1.5 rounded-t-lg border-b-2 px-3 py-1.5 text-xs font-medium transition-colors',
                  activeTab === tab.id
                    ? 'border-primary text-primary'
                    : enabled
                      ? 'border-transparent text-muted-foreground hover:text-foreground'
                      : 'cursor-not-allowed border-transparent text-slate-300',
                )}
              >
                <Icon className="h-3.5 w-3.5" />
                {tab.label}
              </button>
            );
          })}
        </nav>

        {/* 内容 */}
        <div className="flex-1 overflow-y-auto px-5 py-4">
          {degraded && (
            <div className="mb-4 rounded-lg border border-slate-200 bg-slate-50 p-3 text-xs">
              <p className="font-medium text-slate-700">
                该运行是降级终态（{BACKTEST_STATUS_LABELS[target.status ?? ''] ?? target.status}），没有可展示的曲线。
              </p>
              {target.status && DEGRADED_HINTS[target.status] && (
                <p className="mt-1 text-muted-foreground">{DEGRADED_HINTS[target.status]}</p>
              )}
              {target.error && (
                <p className="mt-1 break-all text-rose-500">{target.error}</p>
              )}
            </div>
          )}

          {!degraded && target.runId && seriesLoading && (
            <p className="flex items-center gap-2 py-3 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" /> 加载曲线…
            </p>
          )}

          {!degraded && target.runId && !seriesLoading && seriesError && (
            <p className="flex items-center gap-2 py-3 text-sm text-amber-600">
              <AlertCircle className="h-4 w-4" /> {seriesError}（标量指标仍可看，曲线页签已禁用）
            </p>
          )}

          {!degraded && target.runId && !seriesLoading && reportError && (
            <p className="flex items-center gap-2 py-3 text-sm text-amber-600">
              <AlertCircle className="h-4 w-4" /> 机构报告块加载失败：{reportError}
              （曲线与台账指标不受影响）
            </p>
          )}

          {exportError && (
            <p
              className="flex items-center gap-2 py-3 text-sm text-amber-600"
              data-testid="export-pdf-error"
            >
              <AlertCircle className="h-4 w-4" /> 导出报告 PDF 失败：{exportError}
            </p>
          )}

          {!target.runId && !degraded && (
            <p className="py-3 text-sm text-muted-foreground">
              该格尚未回测——在派发台选中此因子并派发批量回测后，这里会出报告。
            </p>
          )}

          {/* 概览 */}
          {activeTab === 'overview' &&
            (overviewMetrics.length > 0 || series || report) && (
            <div className="space-y-5">
              {overviewMetrics.length > 0 && (
                <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-4">
                  {overviewMetrics.map((m) => (
                    <div key={m.key} className="rounded-lg border border-border/50 bg-muted/20 p-2.5">
                      <div className="text-[10px] text-muted-foreground">{m.label}</div>
                      <div className="mt-0.5 font-mono text-sm font-semibold text-slate-800">
                        {typeof m.value === 'number' && Number.isFinite(m.value)
                          ? m.fmt(m.value)
                          : '—'}
                      </div>
                    </div>
                  ))}
                </div>
              )}
              {/* 报告块不可用（非完成终态 / 序列缺失）——照实说明，不出数字 */}
              {report && !report.available && (
                <p className="text-[11px] text-muted-foreground" data-testid="report-unavailable-note">
                  机构报告块不可用：{report.reason ?? '原因缺失'}
                  {report.note ? `（${report.note}）` : ''}
                </p>
              )}

              {/* 多空腿头部（BRAIN 口径） */}
              {headline && (
                <div data-testid="report-headline">
                  <h4 className="mb-2 text-xs font-medium text-muted-foreground">
                    多空腿头部（BRAIN 口径；Returns 为简单年化 μ×252，非 CAGR）
                  </h4>
                  <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
                    <StatCell label="Returns（毛年化）" value={fmtPct(headline.returns)} />
                    <StatCell label="IR" value={fmtNum(headline.ir, 2)} />
                    <StatCell label="日均换手（双边）" value={fmtNum(headline.turnover, 2)} />
                    <StatCell label="Fitness" value={fmtNum(headline.fitness, 2)} />
                    <StatCell label="Margin（R/换手）" value={fmtNum(headline.margin, 2)} />
                    <StatCell label="累计收益" value={fmtPct(headline.cumReturn)} />
                  </div>
                </div>
              )}

              {/* 显著性 */}
              {sig && (
                <div data-testid="report-significance">
                  <h4 className="mb-2 text-xs font-medium text-muted-foreground">
                    显著性（Newey-West t → 正态双侧 p → BY 族校正 → DSR 去膨胀）
                  </h4>
                  <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                    <StatCell label="NW t 值" value={fmtNum(sig.nwT, 2)} />
                    <StatCell label="普通 t 值（对照）" value={fmtNum(sig.plainT, 2)} />
                    <StatCell label="p 值（双侧）" value={fmtNum(sig.pValue, 4)} />
                    <StatCell label="BY q 值（族校正后）" value={fmtNum(sig.qValueBhy, 4)} />
                    <StatCell label="DSR（去膨胀）" value={fmtNum(sig.dsr, 4)} />
                    <StatCell
                      label="试次数 n_trials"
                      value={String(sig.nTrials)}
                      hint={N_TRIALS_SOURCE_LABELS[sig.nTrialsSource] ?? sig.nTrialsSource}
                    />
                    <StatCell label="族大小" value={String(sig.familyN)} />
                    <StatCell
                      label="Bootstrap 置信区间"
                      value={
                        sig.bootstrap
                          ? `${fmtNum(sig.bootstrap.lo, 4)} ~ ${fmtNum(sig.bootstrap.hi, 4)}`
                          : '—'
                      }
                      hint={
                        sig.bootstrap
                          ? `均值 ${fmtNum(sig.bootstrap.point, 4)} · ${Math.round(
                              (sig.bootstrap.level ?? 0.95) * 100,
                            )}% · n=${sig.bootstrap.nBoot ?? '—'}`
                          : null
                      }
                    />
                  </div>
                  {sig.familyNote && (
                    <p className="mt-1.5 text-[11px] text-muted-foreground">{sig.familyNote}</p>
                  )}
                  {sig.dsrNote && (
                    <p className="mt-0.5 text-[11px] text-muted-foreground">{sig.dsrNote}</p>
                  )}
                  {sig.crowding && (
                    <p className="mt-0.5 text-[11px] text-muted-foreground">
                      拥挤度 {fmtNum(sig.crowding.score, 3)}
                      {sig.crowding.note ? `（${sig.crowding.note}）` : ''}
                    </p>
                  )}
                </div>
              )}

              {/* 成本敏感性网格 */}
              {costGrid && costGrid.rows.length > 0 && (
                <div data-testid="report-cost-grid">
                  <h4 className="mb-2 text-xs font-medium text-muted-foreground">
                    成本敏感性（多空腿净收益 = 毛收益 − 双边换手 × 费率；盈亏平衡{' '}
                    {fmtNum(costGrid.breakEvenBps, 1)}bp）
                  </h4>
                  <div className="overflow-x-auto rounded-lg border border-border/50">
                    <table className="w-full text-xs">
                      <thead>
                        <tr className="border-b border-border/40 bg-muted/30 text-[10px] text-muted-foreground">
                          <th className="px-2.5 py-1.5 text-left font-medium">费率（bp）</th>
                          <th className="px-2.5 py-1.5 text-right font-medium">净年化</th>
                          <th className="px-2.5 py-1.5 text-right font-medium">净 IR</th>
                          <th className="px-2.5 py-1.5 text-right font-medium">净 Fitness</th>
                        </tr>
                      </thead>
                      <tbody>
                        {costGrid.rows.map((r) => (
                          <tr
                            key={r.bps}
                            className={cn(
                              'border-b border-border/20 font-mono last:border-0',
                              costGrid.defaultBps != null &&
                                r.bps === costGrid.defaultBps &&
                                'bg-indigo-50/40',
                            )}
                          >
                            <td className="px-2.5 py-1">{r.bps}</td>
                            <td className="px-2.5 py-1 text-right">{fmtPct(r.netReturn)}</td>
                            <td className="px-2.5 py-1 text-right">{fmtNum(r.netIr, 2)}</td>
                            <td className="px-2.5 py-1 text-right">{fmtNum(r.netFitness, 2)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                  {costGrid.defaultBps != null && (
                    <p className="mt-1 text-[10px] text-muted-foreground">
                      高亮行 = 该市场默认费率档（{costGrid.defaultBps}bp）。
                    </p>
                  )}
                  {costGrid.breakEvenNote && (
                    <p className="mt-1 text-[11px] text-amber-600">{costGrid.breakEvenNote}</p>
                  )}
                </div>
              )}

              {/* 暂缺报告块（数据面限制，不做近似替代） */}
              {report?.available && (report.unavailable?.length ?? 0) > 0 && (
                <div data-testid="report-unavailable">
                  <h4 className="mb-1.5 text-xs font-medium text-muted-foreground">
                    暂缺的报告块（当前数据面算不出，不做近似替代）
                  </h4>
                  <ul className="space-y-0.5 text-[11px] text-muted-foreground">
                    {(report.unavailable ?? []).map((b) => (
                      <li key={b.block}>
                        · {REPORT_BLOCK_LABELS[b.block] ?? b.block}：{b.reason}
                      </li>
                    ))}
                  </ul>
                </div>
              )}

              {series && (
                <div>
                  <h4 className="mb-2 text-xs font-medium text-muted-foreground">
                    净值曲线（多头腿 / 多空腿 / 基准，起点 1.0；缺失日不连线）
                  </h4>
                  <div className="h-[320px]">
                    <EChartsChart
                      style={{ height: '100%' }}
                      option={lineOption(series.dates, [
                        { name: '多头组合', values: series.navLong, color: '#2563eb' },
                        { name: '多空组合', values: series.navLs, color: '#dc2626' },
                        { name: '基准', values: series.navBench, color: '#94a3b8' },
                      ])}
                    />
                  </div>
                  {series.bench === 'equal_weight' && (
                    <p className="mt-1 text-[11px] text-amber-600">
                      基准 = 等权兜底（该市场暂无基准指数数据），不是指数超额。
                    </p>
                  )}
                </div>
              )}
            </div>
          )}

          {/* IC */}
          {activeTab === 'ic' && series && (
            <div className="space-y-4">
              <h4 className="text-xs font-medium text-muted-foreground">
                IC 累计曲线（求和口径——IC 不是收益率，不做复利）＋ 日 IC（浅色柱，右轴）
              </h4>
              <div className="h-[320px]">
                <EChartsChart
                  style={{ height: '100%' }}
                  option={lineOption(
                    series.dates,
                    [
                      { name: 'IC 累计', values: series.icCum, color: '#2563eb' },
                      { name: '日 IC', values: series.ic, color: '#94a3b8', yAxisIndex: 1 },
                    ],
                    { showBars: true, yAxisFormatter: '{value}' },
                  )}
                />
              </div>
            </div>
          )}

          {/* 分组 */}
          {activeTab === 'group' && series && (
            <div className="space-y-4">
              <h4 className="text-xs font-medium text-muted-foreground">
                分位桶净值（q1=最低分位 → 顶部=最高分位；单调性一眼可读）
              </h4>
              <div className="h-[360px]">
                <EChartsChart
                  style={{ height: '100%' }}
                  option={lineOption(
                    series.dates,
                    Object.keys(series.qCurves)
                      .sort((a, b) => {
                        const na = Number(a.replace(/\D/g, ''));
                        const nb = Number(b.replace(/\D/g, ''));
                        return na - nb;
                      })
                      .map((k, i, arr) => {
                        // 低分位冷色 → 高分位暖色（A股口径：红=头部）
                        const t = arr.length > 1 ? i / (arr.length - 1) : 0;
                        const r = Math.round(37 + (220 - 37) * t);
                        const g = Math.round(99 + (38 - 99) * t);
                        const b = Math.round(235 + (38 - 235) * t);
                        return {
                          name: k.toUpperCase(),
                          values: series.qCurves[k],
                          color: `rgb(${r},${g},${b})`,
                        };
                      }),
                  )}
                />
              </div>
            </div>
          )}

          {/* 超额 */}
          {activeTab === 'excess' && series && (
            <div className="space-y-4">
              <h4 className="text-xs font-medium text-muted-foreground">
                相对基准超额
                {series.bench === 'equal_weight'
                  ? '——基准为等权兜底（该市场暂无基准指数），非指数超额'
                  : `——基准 ${series.bench}`}
              </h4>
              <div className="h-[320px]">
                <EChartsChart
                  style={{ height: '100%' }}
                  option={lineOption(series.dates, [
                    { name: '多头组合', values: series.navLong, color: '#2563eb' },
                    { name: '基准', values: series.navBench, color: '#94a3b8' },
                    {
                      name: '超额（多头/基准）',
                      values: series.navLong.map((v, i) => {
                        const b = series.navBench[i];
                        return v != null && b != null && b !== 0 ? v / b : null;
                      }),
                      color: '#dc2626',
                    },
                  ])}
                />
              </div>
              {report?.available && report.excess && (
                <p className="text-[11px] text-amber-600">{report.excess.note}</p>
              )}
            </div>
          )}

          {/* 稳健性 */}
          {activeTab === 'robust' && series && (
            <div className="space-y-4">
              <h4 className="text-xs font-medium text-muted-foreground">
                换手（双边日均）与覆盖股票数——换手高时净收益打折看（见扣费口径指标）
              </h4>
              <div className="h-[320px]">
                <EChartsChart
                  style={{ height: '100%' }}
                  option={lineOption(
                    series.dates,
                    [
                      { name: '日均换手', values: series.turnover, color: '#d97706' },
                      {
                        name: '覆盖股票数',
                        values: series.coverage,
                        color: '#94a3b8',
                        yAxisIndex: 1,
                      },
                    ],
                    { showBars: true },
                  )}
                />
              </div>
              <p className="text-[11px] text-muted-foreground">
                换手口径：{series.meta.turnoverConvention || '—'} · 费率 {series.meta.costBps}bp ·
                分位桶数 {series.meta.nBuckets} · 头部比例 {(series.meta.topPct * 100).toFixed(0)}%
              </p>
            </div>
          )}

          {/* 跨市场 */}
          {activeTab === 'cross' && (
            <ComparisonOverlay factorId={target.factorId} currentMarket={target.market} />
          )}
        </div>
      </aside>
    </div>
  );
};

export default FactorMarketReport;
