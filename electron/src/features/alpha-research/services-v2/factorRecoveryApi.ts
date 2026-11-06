import { apiClient } from '../../../services/aiStrategyClients';
import type { FactorJob } from './factorJobsApi';

export interface RecoverySubmission {
  factor_ids: string[]; universe: string; start_date?: string; end_date?: string; confirm_ai: true;
}
export interface RecoveryAttempt {
  job_id: string; revision_no: number | null; code_sha256: string | null;
  model: string; prompt_version: string; status: string; created_at: string;
  job_status: string;
  error_message: string | null;
}
function unwrap(value: unknown): FactorJob {
  const job = value as FactorJob | null;
  if (!job || typeof job.job_id !== 'string' || job.kind !== 'recovery') throw new Error('代码恢复作业响应无效');
  return job;
}
export async function submitFactorRecovery(body: RecoverySubmission, key: string, signal: AbortSignal): Promise<FactorJob> {
  const response = await apiClient.post('/alpha-agent/factors/recovery', body, { headers: { 'Idempotency-Key': key }, signal });
  return unwrap(response.data?.data);
}
export async function getRecoveryJob(jobId: string, signal: AbortSignal): Promise<FactorJob> {
  const response = await apiClient.get(`/alpha-agent/factor-jobs/${encodeURIComponent(jobId)}`, { signal });
  return unwrap(response.data?.data);
}
export async function getRecoveryHistory(factorId: string, signal: AbortSignal): Promise<RecoveryAttempt[]> {
  const response = await apiClient.get(`/alpha-agent/factors/${encodeURIComponent(factorId)}/recovery-history`, { signal });
  if (!Array.isArray(response.data?.data)) throw new Error('代码恢复历史响应无效');
  return response.data.data as RecoveryAttempt[];
}
