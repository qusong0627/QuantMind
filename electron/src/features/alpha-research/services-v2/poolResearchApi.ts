import { apiClient } from '../../../services/aiStrategyClients';
import type { FactorJob } from './factorJobsApi';

export interface PoolOrthogonality {
  status: string;
  orthogonal?: boolean | null;
  threshold?: number;
  max_parent_abs_corr?: number | null;
  parents?: Array<{ factor_id: string; name?: string; abs_corr: number | null; factor_revision_no: number; panel_version: string | null }>;
  residual_ic?: number | null;
  candidate_ic?: number | null;
  n_days?: number;
  n_obs?: number;
  note?: string | null;
}
export interface PoolResearchResult {
  diversity?: {
    status: 'computed' | 'unavailable'; reason: string | null;
    method?: string; implementation_origin?: string;
    entropy?: number | null; normalized_entropy?: number | null; effective_factors?: number | null;
    n_days?: number; n_obs?: number; start_date?: string | null; end_date?: string | null;
    factor_ids?: string[]; missing_factor_ids?: string[];
    contributions?: Record<string, { effective_without: number; delta: number }>;
  };
  factor_count: number;
  stats: { evaluated: number; orthogonal: number; not_orthogonal: number; unknown: number; no_panel: number; by_status: Record<string, number> };
  criteria: { max_parents: number; min_stocks_per_day: number; min_sample_days: number; max_parent_abs_corr: number };
  results: Array<{
    factor_id: string; factor_name: string; factor_revision_no: number; panel_version: string | null; icir: number | null;
    panel_source_job_id: string | null; panel_source_generation: number | null;
    orthogonality: PoolOrthogonality;
    panel_window: { start_date: string; end_date: string; n_rows: number; n_days: number; has_forward_returns: boolean } | null;
  }>;
}
export type PoolResearchJob = Omit<FactorJob, 'result_snapshot'> & { result_snapshot?: PoolResearchResult | null };
export interface PoolResearchDetail { job: PoolResearchJob; current_members_match: boolean; numeric_context_match: boolean }
export interface PoolResearchHistoryItem {
  job_id: string; status: FactorJob['status']; created_at: string; completed_at: string | null;
  error_message: string; stats: PoolResearchResult['stats'] | null;
}
export async function submitPoolResearch(universe: string, key: string, signal: AbortSignal): Promise<PoolResearchJob> {
  const response = await apiClient.post('/alpha-agent/factors/pool/research/submit', { universe }, { signal, headers: { 'Idempotency-Key': key } });
  const data = response.data?.data;
  if (!data || data.kind !== 'pool_research' || typeof data.job_id !== 'string') throw new Error('池研究回执无效，请使用原请求重试');
  return data as PoolResearchJob;
}
export async function getPoolResearch(id: string, signal: AbortSignal): Promise<PoolResearchDetail> {
  const response = await apiClient.get(`/alpha-agent/factors/pool/research/${encodeURIComponent(id)}`, { signal });
  const data = response.data?.data;
  if (!data?.job || data.job.job_id !== id || data.job.kind !== 'pool_research') throw new Error('池研究结果格式无效');
  const result = data.job.result_snapshot;
  if (data.job.status === 'completed' && (!result || !Array.isArray(result.results) || !result.stats || !result.criteria)) throw new Error('池研究没有完整的保存结果');
  return data as PoolResearchDetail;
}
export async function listPoolResearch(universe: string, offset: number, signal: AbortSignal): Promise<{ items: PoolResearchHistoryItem[]; has_more: boolean }> {
  const response = await apiClient.get('/alpha-agent/factors/pool/research/history', { params: { universe, offset }, signal });
  const data = response.data?.data;
  if (!data || !Array.isArray(data.items)) throw new Error('池研究历史格式无效');
  return data;
}
