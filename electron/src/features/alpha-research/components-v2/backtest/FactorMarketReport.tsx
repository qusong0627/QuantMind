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
 */
import React, { useEffect, useMemo, useState } from 'react';
import { X, BarChart3, AlertCircle, Loader2, Table2, TrendingUp, Layers, Scale } from 'lucide-react';
import { EChartsChart } from '../../../../components/common/EChartsChart';
import { getRunSeries } from '../../services-v2/factorBacktestApi';
import type { DrillTarget, RunSeriesResult } from '../../types-v2/backtestCenter';
import { BACKTEST_STATUS_LABELS, matrixMetricSpec } from '../../types-v2/backtestCenter';
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

export interface FactorMarketReportProps {
  target: DrillTarget | null;
  onClose: () => void;
}

export const FactorMarketReport: React.FC<FactorMarketReportProps> = ({ target, onClose }) => {
  const [seriesResult, setSeriesResult] = useState<RunSeriesResult | null>(null);
  const [seriesError, setSeriesError] = useState<string | null>(null);
  const [seriesLoading, setSeriesLoading] = useState(false);
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

  // 目标切换：重置页签并按可用性拉序列
  useEffect(() => {
    setActiveTab('overview');
    setSeriesResult(null);
    setSeriesError(null);
    if (!target?.runId) return;
    if (target.status && target.status !== 'completed') return; // 降级：无序列可拉
    let alive = true;
    setSeriesLoading(true);
    (async () => {
      const resp = await getRunSeries(target.runId as string);
      if (!alive) return;
      if (resp.success && resp.data) {
        setSeriesResult(resp.data);
        setSeriesError(null);
      } else {
        setSeriesError(resp.error ?? '加载曲线失败');
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
          <button
            type="button"
            onClick={onClose}
            className="rounded-md p-1.5 text-muted-foreground hover:bg-muted/60"
            aria-label="关闭报告"
          >
            <X className="h-4 w-4" />
          </button>
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

          {!target.runId && !degraded && (
            <p className="py-3 text-sm text-muted-foreground">
              该格尚未回测——在派发台选中此因子并派发批量回测后，这里会出报告。
            </p>
          )}

          {/* 概览 */}
          {activeTab === 'overview' && (overviewMetrics.length > 0 || series) && (
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
