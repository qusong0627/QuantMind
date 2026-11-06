import { apiClient } from '../../../services/aiStrategyClients';
import type { FactorJob } from './factorJobsApi';

export interface ArtifactCleanupPreview {
  confirmed: false;
  preview_token: string;
  items: Array<{ task_id: string; size_bytes: number }>;
  total_bytes: number;
  note: string;
}

export async function previewArtifactCleanup(taskIds: string[], signal: AbortSignal): Promise<ArtifactCleanupPreview> {
  const response = await apiClient.post('/alpha-agent/storage/cleanup', { task_ids: taskIds, confirm: false }, { signal });
  const data = response.data?.data;
  if (data?.confirmed !== false || typeof data.preview_token !== 'string' || !Array.isArray(data.items)
    || typeof data.total_bytes !== 'number' || !Number.isFinite(data.total_bytes)) throw new Error('清理预览响应无效');
  return data as ArtifactCleanupPreview;
}

export async function submitArtifactCleanup(preview: ArtifactCleanupPreview, requestKey: string, signal: AbortSignal): Promise<FactorJob> {
  const response = await apiClient.post('/alpha-agent/storage/cleanup', {
    task_ids: preview.items.map((item) => item.task_id), confirm: true, preview_token: preview.preview_token,
  }, { signal, headers: { 'Idempotency-Key': requestKey } });
  const job = response.data?.data?.job;
  if (response.data?.data?.confirmed !== true || typeof job?.job_id !== 'string' || job.kind !== 'cleanup') throw new Error('清理作业回执无效');
  return job as FactorJob;
}
