import React, { useCallback, useEffect, useRef, useState } from 'react';
import { AlertCircle, BarChart3, CheckCircle2, Clock3, FileDown, Loader2, RefreshCw, Square, Trash2 } from 'lucide-react';
import { useAppSelector } from '../../../store';
import { getSessionGeneration, registerSessionReset } from '../../../store/sessionBoundary';
import { getFactorDetail, getFactors } from '../services-v2/api';
import {
  cancelFactorJob,
  deleteFactorJob,
  downloadFactorJobReport,
  listFactorJobs,
  submitFactorBatch,
  submitFactorJob,
  type FactorBacktestSeries,
  type FactorJob,
} from '../services-v2/factorJobsApi';
import type { Factor } from '../types-v2';
import { formatDateTime, formatNumber, formatPercent } from '../utils-v2';
import { useTaskContext } from '../context-v2/TaskContext';
import { Badge } from '../components-v2/ui/Badge';
import { Button } from '../components-v2/ui/Button';
import { Card, CardContent, CardHeader, CardTitle } from '../components-v2/ui/Card';

const UNIVERSES = [
  ['csi300', '沪深300'],
  ['csi500', '中证500'],
  ['csi1000', '中证1000'],
  ['sse50', '上证50'],
  ['gem', '创业板指'],
  ['star', '科创50'],
  ['csi800', '中证800'],
  ['all_a', '全部A股'],
] as const;

const ACTIVE = new Set(['queued', 'running', 'stopping']);
const FACTOR_PAGE_SIZE = 50;

function statusLabel(status: string): string {
  return ({
    queued: '排队中',
    running: '运行中',
    stopping: '停止中',
    completed: '已完成',
    failed: '失败',
    cancelled: '已取消',
  } as Record<string, string>)[status] ?? status;
}

function statusVariant(status: string): 'default' | 'success' | 'warning' | 'destructive' | 'outline' {
  if (status === 'completed') return 'success';
  if (status === 'failed') return 'destructive';
  if (status === 'queued' || status === 'stopping') return 'warning';
  return 'outline';
}

function metricLabel(value: unknown, percent = false): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—';
  return percent ? formatPercent(value) : formatNumber(value, 4);
}

/**
 * 取第一个有效指标。RD-Agent 功能因子产出 ic_value/rank_ic，
 * 统计派与批量任务产出 ic_mean/rank_ic_median；只认一种会让已完成的
 * 回测在界面上显示「—」。
 */
function pickMetric(metrics: Record<string, unknown>, keys: string[]): unknown {
  for (const key of keys) {
    const value = metrics?.[key];
    if (typeof value === 'number' && Number.isFinite(value)) return value;
  }
  return undefined;
}

