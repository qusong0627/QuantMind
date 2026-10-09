/**
 * 适配矩阵热力图（T-FB-13）——回测中心核心画面。
 *
 * 用户诉求：「我挖掘到那么多因子，都不清楚适合哪些市场」「排名、列表都要清晰」。
 * 本组件回答两个问题：
 *   1. 能不能跑 —— 灰格降级（数据不支持/样本不足/数据缺失）与静态兼容档
 *      （未跑过的格子给出 portable / unknown / data_unsupported）；
 *   2. 跑出来怎样 —— 已完成格按所选指标着色 + Best Market 徽标（**仅样本外**，
 *      CN 列是样本内基准，把它标成「最佳市场」会答错问题）。
 *
 * 口径纪律：
 * - 缺失一律「—」，绝不显示成 0（metrics 键缺席与 null 同待遇）；
 * - 着色只对完成格：有符号指标（IC/夏普族）正=红负=绿（A股口径，与 FactorTable
 *   的 metricTone 同语言）；风险类指标（回撤/换手）用中性琥珀按幅值加深，不借
 *   正负号语义；
 * - Best 徽标要求该因子在样本外列里 ≥2 个有效值才判（单值不称「最佳」，
 *   与 BacktestHistoryPanel 对比表的判据同一条）。
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Grid3X3,
  RefreshCw,
  AlertCircle,
  Download,
  Search,
  Crown,
  Loader2,
} from 'lucide-react';
import { Card, CardContent, CardHeader, CardTitle } from '../ui/Card';
import { Button } from '../ui/Button';
import { fetchMatrix } from '../../services-v2/factorBacktestApi';
import type {
  DrillTarget,
  MatrixCell,
  MatrixFactorRow,
  MatrixResult,
} from '../../types-v2/backtestCenter';
import {
  BACKTEST_STATUS_LABELS,
  MATRIX_METRIC_SPECS,
  MATRIX_SIGNIFICANCE_KEYS,
  matrixMetricSpec,
} from '../../types-v2/backtestCenter';
import type { MatrixSignificanceKey } from '../../types-v2/backtestCenter';
import { cn, formatNumber, formatPercent, formatShortTime } from '../../utils-v2';
import { buildCsvText, downloadCsvFile } from '../../../../utils/csvExport';

type OrderMode = 'factor' | 'market';
type StatusFilter = 'all' | 'has_completed' | 'has_failed' | 'has_degraded' | 'not_run_any';

const STATUS_FILTERS: { id: StatusFilter; label: string }[] = [
  { id: 'all', label: '全部' },
  { id: 'has_completed', label: '已有结果' },
  { id: 'has_failed', label: '有失败' },
  { id: 'has_degraded', label: '有降级' },
  { id: 'not_run_any', label: '尚未回测' },
];

const DEGRADED_STATUSES = ['data_unsupported', 'insufficient', 'unavailable'];

// ── 取值与格式化 ─────────────────────────────────────────────────────

/** 显著性三列（NW t / p / q）住在 cell.significance 段，不在 metrics 字典 */
function isSignificanceKey(key: string): key is MatrixSignificanceKey {
  return (MATRIX_SIGNIFICANCE_KEYS as readonly string[]).includes(key);
}

function metricValue(cell: MatrixCell, key: string): number | null {
  if (isSignificanceKey(key)) {
    const v = cell.significance?.[key] ?? null;
    return typeof v === 'number' && Number.isFinite(v) ? v : null;
  }
  const v = cell.metrics[key];
  return typeof v === 'number' && Number.isFinite(v) ? v : null;
}

function formatMetric(value: number, format: 'number' | 'percent', precision: number): string {
  return format === 'percent' ? formatPercent(value, precision) : formatNumber(value, precision);
}

/**
 * 有符号指标（正负语义）vs 幅值语义指标（无正负着色）。
 * p / q 是 [0,1] 概率（越小越显著），借正负色会把「q=0 最显著」画成浅色、
 * 「q≈1」画成深红——语义反了；按幅值琥珀加深（越弱越深，读作警示）。
 */
