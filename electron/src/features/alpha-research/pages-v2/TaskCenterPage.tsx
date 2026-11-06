import React, { useCallback, useEffect, useRef, useState } from 'react';
import { AlertCircle, ArchiveRestore, ClipboardList, FileText, FileX, Loader2, RefreshCw, Square } from 'lucide-react';
import { useAppSelector } from '../../../store';
import { getSessionGeneration, registerSessionReset } from '../../../store/sessionBoundary';
import { cancelFactorJob, getFactorJobHistory, type FactorJob } from '../services-v2/factorJobsApi';
import {
  cancelMining,
  restoreMiningTask,
  getTaskMiningResult,
  getTaskHistory,
  getTaskLog,
  getTaskTrajectory,
  type MiningTaskHistoryItem,
  type MiningTaskHistoryPage,
  type MiningTaskTrajectory,
  type StatisticalMiningResult,
  type MiningTaskFactorResult,
} from '../services-v2/taskHistoryApi';
import { formatDateTime } from '../utils-v2';
import { Badge } from '../components-v2/ui/Badge';
import { Button } from '../components-v2/ui/Button';
import { Card, CardContent, CardHeader, CardTitle } from '../components-v2/ui/Card';
import { VariantLedgerDialog } from '../components-v2/VariantLedgerDialog';
import { ResearchRequestsPanel } from '../components-v2/ResearchRequestsPanel';
import { DirectionSelectionEvidence } from '../components-v2/DirectionSelectionEvidence';
import { MaterializationResult } from '../components-v2/MaterializationResult';
import { PurgeAllDataDialog } from '../components-v2/PurgeAllDataDialog';

const PAGE_SIZE = 20;
const FACTOR_JOB_PAGE_SIZE = 5;
const EMPTY_PAGE: MiningTaskHistoryPage = { tasks: [], total: 0, limit: PAGE_SIZE, offset: 0 };
const ACTIVE = new Set(['pending', 'queued', 'running', 'stopping']);

interface MiningTaskDetails {
  taskId: string;
  algorithm: string;
  progress: string | null;
  error: string | null;
  lines: string[];
  totalLines: number;
  statisticalResult: StatisticalMiningResult | null;
  factors: MiningTaskFactorResult[];
  trajectory: MiningTaskTrajectory | null;
  directionSelection?: unknown;
}

const STATUS_LABELS: Record<string, string> = {
  pending: '准备中', queued: '排队中', running: '运行中', stopping: '停止中',
  completed: '已完成', failed: '失败', cancelled: '已取消',
};

function statusVariant(status: string): 'default' | 'success' | 'warning' | 'destructive' | 'outline' {
  if (status === 'completed') return 'success';
  if (status === 'failed') return 'destructive';
  if (ACTIVE.has(status)) return 'warning';
  return 'outline';
}

function scopeLabel(value: string | null | undefined): string {
  return value === 'a_share' || value === 'CN' ? 'A股' : value || '—';
}

