import { apiClient } from '../../services/api-client';
import { TASK_KIND_META, type BackgroundTask, type ServerLoad, type TaskStatus } from './types';

/** 归一化状态字符串到任务状态；未知一律按 queued 处理，避免误报 running。 */
function normalizeStatus(raw: unknown): TaskStatus {
  const s = String(raw ?? '').toLowerCase();
  if (
    ['running', 'in_progress', 'processing', 'active', 'provisioning', 'waiting_callback', 'cancelling'].includes(s)
  ) {
    return 'running';
  }
  if (['pending', 'queued', 'waiting', 'created'].includes(s)) return 'queued';
  if (['failed', 'error'].includes(s)) return 'failed';
  return 'done';
}

function clampPct(value: unknown): number | null {
  const num = Number(value);
  if (!Number.isFinite(num)) return null;
  return Math.max(0, Math.min(100, num));
}

/** 只保留仍在跑的任务（已完成/失败不进坞，避免长期占位）。 */
function keepActive(tasks: BackgroundTask[]): BackgroundTask[] {
  return tasks.filter((t) => t.status === 'running' || t.status === 'queued');
}

async function fetchTraining(): Promise<BackgroundTask[]> {
  try {
    const { data } = await apiClient.get<any>('/api/v1/models/training-runs/active');
    if (!data) return [];
    const status = normalizeStatus(data.status);
    if (status === 'done' || status === 'failed') return [];
    const runId = String(data.runId ?? data.run_id ?? '');
    if (!runId) return [];
    return [
      {
        id: runId,
        kind: 'training',
        title: String(data.displayName ?? data.display_name ?? data.jobName ?? '模型训练'),
        status,
        progress: clampPct(data.progress),
        startedAt: data.createdAt ?? data.created_at ?? null,
        href: TASK_KIND_META.training.href,
        detail: String(data.modelType ?? data.model_type ?? ''),
      },
    ];
  } catch {
    return [];
  }
}

async function fetchInference(): Promise<BackgroundTask[]> {
  try {
    const { data } = await apiClient.get<any>('/api/v1/models/inference/backfill-jobs');
    const rows: any[] = Array.isArray(data)
      ? data
      : data?.items ?? data?.jobs ?? data?.data ?? [];
    return keepActive(
      rows.map((row) => {
        // 服务端 job_payload 把汇总放在 totals 里（models/days/completed_days），
        // 顶层字段是历史兼容；两处都读，避免进度恒为 null。
        const totals = row.totals ?? {};
        const total = Number(
          row.total_days ?? row.totalDays ?? totals.days ?? 0,
        );
        const done = Number(
          row.completed_days ?? row.completedDays ?? totals.completed_days ?? 0,
        );
        const models = Number(
          row.total_models ?? row.totalModels ?? totals.models ?? 1,
        );
        return {
          id: String(row.job_id ?? row.jobId ?? ''),
          kind: 'inference',
          title: `推理补推 · ${models} 个模型`,
          status: normalizeStatus(row.status),
          progress: total > 0 ? clampPct((done / total) * 100) : null,
          startedAt: row.created_at ?? row.createdAt ?? null,
          href: TASK_KIND_META.inference.href,
          detail: done && total ? `${done}/${total} 日` : undefined,
        } satisfies BackgroundTask;
      }),
    );
  } catch {
    return [];
  }
}

async function fetchMining(): Promise<BackgroundTask[]> {
  try {
    const { data } = await apiClient.get<any>('/api/v1/alpha-agent/tasks');
    const rows: any[] = Array.isArray(data) ? data : data?.data?.tasks ?? data?.tasks ?? [];
    return keepActive(
      rows.map((row) => {
        // progress 可能是 "3/10" 这类字符串，尽量解析成百分比
        let pct: number | null = null;
        const raw = row.progress;
        if (typeof raw === 'number') pct = clampPct(raw);
        else if (typeof raw === 'string') {
          const m = raw.match(/(\d+(?:\.\d+)?)\s*\/\s*(\d+(?:\.\d+)?)/);
          if (m) pct = clampPct((Number(m[1]) / Number(m[2])) * 100);
        }
        return {
          id: String(row.task_id ?? row.taskId ?? ''),
          kind: 'mining',
          title: '因子演化任务',
          status: normalizeStatus(row.status),
          progress: pct,
          startedAt: row.created_at ?? row.createdAt ?? null,
          href: TASK_KIND_META.mining.href,
          detail: row.factor_ids?.length ? `已产出 ${row.factor_ids.length} 因子` : undefined,
        } satisfies BackgroundTask;
      }),
    );
  } catch {
    return [];
  }
}

async function fetchSync(): Promise<BackgroundTask[]> {
  try {
    const { data } = await apiClient.get<any>('/api/v1/admin/data-platform/sync-progress');
    const p = data?.data ?? data;
    if (!p || String(p.step ?? 'idle') === 'idle') return [];
    const pct = clampPct(p.pct);
    if (pct !== null && pct >= 100) return [];
    return [
      {
        id: `sync:${String(p.step)}`,
        kind: 'sync',
        title: '市场数据同步',
        status: 'running',
        progress: pct,
        startedAt: null,
        href: TASK_KIND_META.sync.href,
        detail: [p.step, p.detail].filter(Boolean).join(' · ') || undefined,
      },
    ];
  } catch {
    return [];
  }
}

export async function fetchBackgroundTasks(): Promise<BackgroundTask[]> {
  const groups = await Promise.all([
    fetchTraining(),
    fetchInference(),
    fetchMining(),
    fetchSync(),
  ]);
  return groups.flat().filter((t) => t.id);
}

export async function fetchServerLoad(): Promise<ServerLoad | null> {
  try {
    const { data } = await apiClient.get<any>('/api/v1/system/load');
    return {
      cpu_pct: data?.cpu_pct ?? null,
      mem_used_gb: data?.mem_used_gb ?? null,
      mem_total_gb: data?.mem_total_gb ?? null,
      load1: data?.load1 ?? null,
      disk_used_pct: data?.disk_used_pct ?? null,
      gpu: data?.gpu
        ? {
            name: String(data.gpu.name ?? 'GPU'),
            util_pct: Number(data.gpu.util_pct ?? 0),
            mem_used_mb: Number(data.gpu.mem_used_mb ?? 0),
            mem_total_mb: Number(data.gpu.mem_total_mb ?? 0),
          }
        : null,
    };
  } catch {
    return null;
  }
}

/** 取消因子演化任务（保留原监视器的取消能力）。 */
export async function cancelMiningTask(taskId: string): Promise<void> {
  await apiClient.post(`/api/v1/alpha-agent/tasks/${taskId}/cancel`);
}

/** 取消推理补推任务（服务端支持 queued/running 的取消请求）。 */
export async function cancelInferenceTask(taskId: string): Promise<void> {
  await apiClient.post(
    `/api/v1/models/inference/backfill-jobs/${encodeURIComponent(taskId)}/cancel`,
  );
}
