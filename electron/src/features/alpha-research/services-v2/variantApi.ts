import { apiClient } from '../../../services/aiStrategyClients';

export interface VariantMetric {
  horizon: number; n_days: number; status: string; ic_ir: number | null; ic_mean: number | null;
}
export interface VariantLedgerRow {
  key: string; name: string; family: string; baseline_key: string | null; edit_type?: string | null; coolable: boolean;
  observations_count: number; latest_stat: VariantMetric | null; latest_at: string | null;
  decision_stat: VariantMetric | null; cooling_active: boolean; cooled_until: string | null; reset_at: string | null;
  observations: Array<{ task_id: string; at: string; best_horizon: number | null; horizons: VariantMetric[] }>;
}
export interface VariantContextPage {
  items: Array<{ context_id: string; window: Record<string, string | null>; created_at: string; updated_at: string }>;
  total: number; limit: number; offset: number;
}
export interface VariantLedger {
  context_id: string; updated_at: string;
  scope: {
    market: string; universe: string; algorithm_version: string;
    window: Record<string, string | null>; parameters: Record<string, unknown>;
    data: { panel_sha256: string; metadata_sha256: string; symbols_sha256: string };
  };
  constants: { cooldown_days: number; high_conf_days: number; cool_icir_floor: number; max_observations: number };
  variants: VariantLedgerRow[];
  reset_events: Array<{ variant_key: string; reason: string; at: string }>;
}

export async function getVariantContexts(universe: string, offset: number, signal: AbortSignal): Promise<VariantContextPage> {
  const query = new URLSearchParams({ universe, offset: String(offset), limit: '20' });
  const response = await apiClient.get(`/alpha-agent/research/variant-ledger?${query}`, { signal });
  const data = response.data?.data as VariantContextPage | undefined;
  if (!data || !Array.isArray(data.items) || typeof data.total !== 'number') throw new Error('变体环境列表响应无效');
  return data;
}

export async function getVariantLedger(contextId: string, signal: AbortSignal): Promise<VariantLedger> {
  const response = await apiClient.get(`/alpha-agent/research/variant-ledger/${encodeURIComponent(contextId)}`, { signal });
  const data = response.data?.data as VariantLedger | undefined;
  if (!data || data.context_id !== contextId || !data.scope || !data.constants || !Array.isArray(data.variants)) throw new Error('变体台账响应无效');
  return data;
}

export async function resetVariantCooling(contextId: string, variantKey: string, reason: string, signal: AbortSignal): Promise<void> {
  const response = await apiClient.post(`/alpha-agent/research/variant-ledger/${encodeURIComponent(contextId)}/${encodeURIComponent(variantKey)}/reset`, { reason }, { signal });
  const data = response.data?.data;
  if (data?.context_id !== contextId || data?.variant_key !== variantKey || typeof data.changed !== 'boolean') throw new Error('解除冷却回执无效');
}
