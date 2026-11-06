import { apiClient } from '../../../services/aiStrategyClients';
import type { DocumentMiningBatchReceipt, ResearchMiningBatchRequest } from './docApi';

export interface SampledDirectionRequest extends ResearchMiningBatchRequest {
  selection_mode: 'selected' | 'random' | 'quota';
  num_directions: number;
}
export interface SampledDirectionBatch {
  batch_id: string; created_at: string; request: SampledDirectionRequest;
  items: Array<{ index: number; direction: string; source_index: number; evidence: unknown; task_id: string | null; status: string }>;
  dispatch?: DocumentMiningBatchReceipt;
}
export interface SampledDirectionHistory {
  batch_id: string; created_at: string; universe: string; selection_mode: 'selected' | 'random' | 'quota'; selected_count: number;
}
function batch(value: SampledDirectionBatch | undefined, id: string): SampledDirectionBatch {
  if (!value || value.batch_id !== id || !value.request || value.request.batch_id !== id
    || !Array.isArray(value.items) || !value.items.length || value.items.length > 10
    || value.items.some((item, index) => item.index !== index || typeof item.direction !== 'string'
      || typeof item.status !== 'string' || (item.task_id !== null && typeof item.task_id !== 'string'))) {
    throw new Error('批次回执格式无效，请保留原批次ID并从历史找回');
  }
  return value;
}
export async function dispatchSampledDirectionBatch(payload: SampledDirectionRequest, signal: AbortSignal) {
  const response = await apiClient.post<{ data?: SampledDirectionBatch }>('/alpha-agent/directions/sample-dispatch', payload, { signal });
  return batch(response.data.data, payload.batch_id);
}
export async function getSampledDirectionBatch(id: string, signal: AbortSignal) {
  const response = await apiClient.get<{ data?: SampledDirectionBatch }>(`/alpha-agent/directions/batches/${encodeURIComponent(id)}`, { signal });
  return batch(response.data.data, id);
}
export async function listSampledDirectionBatches(offset: number, signal: AbortSignal): Promise<{ items: SampledDirectionHistory[]; has_more: boolean }> {
  const response = await apiClient.get('/alpha-agent/directions/batches/history', { params: { offset }, signal });
  if (!Array.isArray(response.data?.data?.items)) throw new Error('批次历史格式无效');
  return response.data.data;
}
