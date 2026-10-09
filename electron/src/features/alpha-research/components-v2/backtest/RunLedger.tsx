/**
 * 运行台账（T-FB-15）——回测中心第三区：批次历史 + run 级明细。
 *
 * 两块：
 * A. 批次历史：每次批量派发一行（GET /batches），展开行拉 GET /batch/status
 *    出「因子 × 市场」单元表，单元可「看报告」下钻；运行中的批次展开即轮询。
 * B. 因子运行明细：GET /runs?factor_id= 逐行台账，勾选 2–5 条做并排对比、
 *    每行最优高亮（只有 ≥2 个有效值才判最优）——从旧 BacktestHistoryPanel
 *    移植，含**勾选随可见集收敛**守卫（否则滑出窗口的旧勾选会把界面锁死）。
 *
 * 指标键保留后端原始键（与矩阵同口径）：新面是 snake_case（ic / sharpe_net /
 * ann_return_net …），老面行（同一张表的历史数据）是驼峰键——对比/列表通过
 * `aliases` 兼容两种写法，缺失一律「—」，绝不显示成 0。
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  History,
  RefreshCw,
  AlertCircle,
  GitCompare,
  ChevronRight,
  ChevronDown,
  FileText,
  Loader2,
  Ban,
} from 'lucide-react';
import { Card, CardContent, CardHeader, CardTitle } from '../ui/Card';
import { Button } from '../ui/Button';
import {
  cancelBatch,
  getBatchStatus,
  listBacktestMarkets,
  listBatches,
  listRuns,
} from '../../services-v2/factorBacktestApi';
import type {
  BatchListItem,
  BatchStatus,
  DrillTarget,
  LedgerRun,
  MarketInfo,
} from '../../types-v2/backtestCenter';
import { BACKTEST_STATUS_LABELS } from '../../types-v2/backtestCenter';
import { cn, formatNumber, formatPercent, formatShortTime } from '../../utils-v2';

/** 同时对比的运行数上限（再多列宽就不可读了） */
const MAX_COMPARE = 5;
/** 展开批次为运行中时的轮询间隔 */
const POLL_MS = 4000;

function statusChipCls(status: string): string {
  if (status === 'completed') return 'border-emerald-200 bg-emerald-50 text-emerald-600';
  if (status === 'running') return 'border-indigo-200 bg-indigo-50 text-indigo-600';
  if (status === 'failed') return 'border-rose-200 bg-rose-50 text-rose-600';
  if (status === 'cancelled' || status === 'aborted')
    return 'border-amber-200 bg-amber-50 text-amber-600';
  if (status === 'data_unsupported' || status === 'insufficient' || status === 'unavailable')
    return 'border-slate-200 bg-slate-50 text-slate-500';
  return 'border-border bg-muted/40 text-muted-foreground';
}

function StatusChip({ status }: { status: string }) {
  return (
    <span
      className={cn(
        'inline-flex items-center rounded-md border px-1.5 py-0.5 text-[11px] font-medium',
        statusChipCls(status),
      )}
    >
      {BACKTEST_STATUS_LABELS[status] ?? status}
    </span>
  );
}

// ── 指标行定义（新面 snake 键 + 老面 camel 别名） ────────────────────

interface LedgerMetricSpec {
  key: string;
  aliases?: string[];
  label: string;
  fmt: (v: number) => string;
  direction?: 'higher' | 'lower';
}

const LIST_METRICS: LedgerMetricSpec[] = [
  { key: 'rank_ic', aliases: ['rankIc'], label: 'Rank IC', fmt: (v) => formatNumber(v, 4) },
  { key: 'sharpe_net', aliases: ['sharpeNet'], label: '扣费夏普', fmt: (v) => formatNumber(v, 2) },
  { key: 'ann_return_net', aliases: ['annReturnNet'], label: '扣费年化', fmt: (v) => formatPercent(v) },
  { key: 'max_drawdown', aliases: ['maxDrawdown'], label: '回撤', fmt: (v) => formatPercent(v) },
  { key: 'n_days', aliases: ['nObs'], label: '天数', fmt: (v) => formatNumber(v, 0) },
];