export const BacktestPage: React.FC<{ initialFactorId?: string }> = ({ initialFactorId }) => {
  const { backendAvailable } = useTaskContext();
  const isAuthenticated = useAppSelector((state) => state.auth.isAuthenticated);
  const isAuthenticating = useAppSelector((state) => state.auth.isLoading);
  const [factors, setFactors] = useState<Factor[]>([]);
  const [factorSearch, setFactorSearch] = useState('');
  const [factorOffset, setFactorOffset] = useState(0);
  const [factorTotal, setFactorTotal] = useState(0);
  const [factorsLoading, setFactorsLoading] = useState(true);
  const [factorError, setFactorError] = useState('');
  const factorCache = useRef(new Map<string, Factor>());
  const factorController = useRef<AbortController | null>(null);
  const [jobs, setJobs] = useState<FactorJob[]>([]);
  const [factorId, setFactorId] = useState(initialFactorId || '');
  const [selectedFactor, setSelectedFactor] = useState<Factor | null>(null);
  const [batchMode, setBatchMode] = useState(false);
  const [batchFactorIds, setBatchFactorIds] = useState<string[]>([]);
  const factorIdRef = useRef(initialFactorId || '');
  const [jobKind, setJobKind] = useState<'backtest' | 'validate'>('backtest');
  const [universe, setUniverse] = useState('csi300');
  const [startDate, setStartDate] = useState('');
  const [endDate, setEndDate] = useState('');
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [cancelling, setCancelling] = useState('');
  const [deleting, setDeleting] = useState('');
  const [pendingDelete, setPendingDelete] = useState<FactorJob | null>(null);
  const [downloadingReport, setDownloadingReport] = useState('');
  const [error, setError] = useState('');
  const [sessionEpoch, setSessionEpoch] = useState(() => getSessionGeneration());
  const requestEpoch = useRef(0);
  const controllers = useRef(new Set<AbortController>());

  useEffect(() => {
    factorIdRef.current = factorId;
  }, [factorId]);

  useEffect(() => {
    if (!pendingDelete) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setPendingDelete(null);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [pendingDelete]);

  useEffect(() => registerSessionReset(() => {
    requestEpoch.current += 1;
    controllers.current.forEach((controller) => controller.abort());
    controllers.current.clear();
    factorController.current?.abort();
    factorCache.current.clear();
    factorIdRef.current = '';
    setFactors([]);
    setSelectedFactor(null);
    setFactorSearch('');
    setFactorOffset(0);
    setFactorTotal(0);
    setFactorsLoading(false);
    setFactorError('');
    setJobs([]);
    setFactorId('');
    setBatchFactorIds([]);
    setBatchMode(false);
    setJobKind('backtest');
    setDownloadingReport('');
    setDeleting('');
    setPendingDelete(null);
    setError('');
    setLoading(true);
    setSessionEpoch(getSessionGeneration());
  }), []);

  const loadFactors = useCallback(async () => {
    const generation = getSessionGeneration();
    factorController.current?.abort();
    const controller = new AbortController();
    factorController.current = controller;
    setFactorsLoading(true);
    try {
      const response = await getFactors({ limit: FACTOR_PAGE_SIZE, offset: factorOffset, search: factorSearch }, controller.signal);
      if (controller.signal.aborted || generation !== getSessionGeneration()) return;
      const rows = (response.data?.factors ?? []).filter((factor) => !factor.readOnly);
      rows.forEach((factor) => factorCache.current.set(factor.factorId, factor));
      setFactors(rows);
      setFactorTotal(response.data?.total ?? 0);
      setFactorError('');
      if (!factorIdRef.current && rows[0]) {
        factorIdRef.current = rows[0].factorId;
        setFactorId(rows[0].factorId);
      }
    } catch (cause) {
      if (!controller.signal.aborted && generation === getSessionGeneration()) {
        setFactorError(cause instanceof Error ? cause.message : '因子列表加载失败');
      }
    } finally {
      if (!controller.signal.aborted && generation === getSessionGeneration()) setFactorsLoading(false);
    }
  }, [factorSearch, factorOffset]);

  useEffect(() => {
    if (!isAuthenticated || isAuthenticating) return;
    const timer = window.setTimeout(() => void loadFactors(), 250);
    return () => { window.clearTimeout(timer); factorController.current?.abort(); };
  }, [isAuthenticated, isAuthenticating, sessionEpoch, loadFactors]);

  // A factor opened from another page may fall outside the current search page.
  useEffect(() => {
    setSelectedFactor(null);
    if (!isAuthenticated || isAuthenticating || !factorId) return;
    const cached = factorCache.current.get(factorId);
    if (cached) { setSelectedFactor(cached); return; }
    const generation = getSessionGeneration();
    const controller = new AbortController();
    controllers.current.add(controller);
    void getFactorDetail(factorId, controller.signal).then((response) => {
      if (controller.signal.aborted || generation !== getSessionGeneration()) return;
      const factor = response.data?.factor as Factor | undefined;
      if (!factor || factor.readOnly || factor.archivedAt) throw new Error('所选因子当前不可用于回测');
      factorCache.current.set(factor.factorId, factor);
      setSelectedFactor(factor);
    }).catch((cause) => {
      if (!controller.signal.aborted && generation === getSessionGeneration()) setFactorError(cause instanceof Error ? cause.message : '所选因子读取失败');
    }).finally(() => controllers.current.delete(controller));
    return () => { controller.abort(); controllers.current.delete(controller); };
  }, [factorId, isAuthenticated, isAuthenticating, sessionEpoch, factors]);

  const loadJobs = useCallback(async (signal?: AbortSignal) => {
    const generation = getSessionGeneration();
    const requestId = requestEpoch.current;
    try {
      const rows = await listFactorJobs(signal);
      if (generation !== getSessionGeneration() || requestId !== requestEpoch.current) return;
      setJobs(rows.filter((job) => job.kind === 'backtest' || job.kind === 'validate'));
      setError('');
    } catch (cause) {
      if (signal?.aborted) return;
      if (generation === getSessionGeneration() && requestId === requestEpoch.current) {
        setError(cause instanceof Error ? cause.message : '回测任务读取失败');
      }
    } finally {
      if (!signal?.aborted && generation === getSessionGeneration()) setLoading(false);
    }
  }, []);

  useEffect(() => {
    const activeControllers = controllers.current;
    if (!isAuthenticated || isAuthenticating) {
      setFactors([]);
      setJobs([]);
      setLoading(false);
      return;
    }
    const controller = new AbortController();
    controllers.current.add(controller);
    void loadJobs(controller.signal);
    const timer = window.setInterval(() => {
      const next = new AbortController();
      controllers.current.add(next);
      void loadJobs(next.signal).finally(() => controllers.current.delete(next));
    }, 5000);
    return () => {
      window.clearInterval(timer);
      activeControllers.forEach((requestController) => requestController.abort());
      activeControllers.clear();
    };
  }, [isAuthenticated, isAuthenticating, sessionEpoch, loadJobs]);
  const refresh = useCallback(() => {
    setLoading(true);
    void loadFactors();
    void loadJobs();
  }, [loadFactors, loadJobs]);

  const submit = async () => {
    const selectedFactorIds = batchMode ? batchFactorIds : [factorId];
    if (selectedFactorIds.some((id) => !id)) {
      setError('请先选择本人因子');
      return;
    }
    if (!batchMode && (!selectedFactor || selectedFactor.factorId !== factorId)) {
      setError('请等待所选因子读取成功，或重新选择本人因子');
      return;
    }
    if (batchMode && (selectedFactorIds.length < 2 || selectedFactorIds.length > 20)) {
      setError('批量运行需要选择 2 至 20 个本人因子');
      return;
    }
    if ((jobKind === 'validate' && (!startDate || !endDate)) || Boolean(startDate) !== Boolean(endDate) || (startDate && startDate > endDate)) {
      setError(jobKind === 'validate' ? '样本外验证必须填写完整日期区间' : '请填写完整且有效的回测日期区间');
      return;
    }
    setSubmitting(true);
    setError('');
    const generation = getSessionGeneration();
    try {
      const request = {
        kind: jobKind,
        universe,
        ...(startDate ? { start_date: startDate, end_date: endDate } : {}),
        data_source: 'qlib_bin',
      } as const;
      if (batchMode) {
        await submitFactorBatch({ ...request, factor_ids: selectedFactorIds });
      } else {
        await submitFactorJob({ ...request, factor_id: factorId });
      }
      if (generation !== getSessionGeneration()) return;
      await loadJobs();
    } catch (cause) {
      if (generation === getSessionGeneration()) {
        setError(cause instanceof Error ? cause.message : '回测任务提交失败');
      }
    } finally {
      if (generation === getSessionGeneration()) setSubmitting(false);
    }
  };

  const cancel = async (job: FactorJob) => {
    setCancelling(job.job_id);
    const generation = getSessionGeneration();
    try {
      await cancelFactorJob(job.job_id);
      if (generation !== getSessionGeneration()) return;
      await loadJobs();
    } catch (cause) {
      if (generation === getSessionGeneration()) {
        setError(cause instanceof Error ? cause.message : '取消请求失败');
      }
    } finally {
      if (generation === getSessionGeneration()) setCancelling('');
    }
  };

  const remove = async (job: FactorJob) => {
    const generation = getSessionGeneration();
    const controller = new AbortController();
    controllers.current.add(controller);
    setDeleting(job.job_id);
    try {
      await deleteFactorJob(job.job_id, controller.signal);
      if (!controller.signal.aborted && generation === getSessionGeneration()) {
        setJobs((prev) => prev.filter((item) => item.job_id !== job.job_id));
        setPendingDelete(null);
      }
    } catch (cause) {
      if (!controller.signal.aborted && generation === getSessionGeneration()) {
        setError(cause instanceof Error ? cause.message : '清除记录失败');
        // 关闭弹窗，错误统一走页面顶部提示（防重复提交也依赖它）。
        setPendingDelete(null);
      }
    } finally {
      controllers.current.delete(controller);
      if (generation === getSessionGeneration()) setDeleting('');
    }
  };

  const downloadReport = async (job: FactorJob) => {
    const generation = getSessionGeneration();
    const controller = new AbortController();
    controllers.current.add(controller);
    setDownloadingReport(job.job_id);
    try {
      const blob = await downloadFactorJobReport(job.job_id, controller.signal);
      if (generation !== getSessionGeneration()) return;
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = `factor-report-${job.job_id.slice(0, 8).replace(/[^a-zA-Z0-9-]/g, '')}.pdf`;
      anchor.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 30_000);
    } catch (cause) {
      if (!controller.signal.aborted && generation === getSessionGeneration()) {
        setError(cause instanceof Error ? cause.message : 'PDF 报告下载失败');
      }
    } finally {
      controllers.current.delete(controller);
      if (generation === getSessionGeneration()) setDownloadingReport('');
    }
  };

  return (
    <div className="space-y-4 animate-fade-in-up">
      <header>
        <h1 className="text-xl font-bold flex items-center gap-2">
          <BarChart3 className="h-5 w-5 text-primary" />
          回测中心
        </h1>
        <p className="text-xs text-muted-foreground mt-0.5">提交本人因子的 A 股日频回测或样本外验证，任务和结果只对当前账号可见。</p>
      </header>

      {backendAvailable === false && (
        <Card className="border-destructive/50">
          <CardContent className="p-4 flex items-center gap-3 text-destructive">
            <AlertCircle className="h-5 w-5" />后端服务暂不可用，请稍后刷新。
          </CardContent>
        </Card>
      )}
      {(error || factorError) && (
        <Card className="border-destructive/50">
          <CardContent className="p-4 flex items-start gap-3 text-destructive">
            <AlertCircle className="h-5 w-5 mt-0.5 shrink-0" />
            <span>{error || factorError}</span>
          </CardContent>
        </Card>
      )}

      <Card className="glass card-hover">
        <CardHeader className="pb-2">
          <CardTitle className="text-sm">创建回测 / 验证任务</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <label className="text-xs font-medium space-y-2">
              <span>运行类型</span>
              <select
                className="w-full rounded-lg border border-input bg-background px-3 py-2"
                value={jobKind}
                onChange={(event) => setJobKind(event.target.value as 'backtest' | 'validate')}
              >
                <option value="backtest">回测</option>
                <option value="validate">样本外验证</option>
              </select>
            </label>
            {!batchMode && <label className="text-xs font-medium space-y-2">
              <span>本人因子</span>
              <select
                className="w-full rounded-lg border border-input bg-background px-3 py-2"
                value={factorId}
                onChange={(event) => setFactorId(event.target.value)}
                disabled={submitting || (!factors.length && !factorId)}
              >
                {!factorId && <option value="">请选择本人因子</option>}
                {factorId && !factors.some((factor) => factor.factorId === factorId) && <option value={factorId}>{selectedFactor?.factorName || factorId}（当前页外已选）</option>}
                {factors.map((factor) => (
                  <option key={factor.factorId} value={factor.factorId}>
                    {factor.factorName} · {factor.factorId}
                  </option>
                ))}
              </select>
            </label>}
            <label className="text-xs font-medium space-y-2">
              <span>A 股股票池</span>
              <select
                className="w-full rounded-lg border border-input bg-background px-3 py-2"
                value={universe}
                onChange={(event) => setUniverse(event.target.value)}
              >
                {UNIVERSES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
              </select>
            </label>
            <label className="text-xs font-medium space-y-2">
              <span>开始日期（{jobKind === 'validate' ? '必填' : '可留空'}）</span>
              <input
                className="w-full rounded-lg border border-input bg-background px-3 py-2"
                type="date"
                value={startDate}
                onChange={(event) => setStartDate(event.target.value)}
              />
            </label>
            <label className="text-xs font-medium space-y-2">
              <span>结束日期（{jobKind === 'validate' ? '必填' : '可留空'}）</span>
              <input
                className="w-full rounded-lg border border-input bg-background px-3 py-2"
                type="date"
                min={startDate || undefined}
                value={endDate}
                onChange={(event) => setEndDate(event.target.value)}
              />
            </label>
            <label className="text-xs font-medium space-y-2">
              <span>数据适配</span>
              <p className="text-xs text-muted-foreground py-2">自动匹配：Qlib 类因子使用 Qlib 日线，函数因子使用兼容的 Parquet 面板。</p>
            </label>
          </div>
          <div className="flex flex-wrap items-center gap-3">
            <input
              aria-label="搜索本人因子"
              placeholder="搜索因子名称、表达式或描述"
              maxLength={200}
              className="min-w-56 flex-1 rounded-lg border border-input bg-background px-3 py-2 text-xs"
              value={factorSearch}
              onChange={(event) => { setFactorSearch(event.target.value); setFactorOffset(0); }}
            />
            <span className="text-xs text-muted-foreground">共 {factorTotal} 个 · 第 {Math.floor(factorOffset / FACTOR_PAGE_SIZE) + 1} 页{factorsLoading ? ' · 加载中' : ''}</span>
            <Button variant="outline" size="sm" disabled={factorOffset === 0 || factorsLoading} onClick={() => setFactorOffset(Math.max(0, factorOffset - FACTOR_PAGE_SIZE))}>上一页</Button>
            <Button variant="outline" size="sm" disabled={factorOffset + FACTOR_PAGE_SIZE >= factorTotal || factorsLoading} onClick={() => setFactorOffset(factorOffset + FACTOR_PAGE_SIZE)}>下一页</Button>
          </div>
          <label className="flex items-center gap-2 text-xs">
            <input
              type="checkbox"
              checked={batchMode}
              onChange={(event) => {
                setBatchMode(event.target.checked);
                if (!event.target.checked) setBatchFactorIds([]);
                else if (factorId) setBatchFactorIds([factorId]);
              }}
            />
            批量运行（顺序执行 2–20 个因子，共用一个任务槽）
          </label>
          {batchMode && (
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2 max-h-56 overflow-y-auto rounded-lg border border-border/70 p-3">
              {factors.map((factor) => {
                const checked = batchFactorIds.includes(factor.factorId);
                return (
                  <label key={factor.factorId} className="flex items-center gap-2 text-xs min-w-0">
                    <input
                      type="checkbox"
                      checked={checked}
                      disabled={!checked && batchFactorIds.length >= 20}
                      onChange={(event) => setBatchFactorIds(event.target.checked
                        ? [...batchFactorIds, factor.factorId]
                        : batchFactorIds.filter((id) => id !== factor.factorId))}
                    />
                    <span className="truncate" title={`${factor.factorName} · ${factor.factorId}`}>
                      {factor.factorName} · {factor.factorId}
                    </span>
                  </label>
                );
              })}
            </div>
          )}
          {batchMode && <div className="space-y-2">
            <p className="text-xs text-muted-foreground">已跨页选中 {batchFactorIds.length} 个。批量任务按选择顺序执行；任一因子失败会使整批失败，当前不会保存部分结果。</p>
            <div className="flex flex-wrap gap-2">{batchFactorIds.map((id) => <button key={id} type="button" className="rounded border px-2 py-1 text-xs" title={`移除 ${id}`} onClick={() => setBatchFactorIds((ids) => ids.filter((value) => value !== id))}>{factorCache.current.get(id)?.factorName || id} ×</button>)}</div>
          </div>}
          {!factorsLoading && factors.length === 0 && <p className="text-xs text-muted-foreground">当前搜索没有匹配因子；已选因子保留，可修改关键词或清空搜索。</p>}
          {selectedFactor && (
            <p className="text-xs text-muted-foreground">
              {selectedFactor.factorExpression ? `表达式：${selectedFactor.factorExpression.slice(0, 180)}` : '回测执行时从本人因子记录读取代码快照。'}
            </p>
          )}
          <div className="flex flex-wrap items-center gap-3">
            <Button
              variant="primary"
              onClick={submit}
              disabled={!isAuthenticated || backendAvailable === false || submitting || (batchMode ? batchFactorIds.length < 2 : !selectedFactor || selectedFactor.factorId !== factorId)}
            >
              {submitting ? <Loader2 className="h-4 w-4 mr-2 animate-spin" /> : null}
              提交{batchMode ? '批量' : ''}{jobKind === 'validate' ? '样本外验证' : '回测'}
            </Button>
            <Button variant="outline" size="sm" className="text-xs" onClick={refresh} disabled={loading}>
              <RefreshCw className={`h-4 w-4 mr-2 ${loading ? 'animate-spin' : ''}`} />刷新
            </Button>
            <span className="text-xs text-muted-foreground">当前执行槽已占用时，可查看状态；页面不在浏览器本地保存任务详情。</span>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm flex items-center gap-2"><Clock3 className="h-5 w-5" />我的回测与验证任务</CardTitle>
        </CardHeader>
        <CardContent>
          {loading && jobs.length === 0 ? (
            <div className="flex items-center gap-2 py-8 text-muted-foreground"><Loader2 className="h-4 w-4 animate-spin" />读取任务中</div>
          ) : jobs.length === 0 ? (
            <div className="py-8 text-center text-muted-foreground">还没有回测任务</div>
          ) : (
            <div className="space-y-3">
              {jobs.map((job) => {
                const metrics = job.result_snapshot?.metrics ?? {};
                const active = ACTIVE.has(job.status);
                const name = factors.find((item) => item.factorId === job.request.factor_id)?.factorName;
                const batchResults = job.result_snapshot?.results ?? [];
                const progress = job.progress_snapshot;
                return (
                  <article key={job.job_id} className="rounded-lg border border-border/70 p-4 space-y-3">
                    <div className="flex flex-wrap items-center justify-between gap-3">
                      <div className="min-w-0">
                        <div className="font-semibold truncate">{job.kind === 'validate' ? '样本外验证 · ' : '回测 · '}{job.request.factor_ids?.length ? `批量 ${job.request.factor_ids.length} 个因子` : name || job.request.factor_id || '本人因子'}</div>
                        <div className="text-xs text-muted-foreground mt-1">
                          {job.job_id} · {UNIVERSES.find(([value]) => value === job.request.universe)?.[1] ?? job.request.universe ?? 'A 股'}
                          {' · '}{job.created_at ? formatDateTime(job.created_at) : '提交时间未知'}
                        </div>
                      </div>
                      <div className="flex items-center gap-2">
                        <Badge variant={statusVariant(job.status)}>{statusLabel(job.status)}</Badge>
                        {job.status === 'completed' && (
                          <Button variant="outline" size="sm" onClick={() => void downloadReport(job)} disabled={downloadingReport === job.job_id}>
                            {downloadingReport === job.job_id ? <Loader2 className="h-4 w-4 animate-spin" /> : <FileDown className="h-4 w-4" />}
                            导出 PDF
                          </Button>
                        )}
                        {active && (
                          <Button variant="destructive" size="sm" onClick={() => cancel(job)} disabled={cancelling === job.job_id || job.status === 'stopping'}>
                            {cancelling === job.job_id ? <Loader2 className="h-4 w-4 animate-spin" /> : <Square className="h-3.5 w-3.5 mr-1" />}
                            {job.status === 'stopping' ? '等待停止' : '取消'}
                          </Button>
                        )}
                        {!active && (
                          <Button
                            variant="outline"
                            size="sm"
                            onClick={() => setPendingDelete(job)}
                            disabled={deleting === job.job_id}
                            title="从记录中清除（同时删除服务端数据）"
                          >
                            {deleting === job.job_id ? <Loader2 className="h-4 w-4 animate-spin" /> : <Trash2 className="h-3.5 w-3.5 mr-1" />}
                            清除
                          </Button>
                        )}
                      </div>
                    </div>
                    {active && (
                      <div className="text-xs text-muted-foreground">
                        {typeof progress?.stage === 'string' ? progress.stage : statusLabel(job.status)}
                        {typeof progress?.progress_pct === 'number' ? ` · ${progress.progress_pct}%` : ''}
                      </div>
                    )}
                    {job.status === 'completed' && (
                      <div className="grid grid-cols-2 md:grid-cols-5 gap-3 text-xs">
                        <Metric label="平均 IC" value={metricLabel(pickMetric(metrics, ['ic_mean', 'ic_value']))} />
                        <Metric label="Rank IC 中位数" value={metricLabel(pickMetric(metrics, ['rank_ic_median', 'rank_ic']))} />
                        <Metric label="换手后 Rank IC" value={metricLabel(metrics.net_rank_ic_mean)} />
                        <Metric label="年化收益" value={metricLabel(metrics.annual_return, true)} />
                        <Metric label="最大回撤" value={metricLabel(metrics.max_drawdown, true)} />
                      </div>
                    )}
                    {job.status === 'completed' && job.kind === 'validate' && job.result_snapshot?.validation && (
                      <div className="text-xs text-muted-foreground">
                        训练区间 {job.result_snapshot.validation.training_window?.start_date} 至 {job.result_snapshot.validation.training_window?.end_date}
                        {' · '}验证区间 {job.result_snapshot.validation.validation_window?.start_date} 至 {job.result_snapshot.validation.validation_window?.end_date}
                        {' · '}训练与验证区间不重叠
                      </div>
                    )}
                    {job.status === 'completed' && job.result_snapshot?.series && (
                      <div className="space-y-2">
                        <FactorSeriesCharts series={job.result_snapshot.series} />
                        <p className="text-[11px] text-muted-foreground">
                          等权因子前 30% 次日净收益；冻结双边成本率 {percentLabel(job.result_snapshot.series.cost_rate ?? job.request.cost_model?.round_trip_cost)}，按新进多头持仓换手扣费。此组合估算未模拟最低佣金、个股涨跌停和逐笔撮合。
                        </p>
                      </div>
                    )}
                    {job.status === 'completed' && batchResults.length > 0 && (
                      <div className="space-y-2">
                        {batchResults.map((item) => {
                          const factor = factors.find((candidate) => candidate.factorId === item.factor_id);
                          return (
                            <div key={item.factor_id} className="space-y-2">
                              <div className="flex flex-wrap items-center justify-between gap-2 rounded-md bg-secondary/30 px-3 py-2 text-xs">
                                <span className="truncate">{factor?.factorName ?? item.factor_id}</span>
                                <span className="text-xs text-muted-foreground">IC {metricLabel(pickMetric(item.metrics ?? {}, ['ic_mean', 'ic_value']))} · Rank IC {metricLabel(pickMetric(item.metrics ?? {}, ['rank_ic_median', 'rank_ic']))}</span>
                              </div>
                              {item.series && (
                                <details className="rounded-md border border-border/50 px-3 py-2">
                                  <summary className="cursor-pointer text-xs text-muted-foreground">查看收益与回撤曲线 · {factor?.factorName ?? item.factor_id}</summary>
                                  <div className="mt-2"><FactorSeriesCharts series={item.series} /></div>
                                  <p className="text-[11px] text-muted-foreground mt-2">
                                    净收益口径；冻结双边成本率 {percentLabel(item.series.cost_rate ?? job.request.cost_model?.round_trip_cost)}，按新进多头持仓换手扣费，未模拟最低佣金、个股涨跌停和逐笔撮合。
                                  </p>
                                </details>
                              )}
                            </div>
                          );
                        })}
                      </div>
                    )}
                    {job.status === 'completed' && <div className="text-xs text-emerald-500 flex items-center gap-1"><CheckCircle2 className="h-3.5 w-3.5" />结果已保存到本人任务记录</div>}
                    {job.status === 'failed' && job.error_message && <p className="text-xs text-destructive">{job.error_message}</p>}
                  </article>
                );
              })}
            </div>
          )}
        </CardContent>
      </Card>
      {pendingDelete && (
        <div
          role="dialog"
          aria-modal="true"
          aria-label="清除回测记录"
          className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/50 p-4"
          onClick={(event) => {
            if (event.target === event.currentTarget && !deleting) setPendingDelete(null);
          }}
        >
          <div className="w-full max-w-md rounded-2xl border border-border bg-background p-5 shadow-xl space-y-4">
            <h3 className="font-semibold">清除记录</h3>
            <p className="text-sm">
              {pendingDelete.kind === 'validate' ? '样本外验证' : '回测'} ·{' '}
              {factors.find((item) => item.factorId === pendingDelete.request.factor_id)?.factorName
                || pendingDelete.request.factor_id
                || '本人因子'}
            </p>
            <p className="text-xs text-muted-foreground break-all">
              {pendingDelete.job_id} · {pendingDelete.created_at ? formatDateTime(pendingDelete.created_at) : '提交时间未知'}
            </p>
            <p className="text-sm text-destructive">
              这条记录与它的回测结果会一并从服务端删除，无法恢复。
            </p>
            <div className="flex justify-end gap-2">
              <Button variant="outline" size="sm" disabled={Boolean(deleting)} onClick={() => setPendingDelete(null)}>
                取消
              </Button>
              <Button variant="destructive" size="sm" disabled={Boolean(deleting)} onClick={() => void remove(pendingDelete)}>
                {deleting === pendingDelete.job_id ? <Loader2 className="h-4 w-4 animate-spin mr-1" /> : <Trash2 className="h-3.5 w-3.5 mr-1" />}
                确认清除
              </Button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md bg-secondary/40 px-3 py-2">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="font-mono font-semibold mt-1">{value}</div>
    </div>
  );
}

function percentLabel(value: number | undefined) {
  return typeof value === 'number' && Number.isFinite(value)
    ? `${(value * 100).toFixed(3)}%`
    : '未知';
}

function FactorSeriesCharts({ series }: { series: FactorBacktestSeries }) {
  const dates = series.dates ?? [];
  if (dates.length > 0) {
    const quantiles = Object.entries(series.q_curves ?? {})
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, values], index) => ({
        key,
        label: `分位 ${key.slice(1)}`,
        color: ['#94a3b8', '#60a5fa', '#a78bfa', '#f59e0b', '#34d399'][index % 5],
        values,
      }));
    return (
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        <MultiSeriesChart label="累计净值 · 多头 / 基准" dates={dates} lines={[
          { key: 'net', label: '多头净值（扣费）', color: '#38bdf8', values: series.nav_long },
          { key: 'gross', label: '多头净值（毛）', color: '#a78bfa', values: series.nav_long_gross },
          { key: 'bench', label: '等权基准', color: '#f59e0b', values: series.nav_bench },
        ]} />
        <MultiSeriesChart label="相对基准超额（%）" dates={dates} lines={[
          { key: 'excess', label: '超额收益', color: '#34d399', values: series.nav_excess_pct },
        ]} />
        <MultiSeriesChart label="日度 IC" dates={dates} lines={[
          { key: 'ic', label: 'IC', color: '#38bdf8', values: series.ic },
          { key: 'rank_ic', label: 'Rank IC', color: '#f97316', values: series.rank_ic },
        ]} />
        <MultiSeriesChart label="分位组合净值（毛）" dates={dates} lines={quantiles} />
        <MultiSeriesChart label="新进多头持仓比例" dates={dates} lines={[
          { key: 'turnover', label: '新进持仓占比', color: '#f97316', values: series.turnover },
        ]} />
        <MultiSeriesChart label="多头净值回撤" dates={dates} lines={[
          { key: 'drawdown', label: '回撤', color: '#f97316', values: series.drawdown },
        ]} />
      </div>
    );
  }
  return (
    <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
      <CurveChart label="累计净值（初始值 1）" values={series.equity_curve ?? []} color="#38bdf8" />
      <CurveChart label="回撤" values={series.drawdown_curve ?? []} color="#f97316" />
    </div>
  );
}