function metricIsSigned(key: string): boolean {
  return !['max_drawdown', 'ann_turnover', 'n_days', 'p_value', 'q_value_bhy'].includes(key);
}

/** 完成格的背景/文字色：有符号指标红正绿负，风险指标琥珀按幅值 */
function cellToneClass(value: number, key: string, maxAbs: number): string {
  const ratio = maxAbs > 0 ? Math.min(Math.abs(value) / maxAbs, 1) : 0;
  const bucket = ratio >= 0.66 ? 3 : ratio >= 0.33 ? 2 : ratio > 0 ? 1 : 0;
  if (bucket === 0) return 'bg-muted/30 text-muted-foreground';
  if (metricIsSigned(key)) {
    const palette =
      value > 0
        ? ['', 'bg-rose-500/10 text-rose-600', 'bg-rose-500/20 text-rose-700', 'bg-rose-500/30 text-rose-800']
        : ['', 'bg-emerald-500/10 text-emerald-600', 'bg-emerald-500/20 text-emerald-700', 'bg-emerald-500/30 text-emerald-800'];
    return palette[bucket];
  }
  const palette = ['', 'bg-amber-500/10 text-amber-600', 'bg-amber-500/20 text-amber-700', 'bg-amber-500/30 text-amber-800'];
  return palette[bucket];
}

/** 降级/失败状态 → 格子样式（灰格族） */
function degradedClass(status: string): string {
  switch (status) {
    case 'failed':
      return 'bg-rose-50 text-rose-600 border-rose-200';
    case 'cancelled':
      return 'bg-amber-50 text-amber-600 border-amber-200';
    case 'data_unsupported':
      return 'bg-slate-100 text-slate-400 border-slate-200';
    case 'insufficient':
      return 'bg-slate-100 text-slate-500 border-slate-200';
    case 'unavailable':
      return 'bg-slate-100 text-slate-500 border-slate-200';
    default:
      return 'bg-slate-50 text-slate-400 border-slate-200';
  }
}

function cellTooltip(row: MatrixFactorRow, marketLabel: string, cell: MatrixCell): string {
  const lines = [`${row.factorName ?? row.factorId} · ${marketLabel}`];
  lines.push(`状态：${BACKTEST_STATUS_LABELS[cell.status] ?? cell.status}`);
  if (cell.status === 'not_run') {
    const compatLabel =
      cell.compat === 'portable' ? '可移植' : cell.compat === 'data_unsupported' ? '列依赖不支持' : '待实跑裁决';
    lines.push(`静态兼容：${compatLabel}`);
    if (cell.missing.length > 0) lines.push(`缺失列：${cell.missing.join(', ')}`);
  }
  if (cell.universe) lines.push(`股票池：${cell.universe}`);
  if (cell.dateRange) lines.push(`区间：${cell.dateRange}`);
  if (cell.finishedAt) lines.push(`收口：${formatShortTime(cell.finishedAt)}`);
  if (cell.error) lines.push(`原因：${cell.error}`);
  if (cell.significance) {
    const s = cell.significance;
    const fmt = (v: number | null, digits: number) => (v == null ? '—' : formatNumber(v, digits));
    const family =
      s.family_n == null ? '' : s.family_n > 1 ? `（族 N=${s.family_n}，BY 校正）` : '（无族上下文，q=p）';
    lines.push(
      `显著性：NW t=${fmt(s.nw_t, 2)}  p=${fmt(s.p_value, 4)}  BY q=${fmt(s.q_value_bhy, 4)}${family}`,
    );
  }
  const metricKeys = Object.keys(cell.metrics).filter(
    // significance 段在场时跳过 ic_nw_t（同一数值，显著性行已展示，避免重复）
    (k) => cell.metrics[k] != null && !(cell.significance && k === 'ic_nw_t'),
  );
  if (metricKeys.length > 0) {
    lines.push(
      metricKeys
        .slice(0, 10)
        .map((k) => {
          const spec = matrixMetricSpec(k);
          const v = cell.metrics[k] as number;
          return `${spec.label}=${formatMetric(v, spec.format, spec.precision)}`;
        })
        .join('  '),
    );
  }
  return lines.join('\n');
}

