import { apiClient } from '../../../services/aiStrategyClients';
import type { FactorJob } from './factorJobsApi';

export interface PurgeStorageItem {
  exists: boolean;
  bytes: number;
  entries: number;
}

export interface PurgePreview {
  counts: Record<string, number>;
  blockers: Record<string, number>;
  storage: {
    items: Record<string, PurgeStorageItem>;
    total_bytes: number;
  };
  total_bytes: number;
  preview_token: string;
  generated_at: string;
}

export interface PurgeSubmitResult {
  job_id: string;
  status: string;
  reused: boolean;
}

export async function previewOwnerPurge(signal?: AbortSignal, taskId?: string): Promise<PurgePreview> {
  const response = await apiClient.post(
    `/alpha-agent/purge/preview${taskId ? `?task_id=${encodeURIComponent(taskId)}` : ''}`,
    undefined,
    { signal },
  );
  const data = response.data?.data;
  if (!data || typeof data.preview_token !== 'string') {
    throw new Error('清理预览格式无效');
  }
  return data as PurgePreview;
}

export async function submitOwnerPurge(
  previewToken: string,
  idempotencyKey: string,
  signal?: AbortSignal,
  taskId?: string,
): Promise<PurgeSubmitResult> {
  const response = await apiClient.post(
    '/alpha-agent/purge',
    taskId ? { confirm: true, preview_token: previewToken, task_id: taskId } : { confirm: true, preview_token: previewToken },
    { headers: { 'Idempotency-Key': idempotencyKey }, signal },
  );
  const data = response.data?.data;
  if (!data?.job_id) throw new Error('清理作业提交失败');
  return data as PurgeSubmitResult;
}

export async function getOwnerPurge(
  jobId: string,
  signal?: AbortSignal,
): Promise<FactorJob> {
  const response = await apiClient.get(
    `/alpha-agent/purge/${encodeURIComponent(jobId)}`,
    { signal },
  );
  const data = response.data?.data;
  if (!data?.job_id) throw new Error('清理作业状态无效');
  return data as FactorJob;
}
