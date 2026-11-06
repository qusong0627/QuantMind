import { apiClient } from '../../../services/aiStrategyClients';

export interface PoolDiversitySource {
  job_id: string; completed_at: string; method: string;
  n_days: number; n_obs: number; effective_factors: number;
}

export interface PoolCleanupSuggestion {
  factor_id: string;
  factor_name: string;
  universe: string;
  icir: number | null;
  pool_score: number | null;
  max_pool_corr: number | null;
  max_pool_corr_with: string | null;
  diversity_contrib: number | null;
  diversity_source?: PoolDiversitySource | null;
  severity: 'high' | 'medium';
  reasons: Array<{ code: 'duplicate' | 'weak_icir' | 'no_diversity'; label: string; detail: string }>;
}

export interface PoolCleanupReport {
  items: PoolCleanupSuggestion[];
  pool_size: number;
  archived_member_count: number;
  total: number;
  has_more: boolean;
  as_of: string;
  criteria: { corr_dup: number; weak_icir_quantile: number; min_icir_sample: number };
  scopes: Array<{ universe: string; pool_size: number; icir_sample_size: number; weak_icir_threshold: number | null; diversity_source?: PoolDiversitySource | null }>;
  summary: Record<string, number>;
  missing_evidence: { icir: number; pool_score: number; max_pool_corr: number; diversity_contrib: number };
}

export async function getPoolCleanupSuggestions(
  universe: string | undefined,
  offset: number,
  signal: AbortSignal,
): Promise<PoolCleanupReport> {
  const response = await apiClient.get('/alpha-agent/factors/pool/cleanup/suggestions', {
    params: { universe, offset, limit: 50 }, signal,
  });
  const data = response.data?.data as PoolCleanupReport | undefined;
  if (!data || !Array.isArray(data.items) || !Array.isArray(data.scopes)
    || !data.criteria || !data.missing_evidence || typeof data.total !== 'number'
    || data.items.some((item) => typeof item.factor_id !== 'string' || typeof item.universe !== 'string' || !Array.isArray(item.reasons))) {
    throw new Error('本人因子池建议格式无效');
  }
  return data;
}