function rowHas(cells: MatrixCell[], pick: (c: MatrixCell) => boolean): boolean {
  return cells.some(pick);
}

// ── 组件 ─────────────────────────────────────────────────────────────

export interface MatrixHeatmapProps {
  factorIds: string[];
  markets: string[] | null;
  /** 值变化触发重取（批次终态 / 手动刷新） */
  refreshToken: number;
  onOpenCell: (target: DrillTarget) => void;
}

export const MatrixHeatmap: React.FC<MatrixHeatmapProps> = ({
  factorIds,
  markets,
  refreshToken,
  onOpenCell,
}) => {
  const [result, setResult] = useState<MatrixResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [metricKey, setMetricKey] = useState('rank_ic');
  const [orderMode, setOrderMode] = useState<OrderMode>('factor');
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all');
  const [threshold, setThreshold] = useState('');
  const [search, setSearch] = useState('');
  /** 请求序号：连点刷新/换因子时丢弃过期响应 */
  const seqRef = useRef(0);

  const spec = matrixMetricSpec(metricKey);
  const marketsKey = (markets ?? []).join(',');
  const factorsKey = factorIds.join(',');

  const load = useCallback(async () => {
    if (factorIds.length === 0) {
      setResult(null);
      setError(null);
      return;
    }
    const seq = ++seqRef.current;
    setLoading(true);
    try {
      const resp = await fetchMatrix({ factorIds, markets });
      if (seq !== seqRef.current) return;
      if (resp.success && resp.data) {
        setResult(resp.data);
        setError(null);
      } else {
        setError(resp.error ?? '查询适配矩阵失败');
      }
    } finally {
      if (seq === seqRef.current) setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [factorsKey, marketsKey]);

  useEffect(() => {
    void load();
  }, [load, refreshToken]);

  const marketCols = result?.markets ?? [];

  /** 可见行：搜索 / 状态 / 阈值 过滤（阈值只对有方向指标生效） */
  const visibleRows = useMemo(() => {
    if (!result) return [];
    const thresholdNum = threshold.trim() === '' ? null : Number(threshold);
    let rows = result.factors.filter((row) => {
      if (search.trim()) {
        const s = search.trim().toLowerCase();
        const hay = `${row.factorName ?? ''} ${row.factorId}`.toLowerCase();
        if (!hay.includes(s)) return false;
      }
      const cells = marketCols.map((m) => row.cells[m.market]).filter(Boolean);
      switch (statusFilter) {
        case 'has_completed':
          if (!rowHas(cells, (c) => c.status === 'completed')) return false;
          break;
        case 'has_failed':
          if (!rowHas(cells, (c) => c.status === 'failed')) return false;
          break;
        case 'has_degraded':
          if (!rowHas(cells, (c) => DEGRADED_STATUSES.includes(c.status))) return false;
          break;
        case 'not_run_any':
          if (!cells.every((c) => c.status === 'not_run')) return false;
          break;
        default:
          break;
      }
      if (
        thresholdNum != null &&
        Number.isFinite(thresholdNum) &&
        spec.direction !== 'none'
      ) {
        const values = cells
          .map((c) => (c.status === 'completed' ? metricValue(c, metricKey) : null))
          .filter((v): v is number => v != null);
        if (values.length === 0) return false;
        const okThreshold =
          spec.direction === 'higher'
            ? values.some((v) => v >= thresholdNum)
            : values.some((v) => v <= thresholdNum);
        if (!okThreshold) return false;
      }
      return true;
    });
    if (orderMode === 'factor' && spec.direction !== 'none') {
      rows = [...rows].sort((a, b) => {
        const va = bestValueForRow(a, marketCols, metricKey, spec.direction);
        const vb = bestValueForRow(b, marketCols, metricKey, spec.direction);
        if (va == null && vb == null) return 0;
        if (va == null) return 1; // 缺失恒排最后（缺失不是「小」）
        if (vb == null) return -1;
        return spec.direction === 'higher' ? vb - va : va - vb;
      });
    }
    return rows;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [result, marketCols, search, statusFilter, threshold, orderMode, metricKey, spec.direction]);

  /** 着色归一化幅值（全表该指标 max|v|） */
  const maxAbs = useMemo(() => {
    let m = 0;
    for (const row of result?.factors ?? []) {
      for (const col of marketCols) {
        const cell = row.cells[col.market];
        if (!cell || cell.status !== 'completed') continue;
        const v = metricValue(cell, metricKey);
        if (v != null) m = Math.max(m, Math.abs(v));
      }
    }
    return m;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [result, marketCols, metricKey]);

  /** 市场排名模式：每列内排名（1=最好） */
  const columnRanks = useMemo(() => {
    const ranks: Record<string, Map<string, number>> = {};
    for (const col of marketCols) {
      const entries: { factorId: string; value: number }[] = [];
      for (const row of result?.factors ?? []) {
        const cell = row.cells[col.market];
        if (!cell || cell.status !== 'completed') continue;
        const v = metricValue(cell, metricKey);
        if (v != null) entries.push({ factorId: row.factorId, value: v });
      }
      entries.sort((a, b) =>
        spec.direction === 'lower' ? a.value - b.value : b.value - a.value,
      );
      const map = new Map<string, number>();
      entries.forEach((e, i) => map.set(e.factorId, i + 1));
      ranks[col.market] = map;
    }
    return ranks;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [result, marketCols, metricKey, spec.direction]);

  const handleExportCsv = () => {
    if (!result) return;
    const header = [
      '因子ID',
      '因子名',
      '样本内CN·IC',
      ...marketCols.flatMap((m) => [`${m.label}·${spec.label}`, `${m.label}·状态`]),
    ];
    const rows = visibleRows.map((row) => [
      row.factorId,
      row.factorName ?? '',
      row.cnIc == null ? '' : row.cnIc,
      ...marketCols.flatMap((m) => {
        const cell = row.cells[m.market];
        if (!cell) return ['', ''];
        const v = cell.status === 'completed' ? metricValue(cell, metricKey) : null;
        return [
          v == null ? '' : v,
          BACKTEST_STATUS_LABELS[cell.status] ?? cell.status,
        ];
      }),
    ]);
    // dataRange 不传：每格的区间各不相同（各市场日历不同），编一个统一区间
    // 会比没有更误导。免责段省略该行是诚实行为。
    const csv = buildCsvText(header, rows, { textColumns: [0] });
    downloadCsvFile(csv, `因子市场适配矩阵_${spec.label}_${new Date().toISOString().slice(0, 10)}.csv`);
  };

  const counts = result?.counts ?? {};
  const degradedCount = DEGRADED_STATUSES.reduce((s, k) => s + (counts[k] ?? 0), 0);

  return (
    <Card className="glass" data-testid="matrix-heatmap">
      <CardHeader>
        <CardTitle className="flex flex-wrap items-center justify-between gap-2">
          <span className="flex items-center gap-2">
            <Grid3X3 className="h-5 w-5" />
            因子 × 市场适配矩阵
            {result && (
              <span className="text-xs text-muted-foreground font-normal">
                {result.factors.length} 因子 × {marketCols.length} 市场 · 格 = 最近一次运行
              </span>
            )}
          </span>
          <span className="flex items-center gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={handleExportCsv}
              disabled={!result || visibleRows.length === 0}
              title="导出当前视图为 CSV"
              className="px-2.5"
            >
              <Download className="h-4 w-4" />
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={() => void load()}
              disabled={loading || factorIds.length === 0}
              title="刷新矩阵"
              className="px-2.5"
            >
              <RefreshCw className={cn('h-4 w-4', loading && 'animate-spin')} />
            </Button>
          </span>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {factorIds.length === 0 && (
          <p className="text-sm text-muted-foreground py-2">
            先在派发台选择因子——矩阵将并排展示每个因子在 CN（样本内基准）与各样本外市场的表现。
          </p>
        )}

        {error && (
          <div className="flex items-center gap-2 text-sm text-destructive">
            <AlertCircle className="h-4 w-4 flex-shrink-0" />
            {error}
          </div>
        )}

        {factorIds.length > 0 && (
          <>
            {/* 控制条：指标 / 排序 / 筛选 / 搜索 */}
            <div className="flex flex-wrap items-center gap-2 text-xs">
              <label className="flex items-center gap-1 text-muted-foreground">
                指标
                <select
                  value={metricKey}
                  onChange={(e) => setMetricKey(e.target.value)}
                  className="rounded-md border border-input bg-background px-2 py-1 text-xs"
                  aria-label="矩阵指标"
                >
                  {MATRIX_METRIC_SPECS.map((m) => (
                    <option key={m.key} value={m.key}>
                      {m.label}
                    </option>
                  ))}
                </select>
              </label>

              <div className="inline-flex items-center rounded-md border border-border/60 overflow-hidden">
                {(
                  [
                    { id: 'factor' as const, label: '因子排名' },
                    { id: 'market' as const, label: '市场排名' },
                  ]
                ).map((m) => (
                  <button
                    key={m.id}
                    type="button"
                    onClick={() => setOrderMode(m.id)}
                    className={cn(
                      'px-2 py-1 font-medium transition-colors',
                      orderMode === m.id
                        ? 'bg-primary/15 text-primary'
                        : 'text-muted-foreground hover:bg-muted/50',
                    )}
                  >
                    {m.label}
                  </button>
                ))}
              </div>

              <label className="flex items-center gap-1 text-muted-foreground">
                状态
                <select
                  value={statusFilter}
                  onChange={(e) => setStatusFilter(e.target.value as StatusFilter)}
                  className="rounded-md border border-input bg-background px-2 py-1 text-xs"
                  aria-label="状态筛选"
                >
                  {STATUS_FILTERS.map((f) => (
                    <option key={f.id} value={f.id}>
                      {f.label}
                    </option>
                  ))}
                </select>
              </label>

              {spec.direction !== 'none' && (
                <label className="flex items-center gap-1 text-muted-foreground">
                  {spec.direction === 'higher' ? `${spec.label} ≥` : `${spec.label} ≤`}
                  <input
                    type="number"
                    step="0.01"
                    value={threshold}
                    onChange={(e) => setThreshold(e.target.value)}
                    placeholder="不限"
                    className="w-20 rounded-md border border-input bg-background px-2 py-1 text-xs"
                    aria-label="指标阈值"
                  />
                </label>
              )}

              <label className="relative ml-auto flex items-center">
                <Search className="absolute left-2 h-3.5 w-3.5 text-muted-foreground" />
                <input
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  placeholder="搜因子名/ID"
                  className="w-44 rounded-md border border-input bg-background pl-7 pr-2 py-1 text-xs"
                  aria-label="搜索因子"
                />
              </label>
            </div>

            {/* 状态直方图 + 图例 */}
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted-foreground">
              <span className="inline-flex items-center gap-1">
                <span className="h-2 w-2 rounded-sm bg-rose-500/40" />
                完成 {counts.completed ?? 0}
              </span>
              <span className="inline-flex items-center gap-1">
                <span className="h-2 w-2 rounded-sm bg-amber-500/40" />
                降级 {degradedCount}
              </span>
              <span className="inline-flex items-center gap-1">
                <span className="h-2 w-2 rounded-sm bg-rose-50 border border-rose-200" />
                失败 {counts.failed ?? 0}
              </span>
              <span className="inline-flex items-center gap-1">
                <span className="h-2 w-2 rounded-sm bg-slate-100 border border-slate-200" />
                未回测 {counts.not_run ?? 0}
              </span>
              <span className="ml-auto">
                展示 {visibleRows.length} / {result?.factors.length ?? 0} 行
                {metricIsSigned(metricKey)
                  ? ' · 红=正 绿=负'
                  : ' · 琥珀按幅值加深'}
              </span>
            </div>

            {loading && !result && (
              <p className="flex items-center gap-2 text-sm text-muted-foreground py-2">
                <Loader2 className="h-4 w-4 animate-spin" /> 加载矩阵…
              </p>
            )}

            {result && visibleRows.length === 0 && (
              <p className="text-sm text-muted-foreground py-2">
                没有符合当前筛选条件的因子。
              </p>
            )}

            {result && visibleRows.length > 0 && (
              <div className="overflow-x-auto">
                <table className="w-full border-separate border-spacing-0 text-xs">
                  <thead>
                    <tr className="text-muted-foreground">
                      <th className="sticky left-0 z-10 border-b border-border/60 bg-white py-2 pr-3 text-left font-medium">
                        因子
                      </th>
                      <th className="border-b border-border/60 py-2 pr-3 text-right font-medium whitespace-nowrap">
                        CN·IC
                        <div className="text-[10px] font-normal">样本内基准</div>
                      </th>
                      {marketCols.map((col) => (
                        <th
                          key={col.market}
                          className="border-b border-border/60 px-2 py-2 text-center font-medium whitespace-nowrap"
                        >
                          <span className="inline-flex items-center gap-1">
                            {col.label}
                            {col.inSample && (
                              <span className="rounded-sm bg-indigo-50 px-1 text-[10px] font-normal text-indigo-500">
                                样本内
                              </span>
                            )}
                            {col.experimental && (
                              <span className="rounded-sm bg-slate-100 px-1 text-[10px] font-normal text-slate-500">
                                实验性
                              </span>
                            )}
                          </span>
                          <div className="text-[10px] font-normal">
                            费率 {col.costBps}bp{col.benchmark ? ` · ${col.benchmark}` : ' · 等权基准'}
                          </div>
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {visibleRows.map((row, rowIdx) => {
                      const bestMarket = bestOosMarket(row, marketCols, metricKey, spec.direction);
                      return (
                        <tr key={row.factorId} className="group">
                          <td className="sticky left-0 z-10 border-b border-border/30 bg-white py-1.5 pr-3 align-top">
                            <div className="flex items-start gap-1.5">
                              {orderMode === 'factor' && spec.direction !== 'none' && (
                                <span className="mt-px w-5 shrink-0 text-right font-mono text-[10px] text-muted-foreground">
                                  {rowIdx + 1}
                                </span>
                              )}
                              <div className="min-w-0">
                                <div
                                  className="max-w-[200px] truncate font-medium text-slate-700"
                                  title={row.factorName ?? row.factorId}
                                >
                                  {row.factorName ?? row.factorId}
                                </div>
                                <div className="text-[10px] text-muted-foreground">
                                  {row.owned ? '' : '他人因子 · 只读 '}
                                  {!row.found ? '已不在因子库' : ''}
                                </div>
                              </div>
                            </div>
                          </td>
                          <td className="border-b border-border/30 py-1.5 pr-3 text-right align-top font-mono">
                            {row.cnIc == null ? (
                              <span className="text-muted-foreground">—</span>
                            ) : (
                              formatNumber(row.cnIc, 4)
                            )}
                          </td>
                          {marketCols.map((col) => {
                            const cell: MatrixCell = row.cells[col.market] ?? {
                              status: 'not_run',
                              runId: null,
                              compat: 'unknown',
                              missing: [],
                              dynamic: false,
                              error: null,
                              universe: null,
                              dateRange: null,
                              finishedAt: null,
                              inSample: col.inSample,
                              metrics: {},
                              significance: null,
                            };
                            const value =
                              cell.status === 'completed' ? metricValue(cell, metricKey) : null;
                            const rank = columnRanks[col.market]?.get(row.factorId);
                            const isBest = bestMarket === col.market;
                            return (
                              <td
                                key={col.market}
                                className="border-b border-border/30 px-1 py-1 align-top"
                              >
                                <button
                                  type="button"
                                  data-testid={`matrix-cell-${row.factorId}-${col.market}`}
                                  data-status={cell.status}
                                  data-best={isBest ? '1' : undefined}
                                  title={cellTooltip(row, col.label, cell as MatrixCell)}
                                  onClick={() =>
                                    onOpenCell({
                                      factorId: row.factorId,
                                      factorName: row.factorName,
                                      market: col.market,
                                      marketLabel: `${col.label}${col.inSample ? '（样本内）' : '（样本外）'}`,
                                      runId: cell.runId,
                                      status: cell.status,
                                      error: cell.error,
                                      dateRange: cell.dateRange,
                                      universe: cell.universe,
                                      metrics: cell.metrics,
                                    })
                                  }
                                  className={cn(
                                    'relative block w-full min-w-[86px] rounded-md border px-1.5 py-1 text-center font-mono transition-colors',
                                    cell.status === 'completed' && value != null
                                      ? cn('border-transparent', cellToneClass(value, metricKey, maxAbs))
                                      : cell.status === 'not_run'
                                        ? 'border-dashed border-slate-200 bg-slate-50/60 text-slate-400 hover:bg-slate-100'
                                        : cn('border', degradedClass(cell.status)),
                                    'hover:ring-1 hover:ring-primary/40 cursor-pointer',
                                  )}
                                >
                                  {isBest && (
                                    <span
                                      className="absolute -left-1 -top-1 flex h-4 w-4 items-center justify-center rounded-full bg-amber-400 text-white shadow-sm"
                                      title="样本外最佳市场"
                                    >
                                      <Crown className="h-2.5 w-2.5" />
                                    </span>
                                  )}
                                  <span className="block leading-5">
                                    {cell.status === 'completed'
                                      ? value == null
                                        ? '—'
                                        : formatMetric(value, spec.format, spec.precision)
                                      : cell.status === 'running'
                                        ? '运行中…'
                                        : cell.status === 'not_run'
                                          ? cell.compat === 'data_unsupported'
                                            ? '不支持'
                                            : cell.compat === 'portable'
                                              ? '待回测'
                                              : '待裁决'
                                          : (BACKTEST_STATUS_LABELS[cell.status] ?? cell.status)}
                                  </span>
                                  {orderMode === 'market' && rank != null && (
                                    <span className="block text-[10px] opacity-70">
                                      №{rank}
                                    </span>
                                  )}
                                </button>
                              </td>
                            );
                          })}
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </>
        )}
      </CardContent>
    </Card>
  );
};

// ── 最佳市场 / 行内最优（样本外） ───────────────────────────────────

/** 行内该指标的最佳值（样本外列，完成格，≥1 个值按方向取最大/最小）——排序用 */
function bestValueForRow(
  row: MatrixFactorRow,
  cols: { market: string; inSample: boolean }[],
  metricKey: string,
  direction: 'higher' | 'lower' | 'none',
): number | null {
  const values: number[] = [];
  for (const col of cols) {
    if (col.inSample) continue;
    const cell = row.cells[col.market];
    if (!cell || cell.status !== 'completed') continue;
    const v = metricValue(cell, metricKey);
    if (v != null) values.push(v);
  }
  if (values.length === 0) return null;
  // 回撤/换手类「越小越好」——排序键必须取 min，取 max 会把最差的排最前
  return direction === 'lower' ? Math.min(...values) : Math.max(...values);
}

/**
 * Best Market 徽标：**仅样本外**列里 ≥2 个有效值才判（单值不称最佳；
 * CN 是样本内基准，不参与「适合哪个市场」的结论）。
 */
function bestOosMarket(
  row: MatrixFactorRow,
  cols: { market: string; inSample: boolean }[],
  metricKey: string,
  direction: 'higher' | 'lower' | 'none',
): string | null {
  if (direction === 'none') return null;
  const values: { market: string; value: number }[] = [];
  for (const col of cols) {
    if (col.inSample) continue;
    const cell = row.cells[col.market];
    if (!cell || cell.status !== 'completed') continue;
    const v = metricValue(cell, metricKey);
    if (v != null) values.push({ market: col.market, value: v });
  }
  if (values.length < 2) return null;
  let best = values[0];
  for (const e of values.slice(1)) {
    if (direction === 'higher' ? e.value > best.value : e.value < best.value) best = e;
  }
  return best.market;
}

export default MatrixHeatmap;
