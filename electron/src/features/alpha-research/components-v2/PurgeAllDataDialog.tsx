import React, { useCallback, useEffect, useRef, useState } from 'react';
import { AlertTriangle, Loader2, X } from 'lucide-react';
import { getSessionGeneration, registerSessionReset } from '../../../store/sessionBoundary';
import { generateId } from '../utils-v2';
import {
  getOwnerPurge,
  previewOwnerPurge,
  submitOwnerPurge,
  type PurgePreview,
} from '../services-v2/purgeApi';
import type { FactorJob } from '../services-v2/factorJobsApi';
import { Button } from './ui/Button';

const COUNT_LABELS: Record<string, string> = {
  tasks: '挖掘任务',
  task_specs: '任务规格',
  task_results: '任务结果',
  mining_request_keys: '任务请求键',
  factors: '因子',
  factor_revisions: '因子版本',
  factor_pool: '因子池成员',
  factor_edges: '因子关系',
  factor_jobs: '因子作业',
  factor_job_outbox: '作业 outbox',
  factor_job_request_keys: '作业请求键',
  factor_validation_attempts: '验证记录',
  factor_validation_latest: '最新验证',
  factor_materializations: '物化记录',
  factor_recovery_sources: '补码来源',
  factor_visibility_events: '可见性事件',
  factor_lifecycle_events: '生命周期事件',
  factor_generations: '物化版本',
  factor_generation_features: '物化特征',
  factor_dataset_active: '激活数据集',
  factor_combos: '因子组合',
  factor_combo_members: '组合成员',
  artifact_cleanups: '产物清理记录',
  docs: '研究文档',
  direction_batches: '方向批次',
  research_requests: '研究请求',
  research_llm_calls: '研究模型调用',
  variant_contexts: '变体上下文',
  variant_observations: '变体观察',
  variant_states: '变体状态',
  variant_reset_events: '变体重置',
  node_assignments: '节点分配',
  leases: '执行租约',
};

const BLOCKER_LABELS: Record<string, string> = {
  mining_tasks: '活动挖掘任务',
  mining_task: '活动挖掘任务',
  factor_jobs: '活动因子作业',
  purge_jobs: '正在运行的清理作业',
  research_requests: '活动研究请求',
  leases: '未结束的执行租约',
  combos: '被组合引用的因子',
  published_generations: '已发布的训练版本',
};

const STORAGE_LABELS: Record<string, string> = {
  task_workspaces: '任务工作区',
  task_workspace: '任务工作区',
  docs: '研究文档',
  exports: '因子导出',
  factor_generations: '物化特征版本',
  factor_exports: '因子导出文件',
  pool_panels: '因子池面板',
};

function storageLabel(key: string): string {
  if (key.startsWith('generation:')) return `物化版本 ${key.slice(11, 19)}…`;
  return STORAGE_LABELS[key] ?? key;
}

const ACTIVE = new Set(['queued', 'running', 'stopping']);

function formatBytes(value: number): string {
  if (value >= 1024 ** 3) return `${(value / 1024 ** 3).toFixed(2)} GiB`;
  if (value >= 1024 ** 2) return `${(value / 1024 ** 2).toFixed(2)} MiB`;
  if (value >= 1024) return `${(value / 1024).toFixed(1)} KiB`;
  return `${value} B`;
}

function nonZero(values: Record<string, number>): Array<[string, number]> {
  return Object.entries(values).filter(([, count]) => count > 0);
}