const COMPARE_ROWS: LedgerMetricSpec[] = [
  { key: 'ic', label: 'IC', fmt: (v) => formatNumber(v, 4), direction: 'higher' },
  { key: 'rank_ic', aliases: ['rankIc'], label: 'Rank IC', fmt: (v) => formatNumber(v, 4), direction: 'higher' },
  { key: 'icir', label: 'ICIR', fmt: (v) => formatNumber(v, 4), direction: 'higher' },
  { key: 'rank_icir', aliases: ['rankIcir'], label: 'Rank ICIR', fmt: (v) => formatNumber(v, 4), direction: 'higher' },
  { key: 'ann_return', aliases: ['annualReturn'], label: '年化收益（毛）', fmt: (v) => formatPercent(v), direction: 'higher' },
  { key: 'ann_return_net', aliases: ['annReturnNet'], label: '扣费年化收益', fmt: (v) => formatPercent(v), direction: 'higher' },
  { key: 'sharpe', aliases: ['sharpeRatio'], label: '夏普比率', fmt: (v) => formatNumber(v, 2), direction: 'higher' },
  { key: 'sharpe_net', aliases: ['sharpeNet'], label: '扣费夏普', fmt: (v) => formatNumber(v, 2), direction: 'higher' },
  { key: 'max_drawdown', aliases: ['maxDrawdown'], label: '最大回撤', fmt: (v) => formatPercent(v), direction: 'lower' },
  { key: 'ann_turnover', aliases: ['annTurnover'], label: '年化换手', fmt: (v) => formatNumber(v, 2) },
  { key: 'n_days', aliases: ['nObs'], label: '有效天数', fmt: (v) => formatNumber(v, 0) },
];

function metricOf(run: LedgerRun, spec: LedgerMetricSpec): number | null {
  const keys = [spec.key, ...(spec.aliases ?? [])];
  for (const k of keys) {
    const v = run.metrics[k];
    if (typeof v === 'number' && Number.isFinite(v)) return v;
  }
  return null;
}

function metricText(run: LedgerRun, spec: LedgerMetricSpec): string {
  const v = metricOf(run, spec);
  return v == null ? '—' : spec.fmt(v);
}

/** 每行最优值归属：只有 ≥2 个有效值才判（单值不称「最优」） */
function bestRunIdForRow(spec: LedgerMetricSpec, runs: LedgerRun[]): string | null {
  if (!spec.direction) return null;
  const defined = runs.filter((r) => metricOf(r, spec) != null);
  if (defined.length < 2) return null;
  let best = defined[0];
  for (const r of defined.slice(1)) {
    const a = metricOf(best, spec) as number;
    const b = metricOf(r, spec) as number;
    if (spec.direction === 'higher' ? b > a : b < a) best = r;
  }
  return best.runId;
}

// ── 主组件 ──────────────────────────────────────────────────────────

export interface RunLedgerProps {
  /** 页级因子选择（明细的因子下拉数据源） */
  factorIds: string[];
  /** 派发台刚派发的批次：出现即展开并在运行期轮询 */
  activeBatchId: string | null;
  /** 批次终态后页级自增（矩阵/台账一起重取） */
  refreshToken: number;
  onOpenRun: (target: DrillTarget) => void;
}

