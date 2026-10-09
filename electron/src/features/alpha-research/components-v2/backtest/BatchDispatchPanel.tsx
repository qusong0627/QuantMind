/**
 * 批量派发台（T-FB-12）——回测中心 A 区。
 *
 * 因子多选（搜索/全选/清空）× 市场多选（档案卡：样本内/实验性/数据就绪）
 * × 参数（窗口/费率）→ `POST /batch`，随后轮询 `/batch/status` 展示实时进度、
 * 熔断提示与失败清单，可取消（`POST /batch/cancel`）。
 *
 * 纪律：
 * - 单批因子上限 200（后端 BatchPayload 硬闸）——超限禁用派发并给出可操作
 *   提示，绝不静默截断；
 * - 进度一切数字来自行终态回读（后端保证），组件不自行估算；
 * - 批次到终态恰好回调一次 `onBatchSettled`（驱动矩阵/台账重取）。
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Rocket,
  Search,
  RefreshCw,
  AlertCircle,
  Square,
  Loader2,
  CheckCircle2,
  Info,
} from 'lucide-react';
import { Card, CardContent, CardHeader, CardTitle } from '../ui/Card';
import { Button } from '../ui/Button';
import { getFactors } from '../../services-v2/api';
import {
  cancelBatch,
  getBatchStatus,
  launchBatch,
  listBacktestMarkets,
} from '../../services-v2/factorBacktestApi';
import type { Factor } from '../../types-v2';
import type {
  BatchLaunchResult,
  BatchStatus,
  MarketInfo,
} from '../../types-v2/backtestCenter';
import { cn, formatNumber, formatShortTime } from '../../utils-v2';

/** 单批因子上限（与后端 BatchPayload factor_ids max_length=200 同值） */
export const MAX_BATCH_FACTORS = 200;

/** 进度轮询间隔（批内单元 ~30s 级，4s 足够灵敏又不打爆后端） */
const POLL_MS = 4000;

export interface BatchDispatchPanelProps {
  factorIds: string[];
  onFactorIdsChange: (ids: string[]) => void;
  /** null = 全部市场 */
  markets: string[] | null;
  onMarketsChange: (markets: string[] | null) => void;
  activeBatchId: string | null;
  onBatchDispatched: (batchId: string) => void;
  /** 批次进入终态（完成/取消/熔断）时回调，驱动矩阵与台账重取 */
  onBatchSettled: () => void;
}