export const TaskCenterPage: React.FC = () => {
  const isAuthenticated = useAppSelector((state) => state.auth.isAuthenticated);
  const isAuthenticating = useAppSelector((state) => state.auth.isLoading);
  const [page, setPage] = useState<MiningTaskHistoryPage>(EMPTY_PAGE);
  const [factorJobs, setFactorJobs] = useState<FactorJob[]>([]);
  const [factorJobHasMore, setFactorJobHasMore] = useState(false);
  const [statusFilter, setStatusFilter] = useState('');
  const [showArchived, setShowArchived] = useState(false);
  const [offset, setOffset] = useState(0);
  const [factorJobOffset, setFactorJobOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [details, setDetails] = useState<MiningTaskDetails | null>(null);
  const [variantContext, setVariantContext] = useState<{ universe: string; contextId: string } | null>(null);
  const closeVariantLedger = useCallback(() => setVariantContext(null), []);
  const [purgeOpen, setPurgeOpen] = useState(false);
  const [purgeTaskId, setPurgeTaskId] = useState<string | null>(null);
  const [detailsLoading, setDetailsLoading] = useState('');
  const [cancelling, setCancelling] = useState('');
  const [archiving, setArchiving] = useState('');
  const [sessionEpoch, setSessionEpoch] = useState(() => getSessionGeneration());
  const requestEpoch = useRef(0);
  const loadingRef = useRef(false);
  const listController = useRef<AbortController | null>(null);
  const detailController = useRef<AbortController | null>(null);
  const cancelController = useRef<AbortController | null>(null);
  const archiveController = useRef<AbortController | null>(null);

  useEffect(() => registerSessionReset(() => {
    requestEpoch.current += 1;
    listController.current?.abort();
    detailController.current?.abort();
    cancelController.current?.abort();
    archiveController.current?.abort();
    listController.current = null;
    detailController.current = null;
    cancelController.current = null;
    archiveController.current = null;
    setPage(EMPTY_PAGE);
    setFactorJobs([]);
    setFactorJobHasMore(false);
    setOffset(0);
    setFactorJobOffset(0);
    setStatusFilter('');
    setShowArchived(false);
    setDetails(null);
    setVariantContext(null);
    setDetailsLoading('');
    setCancelling('');
    setArchiving('');
    setError('');
    setLoading(false);
    loadingRef.current = false;
    setSessionEpoch(getSessionGeneration());
  }), []);

  const loadHistory = useCallback(async (options?: { silent?: boolean }) => {
    const silent = options?.silent === true;
    if (!isAuthenticated || isAuthenticating) {
      setPage(EMPTY_PAGE);
      setFactorJobs([]);
      setFactorJobHasMore(false);
      setLoading(false);
      loadingRef.current = false;
      return;
    }
    // 后台静默刷新不得打断用户主动触发的加载，否则前者不负责清 loading，
    // 界面会一直停在“正在读取任务历史…”。
    if (silent && loadingRef.current) return;
    listController.current?.abort();
    const controller = new AbortController();
    listController.current = controller;
    const generation = getSessionGeneration();
    const requestId = ++requestEpoch.current;
    if (!silent) {
      setLoading(true);
      loadingRef.current = true;
      setError('');
    }
    try {
      const [response, childJobs] = await Promise.all([
        getTaskHistory({
          limit: PAGE_SIZE,
          offset,
          includeArchived: showArchived,
          ...(statusFilter ? { status: statusFilter } : {}),
        }, controller.signal),
        getFactorJobHistory({
          limit: FACTOR_JOB_PAGE_SIZE,
          offset: factorJobOffset,
          ...(statusFilter ? { status: statusFilter } : {}),
        }, controller.signal),
      ]);
      if (controller.signal.aborted || generation !== getSessionGeneration() || requestId !== requestEpoch.current) return;
      setPage(response ?? EMPTY_PAGE);
      setFactorJobs(childJobs.jobs);
      setFactorJobHasMore(childJobs.has_more);
    } catch (cause) {
      // 后台刷新失败保持静默，避免轮询把偶发网络抖动变成常驻错误横幅。
      if (!silent && !controller.signal.aborted && generation === getSessionGeneration() && requestId === requestEpoch.current) {
        setError(cause instanceof Error ? cause.message : '任务历史读取失败');
      }
    } finally {
      if (controller.signal.aborted || generation !== getSessionGeneration() || requestId !== requestEpoch.current) {
        return;
      }
      if (!silent) {
        setLoading(false);
        loadingRef.current = false;
      }
    }
  }, [isAuthenticated, isAuthenticating, offset, factorJobOffset, statusFilter, showArchived]);

  useEffect(() => {
    void loadHistory();
    return () => {
      listController.current?.abort();
      detailController.current?.abort();
      cancelController.current?.abort();
      archiveController.current?.abort();
    };
  }, [loadHistory, sessionEpoch]);

  // 有活动任务才轮询；后台刷新静默；页面不可见时暂停并在回到前台时补一次。
  const hasActiveWork =
    page.tasks.some((task) => ACTIVE.has(task.status))
    || factorJobs.some((job) => ACTIVE.has(job.status));

  useEffect(() => {
    if (!isAuthenticated || isAuthenticating || !hasActiveWork) return undefined;
    let timer: number | null = null;
    const stop = () => {
      if (timer !== null) {
        window.clearInterval(timer);
        timer = null;
      }
    };
    const start = () => {
      if (timer !== null || document.hidden) return;
      timer = window.setInterval(() => void loadHistory({ silent: true }), 10000);
    };
    const onVisibilityChange = () => {
      if (document.hidden) {
        stop();
        return;
      }
      void loadHistory({ silent: true });
      start();
    };
    start();
    document.addEventListener('visibilitychange', onVisibilityChange);
    return () => {
      stop();
      document.removeEventListener('visibilitychange', onVisibilityChange);
    };
  }, [hasActiveWork, isAuthenticated, isAuthenticating, loadHistory, sessionEpoch]);

  const openDetails = async (item: MiningTaskHistoryItem) => {
    detailController.current?.abort();
    const controller = new AbortController();
    detailController.current = controller;
    const generation = getSessionGeneration();
    setDetailsLoading(item.task_id);
    setDetails({ taskId: item.task_id, algorithm: item.algorithm, progress: item.progress, error: item.error, lines: [], totalLines: 0, statisticalResult: null, factors: [], trajectory: null, directionSelection: item.params_snapshot?.direction_selection });
    const [logResult, resultResponse, trajectoryResponse] = await Promise.all([
      getTaskLog(item.task_id, controller.signal).then(
        (value) => ({ ok: true as const, value }),
        () => ({ ok: false as const }),
      ),
      getTaskMiningResult(item.task_id, controller.signal).then(
          (value) => ({ ok: true as const, value }),
          () => ({ ok: false as const }),
        ),
      item.algorithm !== 'statistical'
        ? getTaskTrajectory(item.task_id, controller.signal).then(
          (value) => ({ ok: true as const, value }),
          () => ({ ok: false as const }),
        )
        : Promise.resolve({ ok: false as const }),
    ]);
    if (controller.signal.aborted || generation !== getSessionGeneration()) return;
    setDetails({
      taskId: item.task_id,
      algorithm: item.algorithm,
      progress: item.progress,
      error: item.error,
      lines: logResult.ok ? logResult.value.lines.slice(-200) : [],
      totalLines: logResult.ok ? logResult.value.total : 0,
      statisticalResult: resultResponse.ok ? resultResponse.value.result : null,
      factors: resultResponse.ok ? resultResponse.value.factors : [],
      trajectory: trajectoryResponse.ok ? trajectoryResponse.value : null,
      directionSelection: item.params_snapshot?.direction_selection,
    });
    setDetailsLoading('');
  };

  const cancelTask = async (item: MiningTaskHistoryItem) => {
    cancelController.current?.abort();
    const controller = new AbortController();
    cancelController.current = controller;
    setCancelling(`mining:${item.task_id}`);
    const generation = getSessionGeneration();
    try {
      await cancelMining(item.task_id, controller.signal);
      if (generation === getSessionGeneration()) await loadHistory();
    } catch (cause) {
      if (generation === getSessionGeneration()) {
        setError(cause instanceof Error ? cause.message : '取消请求失败');
      }
    } finally {
      if (generation === getSessionGeneration()) setCancelling('');
    }
  };

  const restoreTask = async (item: MiningTaskHistoryItem) => {
    if (ACTIVE.has(item.status)) return;
    archiveController.current?.abort();
    const controller = new AbortController();
    archiveController.current = controller;
    const generation = getSessionGeneration();
    setArchiving(item.task_id);
    try {
      await restoreMiningTask(item.task_id, controller.signal);
      if (generation === getSessionGeneration()) await loadHistory();
    } catch (cause) {
      if (generation === getSessionGeneration()) {
        setError(cause instanceof Error ? cause.message : '恢复任务失败');
      }
    } finally {
      if (generation === getSessionGeneration()) {
        setArchiving('');
        if (archiveController.current === controller) archiveController.current = null;
      }
    }
  };

  const cancelFactorResearchJob = async (job: FactorJob) => {
    cancelController.current?.abort();
    const controller = new AbortController();
    cancelController.current = controller;
    setCancelling(`factor-job:${job.job_id}`);
    const generation = getSessionGeneration();
    try {
      await cancelFactorJob(job.job_id, controller.signal);
      if (generation === getSessionGeneration()) await loadHistory();
    } catch (cause) {
      if (generation === getSessionGeneration()) {
        setError(cause instanceof Error ? cause.message : '停止回测/验证任务失败');
      }
    } finally {
      if (generation === getSessionGeneration()) setCancelling('');
    }
  };

  return (
    <div className="space-y-4 animate-fade-in-up">
      <header className="flex items-center justify-between gap-4">
        <div>
          <h1 className="text-xl font-bold flex items-center gap-2">
            <ClipboardList className="h-5 w-5 text-primary" />任务中心
          </h1>
          <p className="text-xs text-muted-foreground mt-0.5 max-w-2xl">挖掘、回测与验证任务均按当前属主读取，状态每 10 秒同步。归档仅隐藏终态历史，因子与审计数据保留。</p>
        </div>
        <Button variant="outline" size="sm" className="text-xs shrink-0" onClick={() => void loadHistory()} disabled={loading}>
          <RefreshCw className={`h-3.5 w-3.5 mr-1.5 ${loading ? 'animate-spin' : ''}`} />刷新
        </Button>
      </header>

      <Card className="glass">
        <CardContent className="p-3 flex flex-wrap items-center justify-between gap-3">
          <label className="text-xs flex items-center gap-2">
            状态
            <select
              value={statusFilter}
              onChange={(event) => { setOffset(0); setFactorJobOffset(0); setStatusFilter(event.target.value); }}
              className="rounded-md border border-border bg-background px-2 py-1.5 text-xs"
            >
              <option value="">全部</option>
              {Object.entries(STATUS_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
            </select>
          </label>
          <span className="text-xs text-muted-foreground">共 {page.total} 条 · 第 {Math.floor(offset / PAGE_SIZE) + 1} 页</span>
          <Button variant="outline" size="sm" onClick={() => { setOffset(0); setShowArchived(!showArchived); }}>
            {showArchived ? '隐藏已归档' : '查看已归档'}
          </Button>
        </CardContent>
      </Card>

      {error && (
        <Card className="border-destructive/50"><CardContent className="p-4 flex items-center gap-2 text-destructive">
          <AlertCircle className="h-4 w-4" />{error}
        </CardContent></Card>
      )}

      <Card className="glass">
        <CardHeader className="pb-2"><CardTitle className="text-sm">挖掘任务历史</CardTitle></CardHeader>
        <CardContent className="p-0 overflow-x-auto">
          <table className="w-full text-xs">
            <thead><tr className="border-b border-border/60 text-muted-foreground">
              <th className="px-3 py-2 text-left font-medium whitespace-nowrap">任务</th>
              <th className="px-3 py-2 text-left font-medium whitespace-nowrap">算法</th>
              <th className="px-3 py-2 text-left font-medium whitespace-nowrap">市场 / 池</th>
              <th className="px-2 py-2 font-medium whitespace-nowrap">状态</th>
              <th className="px-3 py-2 text-left font-medium whitespace-nowrap">进度</th>
              <th className="px-2 py-2 font-medium whitespace-nowrap">因子</th>
              <th className="px-3 py-2 text-left font-medium whitespace-nowrap">创建时间</th>
              <th className="px-3 py-2 text-right font-medium whitespace-nowrap">操作</th>
            </tr></thead>
            <tbody>
              {page.tasks.map((item) => {
                const pct = typeof item.progress_pct === 'number' && Number.isFinite(item.progress_pct)
                  ? Math.max(0, Math.min(100, Math.round(item.progress_pct)))
                  : null;
                const detailBusy = detailsLoading === item.task_id;
                const cancelKey = `mining:${item.task_id}`;
                return (
                <tr key={item.task_id} className="border-b border-border/40 hover:bg-muted/30 align-top">
                  <td className="px-3 py-2 max-w-[280px]">
                    <button className="font-mono text-[11px] text-primary hover:underline" title={item.task_id} onClick={() => void openDetails(item)}>
                      {item.task_id.slice(0, 8)}…
                    </button>
                    <div className="mt-0.5 truncate text-muted-foreground" title={item.direction || ''}>{item.direction || '—'}</div>
                    {item.batch_id && <div className="mt-0.5 text-[11px] text-blue-600" title={item.batch_id}>批次 {item.batch_id.slice(0, 8)}{typeof item.batch_index === 'number' && typeof item.batch_size === 'number' ? ` · 方向 ${item.batch_index + 1}/${item.batch_size}` : ''}</div>}
                    {item.source_doc_id && <div className="mt-0.5 text-[11px] text-muted-foreground" title={item.source_doc_id}>来自本人研究文档</div>}
                    {item.seed_factor_ids?.length > 0 && <div className="mt-0.5 truncate text-[11px] text-muted-foreground" title={item.seed_factor_ids.join('、')}>父本：{item.seed_factor_ids.map((id) => id.slice(0, 8)).join('、')}</div>}
                  </td>
                  <td className="px-3 py-2 whitespace-nowrap">{item.algorithm === 'statistical' ? '统计挖掘' : 'RD-Agent'}<div className="text-[11px] text-muted-foreground">{item.algorithm_version || '版本未知'}</div></td>
                  <td className="px-3 py-2 whitespace-nowrap">{scopeLabel(item.market)}<span className="text-muted-foreground"> · {item.universe || '—'}</span></td>
                  <td className="px-2 py-2 text-center"><Badge variant={statusVariant(item.status)} className="whitespace-nowrap">{STATUS_LABELS[item.status] || item.status}</Badge></td>
                  <td className="px-3 py-2 min-w-[150px]">
                    {pct === null ? (
                      <span className="text-muted-foreground">—</span>
                    ) : (
                      <div title={`${item.phase || item.status} · ${pct}%${item.progress ? ` · ${item.progress}` : ''}`}>
                        <div className="flex items-center justify-between gap-2">
                          <span className="truncate">{item.phase || item.status}</span>
                          <span className="font-mono shrink-0">{pct}%</span>
                        </div>
                        <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-muted">
                          <div className="h-full rounded-full bg-primary transition-all" style={{ width: `${pct}%` }} />
                        </div>
                      </div>
                    )}
                    {item.progress && <div className="mt-1 max-w-[190px] truncate text-[11px] text-muted-foreground" title={item.progress}>{item.progress}</div>}
                  </td>
                  <td className="px-2 py-2 text-center font-mono">{item.factor_count}</td>
                  <td className="px-3 py-2 whitespace-nowrap font-mono text-[11px]">{formatDateTime(item.created_at)}</td>
                  <td className="px-3 py-2 text-right whitespace-nowrap">
                    <Button variant="ghost" size="sm" className="h-7 w-7 p-0" title="详情 / 日志" onClick={() => void openDetails(item)} disabled={detailBusy}>
                      {detailBusy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <FileText className="h-3.5 w-3.5" />}
                    </Button>
                    {ACTIVE.has(item.status) && (
                      <Button variant="ghost" size="sm" className="h-7 w-7 p-0" title="停止任务" disabled={cancelling === cancelKey} onClick={() => void cancelTask(item)}>
                        {cancelling === cancelKey ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Square className="h-3.5 w-3.5" />}
                      </Button>
                    )}
                    {!ACTIVE.has(item.status) && item.archived_at && (
                      <Button variant="ghost" size="sm" className="h-7 w-7 p-0" title="恢复任务" disabled={archiving === item.task_id} onClick={() => void restoreTask(item)}>
                        {archiving === item.task_id ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <ArchiveRestore className="h-3.5 w-3.5" />}
                      </Button>
                    )}
                    {!ACTIVE.has(item.status) && (
                      <Button variant="ghost" size="sm" className="h-7 w-7 p-0" title="全面清理（删除任务记录、因子与产物，不可恢复）" onClick={() => setPurgeTaskId(item.task_id)}>
                        <FileX className="h-3.5 w-3.5 text-destructive" />
                      </Button>
                    )}
                  </td>
                </tr>
                );
              })}
              {!loading && page.tasks.length === 0 && <tr><td colSpan={8} className="p-8 text-center text-xs text-muted-foreground">没有符合条件的任务</td></tr>}
              {loading && <tr><td colSpan={8} className="p-8 text-center text-xs text-muted-foreground">正在读取任务历史…</td></tr>}
            </tbody>
          </table>
        </CardContent>
        <div className="flex items-center justify-between gap-2 border-t border-border/40 p-3">
          <span className="text-xs text-muted-foreground">共 {page.total} 条 · 第 {Math.floor(offset / PAGE_SIZE) + 1} / {Math.max(1, Math.ceil(page.total / PAGE_SIZE))} 页</span>
          <div className="flex gap-2">
            <Button variant="outline" size="sm" disabled={offset === 0 || loading} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>上一页</Button>
            <Button variant="outline" size="sm" disabled={offset + PAGE_SIZE >= page.total || loading} onClick={() => setOffset(offset + PAGE_SIZE)}>下一页</Button>
          </div>
        </div>
      </Card>

      <Card className="glass">
        <CardHeader className="pb-2"><CardTitle className="text-sm">因子与研究作业</CardTitle></CardHeader>
        <CardContent className="space-y-3">
          {factorJobs.length === 0 ? (
          <div className="py-6 text-center text-muted-foreground">暂无因子与研究作业</div>
          ) : factorJobs.map((job) => {
            const cancelKey = `factor-job:${job.job_id}`;
            const metrics = job.result_snapshot?.metrics ?? {};
            const comboMetrics = job.result_snapshot?.validation_metrics as Record<string, unknown> | undefined;
            const comboWeights = job.result_snapshot?.weights as Record<string, unknown> | undefined;
            const purgeDeleted = (job.result_snapshot?.deleted as Record<string, number> | undefined) ?? {};
            const purgeRecordCount = Object.values(purgeDeleted).reduce((sum, value) => sum + Number(value || 0), 0);
            return (
              <article key={job.job_id} className="rounded-lg border border-border/60 p-3 space-y-1.5">
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <div className="min-w-0">
                    <div className="font-medium text-sm">
                      {job.kind === 'purge_task' ? '单任务彻底删除' : job.kind === 'purge' ? '清空挖掘数据' : job.kind === 'pool_research' ? '池正交评估' : job.kind === 'recovery' ? '因子补码评估' : job.kind === 'cleanup' ? '任务文件清理' : job.kind === 'doc_parse' ? '文档解析' : job.kind === 'doc_organize' ? '文档整理' : job.kind === 'validate' ? '样本外验证' : job.kind === 'materialize' ? '因子物化' : job.kind === 'compose' ? '组合优化' : '单因子回测'}
                      {' · '}{job.kind === 'purge_task' ? `任务 ${(job.request.task_id as string | undefined)?.slice(0, 8) ?? '未知'}…` : job.kind === 'purge' ? '全部挖掘数据' : job.kind === 'cleanup' ? `${job.request.task_ids?.length || 0} 个本人任务` : job.kind === 'doc_parse' || job.kind === 'doc_organize' ? job.request.doc_id || '本人文档' : job.kind === 'compose' || job.kind === 'recovery' || job.kind === 'pool_research' ? job.request.name || `${job.request.factor_ids?.length || 0} 个本人因子` : job.request.factor_id || '本人因子'}
                      {job.kind === 'doc_organize' && job.request.organize_kind ? ` · ${job.request.organize_kind === 'paper' ? '论文复现' : '自由挖掘'}` : ''}
                    </div>
                    <div className="text-xs text-muted-foreground font-mono mt-1">{job.job_id}{job.request.universe ? ` · ${job.request.universe}` : ''} · {job.created_at ? formatDateTime(job.created_at) : '时间未知'}</div>
                  </div>
                  <div className="flex items-center gap-2">
                    <Badge variant={statusVariant(job.status)}>{STATUS_LABELS[job.status] || job.status}</Badge>
                    {ACTIVE.has(job.status) && (
                      <Button variant="ghost" size="sm" disabled={cancelling === cancelKey} onClick={() => void cancelFactorResearchJob(job)}>
                        {cancelling === cancelKey ? <Loader2 className="h-4 w-4 animate-spin" /> : <><Square className="h-3 w-3 mr-1" />停止</>}
                      </Button>
                    )}
                  </div>
                </div>
                {job.progress_snapshot && <div className="text-xs text-muted-foreground">{String(job.progress_snapshot.stage || job.status)}{typeof job.progress_snapshot.progress_pct === 'number' ? ` · ${job.progress_snapshot.progress_pct}%` : ''}</div>}
                {job.status === 'completed' && (job.kind === 'backtest' || job.kind === 'validate') && <div className="text-xs text-muted-foreground">IC {typeof metrics.ic_mean === 'number' ? metrics.ic_mean.toFixed(4) : '—'} · Rank IC {typeof metrics.rank_ic_median === 'number' ? metrics.rank_ic_median.toFixed(4) : '—'} · 年化 {typeof metrics.annual_return === 'number' ? `${(metrics.annual_return * 100).toFixed(2)}%` : '—'}</div>}
                {job.kind === 'materialize' && <MaterializationResult job={job} />}
                {job.kind === 'purge' && job.status === 'completed' && <div className="text-xs text-muted-foreground">已清空 {purgeRecordCount} 条本人挖掘记录及其产物</div>}
                {job.kind === 'purge_task' && job.status === 'completed' && <div className="text-xs text-muted-foreground">已删除该任务 {purgeRecordCount} 条记录及其产物</div>}
                {job.kind === 'pool_research' && <div className="text-xs text-muted-foreground">使用提交时冻结的面板与代码版本；逐因子正交性、残差 IC 和父本证据可在因子库选择原股票池后查看“池正交与多样性证据”历史。</div>}
                {job.kind === 'recovery' && <div className="text-xs text-muted-foreground">已保存代码版本不会因取消或评估失败而撤回。{job.result_snapshot ? `评估成功 ${String(job.result_snapshot.completed ?? 0)} · 失败 ${String(job.result_snapshot.failed ?? 0)} · 未处理 ${Array.isArray(job.result_snapshot.unprocessed) ? job.result_snapshot.unprocessed.length : 0}` : ''}</div>}
                {job.status === 'completed' && job.kind === 'cleanup' && <div className="text-xs text-muted-foreground">本次释放 {typeof job.result_snapshot?.removed_bytes === 'number' ? (job.result_snapshot.removed_bytes / (1024 ** 2)).toFixed(2) : '—'} MiB · 任务记录与已入库因子保留</div>}
                {job.kind === 'cleanup' && job.request.task_ids && <div className="break-all text-xs text-muted-foreground">{job.request.task_ids.join('、')}</div>}
                {job.kind === 'cleanup' && (job.status === 'failed' || job.status === 'cancelled' || job.status === 'stopping') && <div className="text-xs text-muted-foreground">停止在当前目录处理结束后生效；部分文件可能已删除。可从原挖掘任务重新预览剩余文件，再提交清理。</div>}
                {job.status === 'completed' && job.kind === 'compose' && <div className="text-xs text-muted-foreground">验证 Rank IC {typeof comboMetrics?.mean_rank_ic === 'number' ? comboMetrics.mean_rank_ic.toFixed(4) : '—'} · 验证年化 {typeof comboMetrics?.annual_return === 'number' ? `${(comboMetrics.annual_return * 100).toFixed(2)}%` : '—'} · 权重 {comboWeights ? Object.entries(comboWeights).map(([factorId, weight]) => `${factorId.slice(0, 12)} ${typeof weight === 'number' ? weight.toFixed(3) : '—'}`).join('，') : '—'}</div>}
                {job.status === 'completed' && job.kind === 'doc_parse' && <div className="text-xs text-muted-foreground">文档解析完成，产物状态可在研究文档页查看</div>}
                {job.status === 'completed' && job.kind === 'doc_organize' && <div className="text-xs text-muted-foreground">整理草稿已保存，可在研究文档页编辑并确认</div>}
                {job.status === 'failed' && job.error_message && <div className="text-xs text-destructive">{job.error_message}</div>}
              </article>
            );
          })}
        </CardContent>
        <div className="flex items-center justify-between gap-2 p-3 border-t border-border/40">
          <span className="text-xs text-muted-foreground">第 {Math.floor(factorJobOffset / FACTOR_JOB_PAGE_SIZE) + 1} 页</span>
          <div className="flex gap-2">
            <Button variant="outline" size="sm" disabled={factorJobOffset === 0 || loading} onClick={() => setFactorJobOffset(Math.max(0, factorJobOffset - FACTOR_JOB_PAGE_SIZE))}>上一页</Button>
            <Button variant="outline" size="sm" disabled={!factorJobHasMore || loading} onClick={() => setFactorJobOffset(factorJobOffset + FACTOR_JOB_PAGE_SIZE)}>下一页</Button>
          </div>
        </div>
      </Card>

      <Card className="border-destructive/40">
        <CardHeader className="pb-2"><CardTitle className="text-sm text-destructive">危险操作</CardTitle></CardHeader>
        <CardContent className="flex flex-wrap items-center justify-between gap-3">
          <p className="max-w-2xl text-xs text-muted-foreground">
            清空本人全部挖掘任务、因子与版本、因子池、研究文档、方向批次、研究请求、变体记忆、物化特征和任务产物。不可恢复；清理作业本身会保留一条审计记录。
          </p>
          <Button variant="destructive" size="sm" onClick={() => setPurgeOpen(true)}>清空全部挖掘数据</Button>
        </CardContent>
      </Card>

      {purgeOpen && (
        <PurgeAllDataDialog
          onClose={() => setPurgeOpen(false)}
          onSubmitted={() => { setOffset(0); setFactorJobOffset(0); void loadHistory(); }}
        />
      )}

      {purgeTaskId && (
        <PurgeAllDataDialog
          taskId={purgeTaskId}
          onClose={() => setPurgeTaskId(null)}
          onSubmitted={() => { setOffset(0); setFactorJobOffset(0); void loadHistory(); }}
        />
      )}

      <ResearchRequestsPanel />

      {details && (
        <Card className="glass">
          <CardHeader className="flex flex-row items-center justify-between">
            <CardTitle className="text-sm">任务详情 · <span className="font-mono text-xs">{details.taskId}</span></CardTitle>
            <Button variant="ghost" size="sm" onClick={() => { detailController.current?.abort(); setDetailsLoading(''); setDetails(null); }}>关闭</Button>
          </CardHeader>
          <CardContent className="space-y-4">
            <DirectionSelectionEvidence value={details.directionSelection} />
            <div className="text-xs text-muted-foreground">{details.progress || '任务状态已加载'}{details.error ? ` · ${details.error}` : ''}</div>
            {details.algorithm === 'statistical' && (
              <div>
                <div className="text-xs font-medium mb-1.5">统计派结果</div>
                {!details.statisticalResult ? (
                  <p className="text-xs text-muted-foreground">结果暂不可用；请确认任务已完成后刷新详情。</p>
                ) : (
                  <div className="space-y-3">
                    <p className="text-xs text-muted-foreground">
                      {details.statisticalResult.window?.mining_start || '日期未知'} 至 {details.statisticalResult.window?.mining_end || '日期未知'} · {details.statisticalResult.universe || 'A 股'} · {details.statisticalResult.n_symbols ?? '—'} 只标的 · {details.statisticalResult.panel_rows ?? '—'} 行
                    </p>
                    {details.statisticalResult.variant_context_id && details.statisticalResult.universe && (
                      <div className="space-y-1 text-xs text-muted-foreground">
                        <p>本轮按冻结冷却名单跳过 {details.statisticalResult.cooled_keys?.length ?? 0} 个变体{details.statisticalResult.cooled_keys?.length ? `：${details.statisticalResult.cooled_keys.join('、')}` : ''}；多重检验按实际评估候选数计算。</p>
                        <button type="button" className="text-blue-600" onClick={() => {
                          const result = details.statisticalResult;
                          if (result?.universe && result.variant_context_id) setVariantContext({ universe: result.universe, contextId: result.variant_context_id });
                        }}>查看本次研究环境的变体台账</button>
                      </div>
                    )}
                    <div className="max-h-80 overflow-auto rounded-lg border border-border/50">
                      <table className="w-full text-xs">
                        <thead className="sticky top-0 bg-background text-muted-foreground">
                          <tr><th className="p-2 text-left">候选因子</th><th className="p-2">选择分</th><th className="p-2">Rank IC</th><th className="p-2">ICIR</th><th className="p-2">OOS Rank IC</th></tr>
                        </thead>
                        <tbody>
                          {details.statisticalResult.candidates.map((candidate) => (
                            <tr key={candidate.key} className="border-t border-border/40 align-top">
                              <td className="p-2 min-w-[240px]">
                                <div className="font-medium">{candidate.name} {candidate.selected ? <Badge variant="success">入选</Badge> : null}</div>
                                <code className="mt-1 block break-all text-muted-foreground">{candidate.expression}</code>
                                {!candidate.selected && candidate.reject_reason && <div className="mt-1 text-muted-foreground">{candidate.reject_reason}</div>}
                              </td>
                              <td className="p-2 text-center">{typeof candidate.selection_score === 'number' ? candidate.selection_score.toFixed(4) : '—'}</td>
                              <td className="p-2 text-center">{typeof candidate.rank_ic_mean === 'number' ? candidate.rank_ic_mean.toFixed(4) : '—'}</td>
                              <td className="p-2 text-center">{typeof candidate.icir === 'number' ? candidate.icir.toFixed(4) : '—'}</td>
                              <td className="p-2 text-center">{typeof candidate.oos_rank_ic === 'number' ? candidate.oos_rank_ic.toFixed(4) : '—'}</td>
                            </tr>
                          ))}
                          {details.statisticalResult.candidates.length === 0 && <tr><td colSpan={5} className="p-4 text-center text-muted-foreground">本次没有候选因子结果</td></tr>}
                        </tbody>
                      </table>
                    </div>
                  </div>
                )}
              </div>
            )}
            <div>
              <div className="text-xs font-medium mb-1.5">本人因子结果</div>
              {details.factors.length === 0 ? (
                <p className="text-xs text-muted-foreground">当前任务尚无已落库因子。</p>
              ) : (
                <div className="max-h-72 overflow-auto rounded-lg border border-border/50">
                  <table className="w-full text-xs">
                    <thead className="sticky top-0 bg-background text-muted-foreground">
                      <tr><th className="p-2 text-left">因子</th><th className="p-2">状态</th><th className="p-2">Rank IC</th><th className="p-2">版本</th></tr>
                    </thead>
                    <tbody>
                      {details.factors.map((factor) => (
                        <tr key={factor.factor_id} className="border-t border-border/40 align-top">
                          <td className="p-2 min-w-[240px]">
                            <div className="font-medium">{factor.factor_name || factor.factor_id}</div>
                            <code className="mt-1 block break-all text-muted-foreground">{factor.factor_formulation || '表达式暂不可用'}</code>
                          </td>
                          <td className="p-2 text-center">{factor.status}</td>
                          <td className="p-2 text-center">{typeof factor.rank_ic === 'number' && Number.isFinite(factor.rank_ic) ? factor.rank_ic.toFixed(4) : '—'}</td>
                          <td className="p-2 text-center">{factor.factor_revision_no ?? '—'}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
            {details.algorithm !== 'statistical' && details.trajectory && (
              <div>
                <div className="text-xs font-medium mb-1.5">RD-Agent 决策轨迹</div>
                <p className="text-xs text-muted-foreground mb-3">
                  阶段耗时来自文件时间，因子归因使用受限反序列化；无法识别的载荷会降级为任务级。{details.trajectory.truncated ? ` 仅展示最近 ${details.trajectory.loops.length} 轮，省略 ${details.trajectory.dropped_loops} 轮。` : ''}
                </p>
                {details.trajectory.attribution === 'task_level' && details.trajectory.factors && (
                  <div className="mb-3 rounded-lg border border-border/50 p-3 text-xs">
                    <div className="font-medium mb-2">任务级因子（载荷无法可靠归属到具体轮次）</div>
                    {details.trajectory.factors.length === 0 ? <span className="text-muted-foreground">没有已落库因子</span> : details.trajectory.factors.map((factor) => (
                      <div key={factor.name} className="flex flex-wrap justify-between gap-2 py-1">
                        <span>{factor.name}</span><span className="text-muted-foreground">{factor.outcome || '结局未知'}</span>
                      </div>
                    ))}
                  </div>
                )}
                {details.trajectory.loops.length === 0 ? (
                  <p className="text-xs text-muted-foreground">尚无 Loop 阶段文件。</p>
                ) : (
                  <div className="space-y-3">
                    {details.trajectory.loops.map((loop) => (
                      <div key={loop.loop} className="rounded-lg border border-border/50 p-3">
                        <div className="flex flex-wrap items-center gap-2 mb-2">
                          <div className="font-medium text-sm">Loop {loop.loop}</div>
                          {loop.flags.map((flag) => <Badge key={flag} variant="warning">{details.trajectory?.flag_texts[flag] || flag}</Badge>)}
                        </div>
                        <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
                          {loop.phases.map((phase) => (
                            <div key={phase.phase} className="rounded bg-muted/30 px-3 py-2 text-xs">
                              <div className="flex justify-between gap-2"><span>{phase.label}</span><span className="text-muted-foreground">{phase.status === 'observed' ? '有文件' : '无文件'}</span></div>
                              <div className="mt-1 text-muted-foreground">{phase.artifact_count}{phase.count_truncated ? '+' : ''} 个文件</div>
                              {phase.start_time && <div className="mt-1 text-muted-foreground">{formatDateTime(phase.start_time)}{phase.end_time ? ` – ${formatDateTime(phase.end_time)}` : ' · 尚未记录结束'}</div>}
                              {phase.duration_seconds !== null && <div className="text-muted-foreground">耗时 {phase.duration_seconds.toFixed(1)} 秒 · {phase.time_source === 'time_info' ? '任务计时' : '文件时间估计'}</div>}
                            </div>
                          ))}
                        </div>
                        {loop.factors.length > 0 && (
                          <div className="mt-3 border-t border-border/40 pt-2 text-xs">
                            <div className="font-medium mb-1">本轮因子</div>
                            {loop.factors.map((factor) => (
                              <div key={factor.name} className="flex flex-wrap justify-between gap-2 py-1">
                                <span>{factor.name}</span>
                                <span className="text-muted-foreground">{factor.selected ? '进入编码' : '未进入编码'} · {factor.outcome || '结局未知'}</span>
                              </div>
                            ))}
                          </div>
                        )}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}
            <div>
              <div className="text-xs font-medium mb-1.5">日志（显示最近 200 行，共 {details.totalLines} 行）</div>
              <pre className="max-h-96 overflow-auto rounded-lg bg-slate-950 p-3 text-xs text-slate-100 whitespace-pre-wrap">{details.lines.join('\n') || '暂无日志或日志尚未生成。'}</pre>
            </div>
          </CardContent>
        </Card>
      )}
      {variantContext && <VariantLedgerDialog universe={variantContext.universe} initialContextId={variantContext.contextId} onClose={closeVariantLedger} />}
    </div>
  );
};