export const RunLedger: React.FC<RunLedgerProps> = ({
  factorIds,
  activeBatchId,
  refreshToken,
  onOpenRun,
}) => {
  // 市场档案（标签映射；失败就退化为原始 code，不阻塞台账）
  const [marketMap, setMarketMap] = useState<Record<string, MarketInfo>>({});
  useEffect(() => {
    let alive = true;
    void (async () => {
      const resp = await listBacktestMarkets();
      if (!alive || !resp.success || !resp.data) return;
      const m: Record<string, MarketInfo> = {};
      for (const info of resp.data) m[info.market] = info;
      setMarketMap(m);
    })();
    return () => {
      alive = false;
    };
  }, []);
  const marketLabel = useCallback(
    (market: string | null): string => (market ? (marketMap[market]?.label ?? market) : '—'),
    [marketMap],
  );

  // ── A. 批次历史 ─────────────────────────────────────────────────
  const [batches, setBatches] = useState<BatchListItem[]>([]);
  const [batchesLoading, setBatchesLoading] = useState(true);
  const [batchesError, setBatchesError] = useState<string | null>(null);
  const [expandedBatchId, setExpandedBatchId] = useState<string | null>(null);
  const [batchDetail, setBatchDetail] = useState<BatchStatus | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [cancelling, setCancelling] = useState(false);

  const loadBatches = useCallback(async () => {
    setBatchesLoading(true);
    const resp = await listBatches(30);
    if (resp.success && resp.data) {
      setBatches(resp.data);
      setBatchesError(null);
    } else {
      setBatchesError(resp.error ?? '查询批次列表失败');
    }
    setBatchesLoading(false);
  }, []);

  useEffect(() => {
    void loadBatches();
  }, [loadBatches, refreshToken]);

  // 新派发的批次出现即展开
  useEffect(() => {
    if (activeBatchId) setExpandedBatchId(activeBatchId);
  }, [activeBatchId]);

  const loadDetail = useCallback(async (batchId: string) => {
    setDetailLoading(true);
    const resp = await getBatchStatus(batchId);
    if (resp.success && resp.data) {
      setBatchDetail(resp.data);
      setDetailError(null);
    } else {
      setDetailError(resp.error ?? '查询批次进度失败');
    }
    setDetailLoading(false);
  }, []);

  // 展开批次：拉详情；运行中则轮询（终态后停并刷新批次列表）
  useEffect(() => {
    setBatchDetail(null);
    setDetailError(null);
    if (!expandedBatchId) return;
    let alive = true;
    void loadDetail(expandedBatchId);
    const timer = window.setInterval(async () => {
      const resp = await getBatchStatus(expandedBatchId);
      if (!alive) return;
      if (resp.success && resp.data) {
        setBatchDetail(resp.data);
        if (resp.data.batch.status !== 'running') {
          window.clearInterval(timer);
          void loadBatches();
        }
      }
    }, POLL_MS);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [expandedBatchId, loadDetail, loadBatches]);

  // 详情先于轮询一次到位时，若非运行中也别让 interval 空转
  useEffect(() => {
    if (batchDetail && batchDetail.batch.status !== 'running' && expandedBatchId) {
      void loadBatches();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [batchDetail?.batch.status, batchDetail?.batch.batchId]);

  const handleCancel = useCallback(async () => {
    if (!expandedBatchId) return;
    setCancelling(true);
    const resp = await cancelBatch(expandedBatchId);
    if (!resp.success) setDetailError(resp.error ?? '取消批次失败');
    await loadDetail(expandedBatchId);
    await loadBatches();
    setCancelling(false);
  }, [expandedBatchId, loadDetail, loadBatches]);

  // ── B. 因子运行明细 ─────────────────────────────────────────────
  const [selectedFactorId, setSelectedFactorId] = useState<string | null>(null);
  const [runs, setRuns] = useState<LedgerRun[]>([]);
  const [runsLoading, setRunsLoading] = useState(false);
  const [runsError, setRunsError] = useState<string | null>(null);
  const [marketFilter, setMarketFilter] = useState<string>('');
  const [statusFilter, setStatusFilter] = useState<string>('');
  const [selectedRunIds, setSelectedRunIds] = useState<string[]>([]);
  const runsSeqRef = useRef(0);

  // 因子下拉随页级选择收敛
  useEffect(() => {
    if (factorIds.length === 0) {
      setSelectedFactorId(null);
      return;
    }
    setSelectedFactorId((prev) => (prev && factorIds.includes(prev) ? prev : factorIds[0]));
  }, [factorIds]);

  const loadRuns = useCallback(async (factorId: string) => {
    const seq = ++runsSeqRef.current;
    setRunsLoading(true);
    const resp = await listRuns({ factorId, limit: 100 });
    if (seq !== runsSeqRef.current) return; // 过期响应整份丢弃
    if (resp.success && resp.data) {
      const next = resp.data;
      setRuns(next);
      setRunsError(null);
      // 勾选随可见集收敛：滑出窗口的旧勾选必须清掉——否则对比区没数据可
      // 渲染（消失），而复选框又因达上限全禁用，界面被永久锁死。
      setSelectedRunIds((prev) => prev.filter((id) => next.some((r) => r.runId === id)));
    } else {
      setRunsError(resp.error ?? '查询回测台账失败');
    }
    setRunsLoading(false);
  }, []);

  useEffect(() => {
    if (!selectedFactorId) {
      setRuns([]);
      return;
    }
    void loadRuns(selectedFactorId);
  }, [selectedFactorId, loadRuns, refreshToken]);

  // 筛选项来自实际数据（不说没有的市场）
  const marketOptions = useMemo(() => {
    const set = new Set<string>();
    for (const r of runs) set.add(r.market ?? '');
    return [...set].sort();
  }, [runs]);
  const statusOptions = useMemo(() => {
    const set = new Set<string>();
    for (const r of runs) set.add(r.status);
    return [...set].sort();
  }, [runs]);

  const visibleRuns = useMemo(
    () =>
      runs.filter(
        (r) =>
          (!marketFilter || (r.market ?? '') === marketFilter) &&
          (!statusFilter || r.status === statusFilter),
      ),
    [runs, marketFilter, statusFilter],
  );

  const toggleRun = (runId: string) => {
    setSelectedRunIds((prev) => {
      if (prev.includes(runId)) return prev.filter((id) => id !== runId);
      if (prev.length >= MAX_COMPARE) return prev;
      return [...prev, runId];
    });
  };

  // 对比列按时间升序（旧→新读趋势）
  const selectedRuns = useMemo(
    () =>
      visibleRuns
        .filter((r) => selectedRunIds.includes(r.runId))
        .sort((a, b) => Date.parse(a.finishedAt ?? a.createdAt ?? '') - Date.parse(b.finishedAt ?? b.createdAt ?? '')),
    [visibleRuns, selectedRunIds],
  );
  const atCap = selectedRunIds.length >= MAX_COMPARE;

  const drillFromRun = (run: LedgerRun) =>
    onOpenRun({
      factorId: run.factorId,
      market: run.market ?? '',
      marketLabel: marketLabel(run.market),
      runId: run.runId,
      status: run.status as DrillTarget['status'],
      error: run.error,
      dateRange: run.dateRange,
      universe: run.universe,
      metrics: run.metrics,
    });

  return (
    <div className="space-y-4">
      {/* ── A. 批次历史 ── */}
      <Card className="glass card-hover" data-testid="run-ledger-batches">
        <CardHeader>
          <CardTitle className="flex items-center justify-between">
            <span className="flex items-center gap-2 text-sm font-black">
              <History className="h-4 w-4 text-primary" />
              批次历史
              {batches.length > 0 && (
                <span className="text-xs font-normal text-muted-foreground">
                  最近 {batches.length} 个批次 · 展开看单元明细
                </span>
              )}
            </span>
            <Button
              variant="outline"
              size="sm"
              onClick={() => void loadBatches()}
              disabled={batchesLoading}
              title="刷新批次列表"
              className="px-2.5"
            >
              <RefreshCw className={cn('h-4 w-4', batchesLoading && 'animate-spin')} />
            </Button>
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-2">
          {batchesError && (
            <p className="flex items-center gap-2 text-sm text-destructive">
              <AlertCircle className="h-4 w-4" /> {batchesError}
            </p>
          )}
          {batches.length === 0 && !batchesLoading && !batchesError && (
            <p className="py-2 text-sm text-muted-foreground">
              还没有批量回测——在派发台选好因子与市场后发起，批次会在这里留档。
            </p>
          )}
          {batches.map((b) => {
            const expanded = expandedBatchId === b.batchId;
            const total = b.spec.factorIds.length * b.spec.markets.length;
            const detail = expanded ? batchDetail : null;
            return (
              <div key={b.batchId} className="rounded-lg border border-border/50">
                <button
                  type="button"
                  onClick={() => setExpandedBatchId(expanded ? null : b.batchId)}
                  className={cn(
                    'flex w-full flex-wrap items-center gap-x-3 gap-y-1 rounded-lg px-3 py-2 text-left text-xs transition-colors',
                    expanded ? 'bg-primary/5' : 'hover:bg-muted/30',
                  )}
                  data-testid={`batch-row-${b.batchId}`}
                >
                  {expanded ? (
                    <ChevronDown className="h-3.5 w-3.5 text-muted-foreground" />
                  ) : (
                    <ChevronRight className="h-3.5 w-3.5 text-muted-foreground" />
                  )}
                  <span className="font-mono text-[11px] text-muted-foreground">
                    {b.batchId.slice(0, 12)}…
                  </span>
                  <StatusChip status={b.status} />
                  <span className="text-slate-700">
                    {b.spec.factorIds.length} 因子 × {b.spec.markets.length} 市场
                    <span className="text-muted-foreground">（{total} 单元）</span>
                  </span>
                  <span className="text-muted-foreground">
                    {b.spec.start ?? '—'} ~ {b.spec.end ?? '—'}
                    {b.spec.costBps != null && ` · ${b.spec.costBps}bp`}
                  </span>
                  <span className="ml-auto text-muted-foreground">
                    {formatShortTime(b.createdAt)} 发起
                    {b.finishedAt && ` · ${formatShortTime(b.finishedAt)} 收口`}
                  </span>
                  {b.error && (
                    <span className="w-full truncate text-[11px] text-destructive" title={b.error}>
                      {b.error}
                    </span>
                  )}
                </button>

                {expanded && (
                  <div className="border-t border-border/40 px-3 py-3">
                    {detailLoading && !detail && (
                      <p className="flex items-center gap-2 text-xs text-muted-foreground">
                        <Loader2 className="h-3.5 w-3.5 animate-spin" /> 加载批次明细…
                      </p>
                    )}
                    {detailError && (
                      <p className="flex items-center gap-2 text-xs text-destructive">
                        <AlertCircle className="h-3.5 w-3.5" /> {detailError}
                      </p>
                    )}
                    {detail && (
                      <div className="space-y-3">
                        {/* 进度摘要 */}
                        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px]">
                          <span className="text-slate-700">
                            进度 {detail.progress.done}/{detail.progress.total}
                          </span>
                          <span className="text-emerald-600">完成 {detail.progress.completed}</span>
                          {detail.progress.failed > 0 && (
                            <span className="text-rose-600">失败 {detail.progress.failed}</span>
                          )}
                          {detail.progress.dataUnsupported > 0 && (
                            <span className="text-slate-500">
                              数据不支持 {detail.progress.dataUnsupported}
                            </span>
                          )}
                          {detail.progress.insufficient > 0 && (
                            <span className="text-slate-500">样本不足 {detail.progress.insufficient}</span>
                          )}
                          {detail.progress.unavailable > 0 && (
                            <span className="text-slate-500">数据缺失 {detail.progress.unavailable}</span>
                          )}
                          {detail.progress.cancelled > 0 && (
                            <span className="text-amber-600">取消 {detail.progress.cancelled}</span>
                          )}
                          <span className="text-muted-foreground">
                            连续失败 {detail.progress.consecFails}/{detail.progress.maxConsecFails}
                          </span>
                          {detail.batch.status === 'running' && !detail.progress.draining && (
                            <span className="text-amber-600" title="排水器不在内存中，等待引擎守护进程接管">
                              等待引擎接管…
                            </span>
                          )}
                          {detail.progress.current.length > 0 && (
                            <span className="text-muted-foreground">
                              进行中：{detail.progress.current.map((c) => `${c.factorId.slice(0, 8)}·${marketLabel(c.market)}`).join('，')}
                            </span>
                          )}
                          {detail.batch.status === 'running' && (
                            <Button
                              variant="outline"
                              size="sm"
                              className="ml-auto px-2 py-0.5 text-[11px] text-rose-600"
                              onClick={() => void handleCancel()}
                              disabled={cancelling}
                            >
                              <Ban className="h-3 w-3" /> 取消批次
                            </Button>
                          )}
                        </div>

                        {/* 熔断/失败原因（最新尝试） */}
                        {detail.failures.length > 0 && (
                          <div className="rounded-md border border-rose-200 bg-rose-50/60 p-2 text-[11px]">
                            <p className="font-medium text-rose-700">
                              失败单元（最新尝试，共 {detail.failures.length}）
                            </p>
                            <ul className="mt-1 space-y-0.5">
                              {detail.failures.slice(0, 6).map((f) => (
                                <li key={`${f.factorId}-${f.market}-${f.attempts}`} className="truncate text-rose-600" title={f.error ?? ''}>
                                  {f.factorId.slice(0, 8)} · {marketLabel(f.market)}（第 {f.attempts} 次）：{f.error ?? '—'}
                                </li>
                              ))}
                            </ul>
                          </div>
                        )}

                        {/* 单元表 */}
                        <div className="max-h-[320px] overflow-y-auto overflow-x-auto">
                          <table className="w-full text-xs">
                            <thead className="sticky top-0 bg-white/95">
                              <tr className="border-b border-border/50 text-[11px] text-muted-foreground">
                                <th className="py-1.5 pr-3 text-left font-medium">因子</th>
                                <th className="py-1.5 pr-3 text-left font-medium">市场</th>
                                <th className="py-1.5 pr-3 text-left font-medium">状态</th>
                                <th className="py-1.5 pr-3 text-right font-medium">IC</th>
                                <th className="py-1.5 pr-3 text-right font-medium">Rank IC</th>
                                <th className="py-1.5 pr-3 text-right font-medium">ICIR</th>
                                <th className="py-1.5 pr-3 text-right font-medium">夏普</th>
                                <th className="py-1.5 pr-3 text-right font-medium">回撤</th>
                                <th className="py-1.5 pr-3 text-right font-medium">天数</th>
                                <th className="py-1.5 text-right font-medium">报告</th>
                              </tr>
                            </thead>
                            <tbody>
                              {detail.units.map((u) => (
                                <tr key={`${u.factorId}-${u.market}`} className="border-b border-border/30">
                                  <td className="py-1.5 pr-3 font-mono" title={u.factorId}>
                                    {u.factorId.slice(0, 8)}
                                  </td>
                                  <td className="py-1.5 pr-3">{marketLabel(u.market)}</td>
                                  <td className="py-1.5 pr-3">
                                    <StatusChip status={u.status} />
                                    {u.attempts > 1 && (
                                      <span className="ml-1 text-[10px] text-amber-600" title="含重试">
                                        ×{u.attempts}
                                      </span>
                                    )}
                                    {u.error && (
                                      <div className="mt-0.5 max-w-[220px] truncate text-[10px] text-destructive" title={u.error}>
                                        {u.error}
                                      </div>
                                    )}
                                  </td>
                                  <td className="py-1.5 pr-3 text-right font-mono">
                                    {u.ic != null ? formatNumber(u.ic, 4) : '—'}
                                  </td>
                                  <td className="py-1.5 pr-3 text-right font-mono">
                                    {u.rankIc != null ? formatNumber(u.rankIc, 4) : '—'}
                                  </td>
                                  <td className="py-1.5 pr-3 text-right font-mono">
                                    {u.icir != null ? formatNumber(u.icir, 3) : '—'}
                                  </td>
                                  <td className="py-1.5 pr-3 text-right font-mono">
                                    {u.sharpe != null ? formatNumber(u.sharpe, 2) : '—'}
                                  </td>
                                  <td className="py-1.5 pr-3 text-right font-mono">
                                    {u.maxDrawdown != null ? formatPercent(u.maxDrawdown) : '—'}
                                  </td>
                                  <td className="py-1.5 pr-3 text-right font-mono">
                                    {u.nDays != null ? formatNumber(u.nDays, 0) : '—'}
                                  </td>
                                  <td className="py-1.5 text-right">
                                    {u.runId ? (
                                      <button
                                        type="button"
                                        onClick={() =>
                                          onOpenRun({
                                            factorId: u.factorId,
                                            market: u.market,
                                            marketLabel: marketLabel(u.market),
                                            runId: u.runId,
                                            status: u.status,
                                            error: u.error,
                                          })
                                        }
                                        className="inline-flex items-center gap-0.5 text-primary hover:underline"
                                      >
                                        <FileText className="h-3 w-3" /> 看报告
                                      </button>
                                    ) : (
                                      <span className="text-muted-foreground">—</span>
                                    )}
                                  </td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
                        </div>
                      </div>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </CardContent>
      </Card>

      {/* ── B. 因子运行明细 ── */}
      <Card className="glass card-hover" data-testid="run-ledger-runs">
        <CardHeader>
          <CardTitle className="flex items-center justify-between">
            <span className="flex items-center gap-2 text-sm font-black">
              <GitCompare className="h-4 w-4 text-primary" />
              因子运行明细
              {selectedRuns.length >= 2 && (
                <span className="text-xs font-normal text-muted-foreground">
                  勾选 {selectedRuns.length} 次对比 · 最优值已高亮
                </span>
              )}
            </span>
            <div className="flex items-center gap-2">
              {statusOptions.length > 0 && (
                <select
                  value={statusFilter}
                  onChange={(e) => setStatusFilter(e.target.value)}
                  className="rounded-md border border-border/60 bg-white/80 px-2 py-1 text-xs"
                  aria-label="按状态筛选"
                >
                  <option value="">全部状态</option>
                  {statusOptions.map((s) => (
                    <option key={s} value={s}>
                      {BACKTEST_STATUS_LABELS[s] ?? s}
                    </option>
                  ))}
                </select>
              )}
              {marketOptions.length > 1 && (
                <select
                  value={marketFilter}
                  onChange={(e) => setMarketFilter(e.target.value)}
                  className="rounded-md border border-border/60 bg-white/80 px-2 py-1 text-xs"
                  aria-label="按市场筛选"
                >
                  <option value="">全部市场</option>
                  {marketOptions.map((m) => (
                    <option key={m} value={m}>
                      {m ? marketLabel(m) : '未标注'}
                    </option>
                  ))}
                </select>
              )}
              {factorIds.length > 0 && (
                <select
                  value={selectedFactorId ?? ''}
                  onChange={(e) => setSelectedFactorId(e.target.value)}
                  className="max-w-[220px] rounded-md border border-border/60 bg-white/80 px-2 py-1 text-xs font-mono"
                  aria-label="选择因子"
                >
                  {factorIds.map((fid) => (
                    <option key={fid} value={fid}>
                      {fid}
                    </option>
                  ))}
                </select>
              )}
            </div>
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          {factorIds.length === 0 && (
            <p className="py-2 text-sm text-muted-foreground">
              先在派发台选择因子——这里会列出所选因子的逐次回测记录，支持并排对比。
            </p>
          )}
          {runsError && (
            <p className="flex items-center gap-2 text-sm text-destructive">
              <AlertCircle className="h-4 w-4" /> {runsError}
            </p>
          )}
          {factorIds.length > 0 && visibleRuns.length === 0 && !runsLoading && !runsError && (
            <p className="py-2 text-sm text-muted-foreground">
              {runs.length === 0
                ? '该因子还没有回测记录——派发批量回测或单发回测后会在这里留痕。'
                : '当前筛选下没有记录。'}
            </p>
          )}
          {visibleRuns.length > 0 && (
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="border-b border-border/50 text-[11px] text-muted-foreground">
                    <th className="w-7 py-2 pr-2" aria-label="勾选对比" />
                    <th className="py-2 pr-3 text-left font-medium">收口时间</th>
                    <th className="py-2 pr-3 text-left font-medium">市场</th>
                    <th className="py-2 pr-3 text-left font-medium">状态</th>
                    <th className="py-2 pr-3 text-left font-medium">区间</th>
                    {LIST_METRICS.map((m) => (
                      <th key={m.key} className="py-2 pr-3 text-right font-medium">
                        {m.label}
                      </th>
                    ))}
                    <th className="py-2 text-right font-medium">报告</th>
                  </tr>
                </thead>
                <tbody>
                  {visibleRuns.map((run) => (
                    <tr
                      key={run.runId}
                      className={cn(
                        'border-b border-border/30 transition-colors',
                        selectedRunIds.includes(run.runId) ? 'bg-primary/5' : 'hover:bg-muted/20',
                      )}
                    >
                      <td className="py-2 pr-2">
                        <input
                          type="checkbox"
                          checked={selectedRunIds.includes(run.runId)}
                          disabled={!selectedRunIds.includes(run.runId) && atCap}
                          onChange={() => toggleRun(run.runId)}
                          aria-label={`选择 ${run.runId} 参与对比`}
                          title={atCap && !selectedRunIds.includes(run.runId) ? `最多对比 ${MAX_COMPARE} 次` : '加入对比'}
                          className="h-3.5 w-3.5 cursor-pointer accent-primary disabled:cursor-not-allowed"
                        />
                      </td>
                      <td className="whitespace-nowrap py-2 pr-3">
                        {formatShortTime(run.finishedAt ?? run.createdAt ?? '')}
                      </td>
                      <td className="whitespace-nowrap py-2 pr-3">{marketLabel(run.market)}</td>
                      <td className="py-2 pr-3">
                        <StatusChip status={run.status} />
                        {run.error && (
                          <div className="mt-0.5 max-w-[200px] truncate text-[10px] text-destructive" title={run.error}>
                            {run.error}
                          </div>
                        )}
                      </td>
                      <td className="whitespace-nowrap py-2 pr-3 text-muted-foreground">
                        {run.dateRange ?? '—'}
                      </td>
                      {LIST_METRICS.map((m) => (
                        <td key={m.key} className="py-2 pr-3 text-right font-mono">
                          {metricText(run, m)}
                        </td>
                      ))}
                      <td className="py-2 text-right">
                        <button
                          type="button"
                          onClick={() => drillFromRun(run)}
                          className="inline-flex items-center gap-0.5 text-primary hover:underline"
                          title={run.hasSeries ? '打开报告' : '无曲线数据，打开降级说明'}
                        >
                          <FileText className="h-3 w-3" /> 看报告
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {runsLoading && (
            <p className="flex items-center gap-2 text-xs text-muted-foreground">
              <Loader2 className="h-3.5 w-3.5 animate-spin" /> 加载台账…
            </p>
          )}
          {atCap && (
            <p className="text-xs text-muted-foreground">最多对比 {MAX_COMPARE} 次，先取消一个再选。</p>
          )}

          {/* 对比表 */}
          {selectedRuns.length >= 2 && (
            <div className="border-t border-border/50 pt-3" data-testid="ledger-compare">
              <div className="overflow-x-auto">
                <table className="w-full text-xs">
                  <thead>
                    <tr className="border-b border-border/50 text-[11px]">
                      <th className="py-2 pr-4 text-left font-medium text-muted-foreground">指标</th>
                      {selectedRuns.map((run) => (
                        <th key={run.runId} className="py-2 pr-4 text-left font-medium">
                          <div className="whitespace-nowrap">
                            {marketLabel(run.market)} · {formatShortTime(run.finishedAt ?? run.createdAt ?? '')}
                          </div>
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    <tr className="border-b border-border/30">
                      <td className="py-2 pr-4 text-muted-foreground">状态</td>
                      {selectedRuns.map((run) => (
                        <td key={run.runId} className="py-2 pr-4">
                          <StatusChip status={run.status} />
                        </td>
                      ))}
                    </tr>
                    <tr className="border-b border-border/30">
                      <td className="py-2 pr-4 text-muted-foreground">回测区间</td>
                      {selectedRuns.map((run) => (
                        <td key={run.runId} className="whitespace-nowrap py-2 pr-4">
                          {run.dateRange ?? '—'}
                        </td>
                      ))}
                    </tr>
                    {COMPARE_ROWS.map((spec) => {
                      const bestRunId = bestRunIdForRow(spec, selectedRuns);
                      return (
                        <tr key={spec.key} className="border-b border-border/30">
                          <td className="py-2 pr-4 text-muted-foreground">{spec.label}</td>
                          {selectedRuns.map((run) => {
                            const isBest = bestRunId === run.runId;
                            return (
                              <td
                                key={run.runId}
                                data-best={isBest ? '1' : undefined}
                                title={isBest ? '最优' : undefined}
                                className={cn('py-2 pr-4 font-mono', isBest && 'font-semibold text-primary')}
                              >
                                {metricText(run, spec)}
                              </td>
                            );
                          })}
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
};

export default RunLedger;