export const BatchDispatchPanel: React.FC<BatchDispatchPanelProps> = ({
  factorIds,
  onFactorIdsChange,
  markets,
  onMarketsChange,
  activeBatchId,
  onBatchDispatched,
  onBatchSettled,
}) => {
  const [factors, setFactors] = useState<Factor[]>([]);
  const [factorsLoading, setFactorsLoading] = useState(false);
  const [factorsError, setFactorsError] = useState<string | null>(null);
  const [factorSearch, setFactorSearch] = useState('');

  const [marketInfos, setMarketInfos] = useState<MarketInfo[]>([]);
  const [marketsError, setMarketsError] = useState<string | null>(null);

  const [start, setStart] = useState('');
  const [end, setEnd] = useState('');
  const [costBps, setCostBps] = useState('');

  const [launching, setLaunching] = useState(false);
  const [launchResult, setLaunchResult] = useState<BatchLaunchResult | null>(null);
  const [launchError, setLaunchError] = useState<string | null>(null);

  const [batchStatus, setBatchStatus] = useState<BatchStatus | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [cancelling, setCancelling] = useState(false);
  /** 终态回调只放一次（每个 batchId） */
  const settledRef = useRef<string | null>(null);

  // ── 因子清单 ──
  const loadFactors = useCallback(async () => {
    setFactorsLoading(true);
    try {
      const resp = await getFactors({ limit: 500 });
      if (resp.success && resp.data) {
        setFactors(resp.data.factors);
        setFactorsError(null);
      } else {
        setFactorsError(resp.error ?? '加载因子清单失败');
      }
    } finally {
      setFactorsLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadFactors();
  }, [loadFactors]);

  // ── 市场档案 ──
  useEffect(() => {
    (async () => {
      const resp = await listBacktestMarkets();
      if (resp.success && resp.data) {
        setMarketInfos(resp.data);
        setMarketsError(null);
      } else {
        setMarketsError(resp.error ?? '加载市场档案失败');
      }
    })();
  }, []);

  // ── 批次进度轮询 ──
  useEffect(() => {
    if (!activeBatchId) {
      setBatchStatus(null);
      setStatusError(null);
      return;
    }
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | null = null;

    const poll = async () => {
      const resp = await getBatchStatus(activeBatchId);
      if (!alive) return;
      if (resp.success && resp.data) {
        setBatchStatus(resp.data);
        setStatusError(null);
        if (resp.data.batch.status === 'running') {
          timer = setTimeout(poll, POLL_MS);
        } else if (settledRef.current !== activeBatchId) {
          settledRef.current = activeBatchId;
          onBatchSettled();
        }
      } else {
        setStatusError(resp.error ?? '查询批次进度失败');
        timer = setTimeout(poll, POLL_MS * 2); // 网络抖动：降频重试，不放弃
      }
    };
    void poll();
    return () => {
      alive = false;
      if (timer) clearTimeout(timer);
    };
    // onBatchSettled 引用变化不应重启动轮询
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeBatchId]);

  // ── 选择器 ──
  const filteredFactors = useMemo(() => {
    const s = factorSearch.trim().toLowerCase();
    if (!s) return factors;
    return factors.filter((f) =>
      `${f.factorName} ${f.factorId}`.toLowerCase().includes(s),
    );
  }, [factors, factorSearch]);

  const selectedSet = useMemo(() => new Set(factorIds), [factorIds]);

  const toggleFactor = (id: string) => {
    onFactorIdsChange(
      selectedSet.has(id) ? factorIds.filter((f) => f !== id) : [...factorIds, id],
    );
  };

  const selectAllFiltered = () => {
    const append = filteredFactors.map((f) => f.factorId).filter((id) => !selectedSet.has(id));
    onFactorIdsChange([...factorIds, ...append]);
  };

  const selectedMarkets = useMemo(
    () => markets ?? marketInfos.map((m) => m.market),
    [markets, marketInfos],
  );

  const toggleMarket = (market: string) => {
    const next = selectedMarkets.includes(market)
      ? selectedMarkets.filter((m) => m !== market)
      : [...selectedMarkets, market];
    onMarketsChange(next);
  };

  const readyMarkets = marketInfos.filter((m) => m.ready).map((m) => m.market);
  const effectiveMarkets = selectedMarkets.filter((m) => readyMarkets.includes(m));
  const overLimit = factorIds.length > MAX_BATCH_FACTORS;
  const canLaunch =
    !launching &&
    batchStatus?.batch.status !== 'running' &&
    factorIds.length > 0 &&
    !overLimit &&
    effectiveMarkets.length > 0;

  // ── 派发 ──
  const handleLaunch = async () => {
    setLaunching(true);
    setLaunchError(null);
    try {
      const resp = await launchBatch({
        factorIds,
        markets,
        start: start || null,
        end: end || null,
        costBps: costBps.trim() === '' ? null : Number(costBps),
      });
      if (resp.success && resp.data) {
        setLaunchResult(resp.data);
        if (resp.data.batchId) {
          onBatchDispatched(resp.data.batchId);
        }
      } else {
        setLaunchError(resp.error ?? '批量派发失败');
      }
    } finally {
      setLaunching(false);
    }
  };

  const handleCancel = async () => {
    if (!activeBatchId) return;
    setCancelling(true);
    try {
      await cancelBatch(activeBatchId);
      const resp = await getBatchStatus(activeBatchId);
      if (resp.success && resp.data) setBatchStatus(resp.data);
    } finally {
      setCancelling(false);
    }
  };

  const progress = batchStatus?.progress;
  const donePct =
    progress && progress.total > 0 ? Math.round((progress.done / progress.total) * 100) : 0;
  const isRunning = batchStatus?.batch.status === 'running';
  /** 降级态不是失败（诚实降级是结论）——分开显示 */
  const degradedCount = progress
    ? progress.dataUnsupported + progress.insufficient + progress.unavailable
    : 0;

  return (
    <Card className="glass" data-testid="batch-dispatch">
      <CardHeader>
        <CardTitle className="flex flex-wrap items-center justify-between gap-2">
          <span className="flex items-center gap-2">
            <Rocket className="h-5 w-5" />
            批量派发台
            <span className="text-xs text-muted-foreground font-normal">
              因子 × 市场 → 样本外矩阵（引擎侧排队排水，可关页面）
            </span>
          </span>
          <span className="text-xs text-muted-foreground">
            已选 <span className="font-semibold text-foreground">{factorIds.length}</span> 因子 ×{' '}
            <span className="font-semibold text-foreground">{effectiveMarkets.length}</span> 市场
            {!overLimit && factorIds.length > 0 && effectiveMarkets.length > 0 && (
              <> = {factorIds.length * effectiveMarkets.length} 单元</>
            )}
          </span>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          {/* 左：因子多选 */}
          <div>
            <div className="mb-2 flex items-center justify-between">
              <span className="text-sm font-medium">因子（{factorIds.length} 已选）</span>
              <span className="flex items-center gap-1.5">
                <button
                  type="button"
                  onClick={selectAllFiltered}
                  className="text-xs text-primary hover:underline"
                >
                  全选{filterSearchActive(factorSearch, factors) ? '筛选结果' : '全部'}
                </button>
                <span className="text-slate-300">|</span>
                <button
                  type="button"
                  onClick={() => onFactorIdsChange([])}
                  className="text-xs text-muted-foreground hover:underline"
                >
                  清空
                </button>
              </span>
            </div>
            <label className="relative mb-2 flex items-center">
              <Search className="absolute left-2 h-3.5 w-3.5 text-muted-foreground" />
              <input
                value={factorSearch}
                onChange={(e) => setFactorSearch(e.target.value)}
                placeholder="搜因子名 / ID"
                className="w-full rounded-md border border-input bg-background pl-7 pr-2 py-1.5 text-xs"
                aria-label="搜索因子"
              />
            </label>
            {factorsError && (
              <p className="flex items-center gap-1.5 text-xs text-destructive">
                <AlertCircle className="h-3.5 w-3.5" /> {factorsError}
              </p>
            )}
            <div className="max-h-56 overflow-y-auto rounded-md border border-border/60">
              {factorsLoading && (
                <p className="flex items-center gap-2 p-2 text-xs text-muted-foreground">
                  <Loader2 className="h-3.5 w-3.5 animate-spin" /> 加载因子清单…
                </p>
              )}
              {!factorsLoading && filteredFactors.length === 0 && (
                <p className="p-2 text-xs text-muted-foreground">没有匹配的因子。</p>
              )}
              {filteredFactors.map((f) => (
                <label
                  key={f.factorId}
                  className={cn(
                    'flex cursor-pointer items-center gap-2 border-b border-border/30 px-2 py-1.5 text-xs last:border-b-0',
                    selectedSet.has(f.factorId) ? 'bg-primary/5' : 'hover:bg-muted/30',
                  )}
                >
                  <input
                    type="checkbox"
                    checked={selectedSet.has(f.factorId)}
                    onChange={() => toggleFactor(f.factorId)}
                    className="h-3.5 w-3.5 accent-primary"
                  />
                  <span className="min-w-0 flex-1 truncate" title={f.factorName}>
                    {f.factorName || f.factorId}
                  </span>
                  <span className="shrink-0 font-mono text-[10px] text-muted-foreground">
                    {typeof f.ic === 'number' ? `IC ${formatNumber(f.ic, 4)}` : 'IC —'}
                  </span>
                </label>
              ))}
            </div>
            {overLimit && (
              <p className="mt-1 flex items-center gap-1.5 text-xs text-amber-600">
                <AlertCircle className="h-3.5 w-3.5 shrink-0" />
                单批上限 {MAX_BATCH_FACTORS} 个因子，当前 {factorIds.length} 个——请先减选再派发（不做静默截断）。
              </p>
            )}
          </div>

          {/* 右：市场多选 + 参数 */}
          <div className="space-y-3">
            <div className="flex items-center justify-between">
              <span className="text-sm font-medium">市场</span>
              <button
                type="button"
                onClick={() => onMarketsChange(null)}
                className="text-xs text-primary hover:underline"
              >
                全部就绪市场
              </button>
            </div>
            {marketsError && (
              <p className="flex items-center gap-1.5 text-xs text-destructive">
                <AlertCircle className="h-3.5 w-3.5" /> {marketsError}
              </p>
            )}
            <div className="flex flex-wrap gap-1.5">
              {marketInfos.map((m) => {
                const selected = selectedMarkets.includes(m.market);
                return (
                  <button
                    key={m.market}
                    type="button"
                    disabled={!m.ready}
                    onClick={() => toggleMarket(m.market)}
                    title={
                      m.ready
                        ? `${m.note ?? ''}${m.calendarStart ? ` · 日历 ${m.calendarStart}~${m.calendarEnd}` : ''}`
                        : `数据未就绪：${m.note ?? 'provider 不可读'}`
                    }
                    className={cn(
                      'rounded-lg border px-2.5 py-1.5 text-xs font-medium transition-all',
                      !m.ready && 'cursor-not-allowed border-slate-200 bg-slate-50 text-slate-300',
                      m.ready &&
                        (selected
                          ? 'border-primary bg-primary/10 text-primary'
                          : 'border-input text-muted-foreground hover:border-primary/50'),
                    )}
                    data-testid={`market-chip-${m.market}`}
                  >
                    {m.label}
                    {m.inSample && <span className="ml-1 text-[10px] opacity-70">样本内</span>}
                    {m.experimental && (
                      <span className="ml-1 text-[10px] opacity-70">实验性</span>
                    )}
                  </button>
                );
              })}
            </div>
            <div className="flex flex-wrap items-center gap-3 text-xs">
              <label className="flex items-center gap-1.5 text-muted-foreground">
                起始
                <input
                  type="date"
                  value={start}
                  onChange={(e) => setStart(e.target.value)}
                  className="rounded-md border border-input bg-background px-2 py-1 text-xs"
                  aria-label="回测起始日期"
                />
              </label>
              <label className="flex items-center gap-1.5 text-muted-foreground">
                结束
                <input
                  type="date"
                  value={end}
                  onChange={(e) => setEnd(e.target.value)}
                  className="rounded-md border border-input bg-background px-2 py-1 text-xs"
                  aria-label="回测结束日期"
                />
              </label>
              <label className="flex items-center gap-1.5 text-muted-foreground">
                费率(bp)
                <input
                  type="number"
                  min={0}
                  max={1000}
                  value={costBps}
                  onChange={(e) => setCostBps(e.target.value)}
                  placeholder="市场默认"
                  className="w-20 rounded-md border border-input bg-background px-2 py-1 text-xs"
                  aria-label="回测费率"
                />
              </label>
            </div>
            <p className="flex items-start gap-1.5 text-[11px] text-muted-foreground">
              <Info className="mt-px h-3.5 w-3.5 shrink-0" />
              留空 = 市场档案默认（窗口年数 / 费率按市场分档）；CN 列是样本内基准，其余市场为样本外重算。
            </p>

            <div className="flex justify-end">
              {isRunning ? (
                <Button variant="outline" onClick={() => void handleCancel()} disabled={cancelling}>
                  {cancelling ? (
                    <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                  ) : (
                    <Square className="h-4 w-4 mr-2" />
                  )}
                  取消批次
                </Button>
              ) : (
                <Button
                  variant="primary"
                  onClick={() => void handleLaunch()}
                  disabled={!canLaunch}
                  data-testid="launch-batch"
                >
                  {launching ? (
                    <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                  ) : (
                    <Rocket className="h-4 w-4 mr-2" />
                  )}
                  派发批量回测
                </Button>
              )}
            </div>
          </div>
        </div>

        {launchError && (
          <p className="flex items-center gap-2 text-sm text-destructive">
            <AlertCircle className="h-4 w-4" /> {launchError}
          </p>
        )}

        {launchResult && (
          <div className="rounded-lg border border-border/60 bg-muted/20 p-3 text-xs space-y-1">
            <p className="font-medium text-foreground">{launchResult.message}</p>
            {launchResult.skipped.length > 0 && (
              <div className="text-muted-foreground">
                跳过 {launchResult.skipped.length} 个：
                {launchResult.skipped.slice(0, 6).map((s) => (
                  <span key={`${s.factorId}-${s.market ?? ''}`} className="mr-2 inline-block">
                    <span className="font-mono">{s.factorId.slice(0, 8)}</span>（{s.reason}）
                  </span>
                ))}
                {launchResult.skipped.length > 6 && <span>…</span>}
              </div>
            )}
          </div>
        )}

        {/* 批次进度（活动批次） */}
        {batchStatus && (
          <div className="rounded-lg border border-border/60 p-3" data-testid="batch-progress">
            <div className="mb-2 flex flex-wrap items-center justify-between gap-2 text-xs">
              <span className="flex items-center gap-2">
                {isRunning ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin text-primary" />
                ) : batchStatus.batch.status === 'completed' ? (
                  <CheckCircle2 className="h-3.5 w-3.5 text-emerald-500" />
                ) : (
                  <AlertCircle className="h-3.5 w-3.5 text-amber-500" />
                )}
                <span className="font-mono">{batchStatus.batch.batchId}</span>
                <span
                  className={cn(
                    'rounded-md border px-1.5 py-0.5 text-[11px] font-medium',
                    batchStatus.batch.status === 'running'
                      ? 'bg-indigo-50 text-indigo-600 border-indigo-200'
                      : batchStatus.batch.status === 'completed'
                        ? 'bg-emerald-50 text-emerald-600 border-emerald-200'
                        : 'bg-amber-50 text-amber-600 border-amber-200',
                  )}
                >
                  {batchStatus.batch.status === 'running'
                    ? '排水运行中'
                    : batchStatus.batch.status === 'completed'
                      ? '已完成'
                      : batchStatus.batch.status === 'cancelled'
                        ? '已取消'
                        : '已中止'}
                </span>
                {batchStatus.batch.status === 'aborted' && batchStatus.batch.error && (
                  <span className="text-rose-500">熔断：{batchStatus.batch.error}</span>
                )}
              </span>
              <span className="text-muted-foreground">
                完成 {progress?.done ?? 0}/{progress?.total ?? 0} · 成功 {progress?.completed ?? 0} ·
                失败 {progress?.failed ?? 0} · 降级 {degradedCount} · 排队 {progress?.pending ?? 0}
              </span>
            </div>
            <div className="h-2 overflow-hidden rounded-full bg-secondary">
              <div
                className={cn(
                  'h-full rounded-full transition-all duration-500',
                  batchStatus.batch.status === 'completed'
                    ? 'bg-emerald-500'
                    : batchStatus.batch.status === 'running'
                      ? 'bg-primary'
                      : 'bg-amber-500',
                )}
                style={{ width: `${donePct}%` }}
              />
            </div>
            <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted-foreground">
              {progress && progress.current.length > 0 && (
                <span>
                  当前：{progress.current.map((c) => `${c.market}`).join('、')}
                </span>
              )}
              {progress && progress.consecFails > 0 && (
                <span className="text-amber-600">
                  连续失败 {progress.consecFails}/{progress.maxConsecFails}（达上限自动熔断保已完成）
                </span>
              )}
              {isRunning && progress && !progress.draining && (
                <span className="text-amber-600">等待引擎接管（重启恢复中）…</span>
              )}
              {statusError && <span className="text-rose-500">{statusError}</span>}
            </div>
            {batchStatus.failures.length > 0 && (
              <div className="mt-2 space-y-0.5 text-[11px]">
                <div className="text-rose-500 font-medium">
                  失败单元（{batchStatus.failures.length}）：
                </div>
                {batchStatus.failures.slice(0, 5).map((u) => (
                  <div key={`${u.factorId}-${u.market}`} className="truncate text-muted-foreground">
                    <span className="font-mono">{u.factorId.slice(0, 8)}</span> · {u.market} ·{' '}
                    {u.error ?? '未知原因'}
                  </div>
                ))}
                {batchStatus.failures.length > 5 && (
                  <div className="text-muted-foreground">…共 {batchStatus.failures.length} 条，台账页可查</div>
                )}
              </div>
            )}
            {batchStatus.batch.finishedAt && (
              <div className="mt-1 text-[11px] text-muted-foreground">
                收口于 {formatShortTime(batchStatus.batch.finishedAt)}
                {progress && progress.done > 0 && progress.completed < progress.done && (
                  <> · 完整单元 {progress.completed}（终态 ≠ 全成功，降级/失败见台账）</>
                )}
              </div>
            )}
          </div>
        )}

        {/* 已收尾批次的跳过清单（无进度块时也能看到派发结论） */}
        {!batchStatus && launchResult && launchResult.batchId && (
          <p className="text-[11px] text-muted-foreground">{launchResult.message}</p>
        )}

        {!batchStatus && !launchResult && statusError && (
          <p className="text-xs text-destructive">{statusError}</p>
        )}
      </CardContent>
    </Card>
  );
};

/** 搜索框当前是否在起过滤作用（决定「全选」文案） */
function filterSearchActive(search: string, factors: Factor[]): boolean {
  const s = search.trim().toLowerCase();
  if (!s) return false;
  return factors.some((f) =>
    `${f.factorName} ${f.factorId}`.toLowerCase().includes(s),
  );
}

export default BatchDispatchPanel;