interface FactorSeriesLine {
  key: string;
  label: string;
  color: string;
  values?: Array<number | null>;
}

function MultiSeriesChart({
  label,
  dates,
  lines,
}: {
  label: string;
  dates: string[];
  lines: FactorSeriesLine[];
}) {
  const active = lines.filter((line) => line.values?.some((value) => typeof value === 'number' && Number.isFinite(value)));
  const points = active.flatMap((line) => (line.values ?? []).filter((value): value is number => typeof value === 'number' && Number.isFinite(value)));
  if (dates.length < 2 || points.length === 0) return null;
  const low = Math.min(...points);
  const high = Math.max(...points);
  const span = high - low || 1;
  return (
    <div className="rounded-md border border-border/50 bg-secondary/20 p-2">
      <div className="text-xs text-muted-foreground mb-1">{label}</div>
      <svg viewBox="0 0 360 96" role="img" aria-label={label} className="w-full h-24">
        {active.map((line) => {
          const coordinates = (line.values ?? []).map((value, index) => {
            if (typeof value !== 'number' || !Number.isFinite(value)) return null;
            const x = (index / Math.max(1, dates.length - 1)) * 360;
            const y = 76 - ((value - low) / span) * 64;
            return `${x.toFixed(1)},${y.toFixed(1)}`;
          }).filter((coordinate): coordinate is string => coordinate !== null).join(' ');
          return <polyline key={line.key} points={coordinates} fill="none" stroke={line.color} strokeWidth="2" vectorEffect="non-scaling-stroke" />;
        })}
      </svg>
      <div className="flex flex-wrap gap-x-3 gap-y-1 text-[10px] text-muted-foreground">
        {active.map((line) => <span key={line.key} className="inline-flex items-center gap-1"><i className="inline-block h-2 w-2 rounded-full" style={{ backgroundColor: line.color }} />{line.label}</span>)}
      </div>
      <div className="flex justify-between text-[10px] text-muted-foreground mt-1"><span>{dates[0]}</span><span>{dates[dates.length - 1]}</span></div>
    </div>
  );
}

function CurveChart({
  label,
  values,
  color,
}: {
  label: string;
  values: FactorBacktestSeries['equity_curve'];
  color: string;
}) {
  const valid = (values ?? []).filter((point) => Number.isFinite(point.value));
  if (valid.length < 2) return null;
  const low = Math.min(...valid.map((point) => point.value));
  const high = Math.max(...valid.map((point) => point.value));
  const span = high - low || 1;
  const points = valid.map((point, index) => {
    const x = (index / (valid.length - 1)) * 360;
    const y = 72 - ((point.value - low) / span) * 64;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(' ');
  return (
    <div className="rounded-md border border-border/50 bg-secondary/20 p-2">
      <div className="text-xs text-muted-foreground mb-1">{label}</div>
      <svg viewBox="0 0 360 80" role="img" aria-label={label} className="w-full h-20">
        <polyline points={points} fill="none" stroke={color} strokeWidth="2" vectorEffect="non-scaling-stroke" />
      </svg>
      <div className="flex justify-between text-[10px] text-muted-foreground">
        <span>{valid[0].date}</span><span>{valid[valid.length - 1].date}</span>
      </div>
    </div>
  );
}