export const PurgeAllDataDialog: React.FC<{
  onClose: () => void;
  onSubmitted: () => void;
  taskId?: string;
}> = ({ onClose, onSubmitted, taskId }) => {
  const [preview, setPreview] = useState<PurgePreview | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [jobId, setJobId] = useState('');
  const [job, setJob] = useState<FactorJob | null>(null);
  const action = useRef<AbortController | null>(null);
  const requestKey = useRef('');
  const dialogTitle = taskId ? `彻底删除任务 ${taskId.slice(0, 8)}…` : '清空本人全部挖掘数据';
  const dialogDesc = taskId
    ? '删除该挖掘任务及其专属因子、版本、验证记录、物化版本、引用作业与任务产物。组合、已发布训练版本与跨任务记忆保留，不可恢复。'
    : '删除挖掘任务、因子与版本、因子池、因子作业、研究文档、方向批次、研究请求、变体记忆、物化特征和任务产物。不可恢复。';

  const loadPreview = useCallback(() => {
    const controller = new AbortController();
    action.current?.abort();
    action.current = controller;
    const generation = getSessionGeneration();
    setLoading(true);
    setError('');
    previewOwnerPurge(controller.signal, taskId)
      .then((result) => {
        if (!controller.signal.aborted && generation === getSessionGeneration()) {
          setPreview(result);
          requestKey.current = globalThis.crypto?.randomUUID?.() ?? generateId();
        }
      })
      .catch((failure: unknown) => {
        if (!controller.signal.aborted && generation === getSessionGeneration()) {
          setError(failure instanceof Error ? failure.message : '清理预览失败');
        }
      })
      .finally(() => {
        if (!controller.signal.aborted && generation === getSessionGeneration()) {
          setLoading(false);
        }
      });
  }, [taskId]);

  useEffect(() => {
    loadPreview();
    return () => action.current?.abort();
  }, [loadPreview]);

  useEffect(() => registerSessionReset(() => {
    action.current?.abort();
    setPreview(null);
    setJob(null);
    setJobId('');
    onClose();
  }), [onClose]);

  useEffect(() => {
    if (!jobId || !job || !ACTIVE.has(job.status)) return;
    const generation = getSessionGeneration();
    const timer = window.setInterval(() => {
      getOwnerPurge(jobId)
        .then((next) => {
          if (generation !== getSessionGeneration()) return;
          setJob(next);
          if (!ACTIVE.has(next.status)) onSubmitted();
        })
        .catch(() => {
          // The dialog keeps the last known state; explicit retry is available.
        });
    }, 2000);
    return () => window.clearInterval(timer);
  }, [jobId, job, onSubmitted]);

  const submit = async () => {
    if (
      !preview
      || submitting
      || (preview.blockers && nonZero(preview.blockers).length > 0)
    ) {
      return;
    }
    const controller = new AbortController();
    action.current?.abort();
    action.current = controller;
    const generation = getSessionGeneration();
    setSubmitting(true);
    setError('');
    try {
      const result = await submitOwnerPurge(
        preview.preview_token,
        requestKey.current,
        controller.signal,
        taskId,
      );
      if (!controller.signal.aborted && generation === getSessionGeneration()) {
        setJobId(result.job_id);
        const current = await getOwnerPurge(result.job_id, controller.signal);
        setJob(current);
        if (!ACTIVE.has(current.status)) onSubmitted();
      }
    } catch (failure: unknown) {
      if (!controller.signal.aborted && generation === getSessionGeneration()) {
        setError(failure instanceof Error ? failure.message : '清理提交失败');
      }
    } finally {
      if (!controller.signal.aborted && generation === getSessionGeneration()) {
        setSubmitting(false);
      }
    }
  };

  const blockers = preview ? nonZero(preview.blockers) : [];
  const counts = preview ? nonZero(preview.counts) : [];
  const storage = preview
    ? Object.entries(preview.storage.items).filter(([, item]) => item.bytes > 0 || item.exists)
    : [];
  const terminal = job ? !ACTIVE.has(job.status) : false;
  const result = (job?.result_snapshot ?? {}) as {
    deleted?: Record<string, number>;
    storage?: { removed_bytes?: number };
    quota?: { bytes?: number };
  };
  const deleted = Object.entries(result.deleted ?? {}).filter(([, value]) => Number(value) > 0);

  return (
    <div role="dialog" aria-modal="true" aria-label={dialogTitle} className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/50 p-4">
      <div className="max-h-[90vh] w-full max-w-2xl overflow-y-auto rounded-2xl border border-destructive/40 bg-background p-5 shadow-xl space-y-4">
        <div className="flex items-start justify-between gap-3">
          <div>
            <h3 className="flex items-center gap-2 font-semibold text-destructive">
              <AlertTriangle className="h-4 w-4" />{dialogTitle}
            </h3>
            <p className="mt-1 text-xs text-muted-foreground">
              {dialogDesc}
            </p>
          </div>
          <button type="button" aria-label="关闭" onClick={onClose}><X className="h-4 w-4" /></button>
        </div>

        {loading && <div className="flex items-center gap-2 text-sm"><Loader2 className="h-4 w-4 animate-spin" />正在统计将删除的数据</div>}
        {error && <p className="text-sm text-destructive">{error}</p>}

        {preview && (
          <>
            {blockers.length > 0 ? (
              <div className="rounded-lg border border-amber-500/40 bg-amber-500/10 p-3 text-sm">
                <p className="font-medium">仍有活动任务，暂不能清理：</p>
                <ul className="mt-1 list-inside list-disc text-xs">
                  {blockers.map(([key, value]) => <li key={key}>{BLOCKER_LABELS[key] ?? key}：{value}</li>)}
                </ul>
                <p className="mt-1 text-xs text-muted-foreground">请先在任务中心停止或等待这些任务结束，然后重新预览。</p>
              </div>
            ) : (
              <div className="grid gap-3 text-sm sm:grid-cols-2">
                <div className="rounded-lg border border-border/60 p-3">
                  <div className="mb-1 text-xs font-medium text-muted-foreground">数据库记录</div>
                  <div className="space-y-0.5 text-xs">
                    {counts.length === 0 ? <span>没有可删除的数据</span> : counts.map(([key, value]) => (
                      <div key={key} className="flex justify-between gap-2"><span>{COUNT_LABELS[key] ?? key}</span><span className="font-mono">{value}</span></div>
                    ))}
                  </div>
                </div>
                <div className="rounded-lg border border-border/60 p-3">
                  <div className="mb-1 text-xs font-medium text-muted-foreground">磁盘产物</div>
                  <div className="space-y-0.5 text-xs">
                    {storage.length === 0 ? <span>没有可删除的产物</span> : storage.map(([key, item]) => (
                      <div key={key} className="flex justify-between gap-2"><span>{storageLabel(key)}</span><span className="font-mono">{formatBytes(item.bytes)}</span></div>
                    ))}
                    <div className="mt-1 flex justify-between gap-2 border-t border-border/40 pt-1 font-medium"><span>合计</span><span className="font-mono">{formatBytes(preview.total_bytes)}</span></div>
                  </div>
                </div>
              </div>
            )}

            {!terminal && blockers.length === 0 && (
              <div className="flex justify-end gap-2">
                <Button variant="outline" onClick={loadPreview} disabled={submitting || Boolean(jobId)}>重新预览</Button>
                <Button
                  variant="destructive"
                  onClick={() => void submit()}
                  disabled={submitting || Boolean(jobId)}
                >
                  {submitting || jobId && !terminal ? <><Loader2 className="mr-1 h-4 w-4 animate-spin" />清理中…</> : '确认清空'}
                </Button>
              </div>
            )}

            {job && (
              <div className="rounded-lg border border-border/60 p-3 text-sm">
                <div className="flex items-center justify-between gap-2">
                  <span>清理作业</span>
                  <span className="font-mono text-xs">{job.status}</span>
                </div>
                {job.progress_snapshot && (
                  <div className="mt-1 text-xs text-muted-foreground">
                    {String(job.progress_snapshot.progress ?? job.progress_snapshot.stage ?? '')}
                    {typeof job.progress_snapshot.progress_pct === 'number' ? ` · ${job.progress_snapshot.progress_pct}%` : ''}
                  </div>
                )}
                {job.status === 'failed' && job.error_message && <p className="mt-1 text-xs text-destructive">{job.error_message}</p>}
                {job.status === 'completed' && (
                  <div className="mt-2 space-y-1 text-xs">
                    <p>已删除 {deleted.reduce((sum, [, value]) => sum + Number(value), 0)} 条记录，释放 {formatBytes(Number(result.storage?.removed_bytes ?? 0))} 文件。</p>
                    {deleted.length > 0 && <div className="text-muted-foreground">{deleted.map(([key, value]) => `${COUNT_LABELS[key] ?? key} ${value}`).join(' · ')}</div>}
                  </div>
                )}
                {terminal && <div className="mt-2 flex justify-end"><Button variant="outline" onClick={onClose}>关闭</Button></div>}
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
};
