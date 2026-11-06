import { apiClient } from '../../../services/aiStrategyClients';

export interface ResearchExperience {
  available: boolean;
  market: 'a_share';
  universe: string | null;
  generated_at: string;
  injection_enabled: boolean;
  reason_buckets: Array<{
    bucket: string; label: string; count: number; last_at: string | null;
    sample_dirs: string[]; sample_columns: string[]; sources: string[];
  }>;
  direction_stats: Array<{
    label: string; tasks: number; completed: number; failed: number; cancelled: number; factors: number;
  }>;
  saturated_directions: Array<{
    label: string; dup_hits: number; dup_hits_90d: number;
    top_opponent: string | null; materialized_count: number;
  }>;
  sources: {
    tasks: number; tasks_total: number;
    statistical_results: number; statistical_results_total: number;
    factor_jobs: number; factor_jobs_total: number;
    sample_truncated: boolean;
    directions_omitted: number; saturated_directions_omitted: number;
    scope: string; duplicates_scope: string;
  };
}

export async function getResearchExperience(universe: string, signal: AbortSignal): Promise<ResearchExperience> {
  const query = new URLSearchParams({ universe });
  const response = await apiClient.get(`/alpha-agent/research/experience?${query}`, { signal });
  const data = response.data?.data as ResearchExperience | undefined;
  if (!data || data.market !== 'a_share' || data.universe !== universe
    || typeof data.available !== 'boolean' || !data.sources
    || !Array.isArray(data.reason_buckets) || !Array.isArray(data.direction_stats)
    || !Array.isArray(data.saturated_directions)) {
    throw new Error('研究经验响应无效');
  }
  return data;
}
