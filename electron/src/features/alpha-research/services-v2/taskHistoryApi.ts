import { apiClient } from '../../../services/aiStrategyClients';

export interface MiningTaskHistoryItem {
  task_id: string;
  market: string | null;
  universe: string | null;
  loop_n: number | null;
  direction: string | null;
  source_doc_id?: string | null;
  batch_id?: string | null;
  batch_index?: number | null;
  batch_size?: number | null;
  data_source: string | null;
  status: string;
  progress: string | null;
  phase: string | null;
  progress_pct: number | null;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
  archived_at: string | null;
  error: string | null;
  algorithm: string;
  algorithm_version: string | null;
  params_snapshot: Record<string, unknown> | null;
  seed_factor_ids: string[];
  data_snapshot: Record<string, unknown> | null;
  schema_version: number | null;
  factor_count: number;
}

export interface MiningTaskHistoryPage {
  tasks: MiningTaskHistoryItem[];
  total: number;
  limit: number;
  offset: number;
}

export interface StatisticalMiningResult {
  variant_context_id?: string;
  cooled_keys?: string[];
  selected: string[];
  universe?: string;
  panel_rows?: number;
  n_symbols?: number;
  window?: {
    mining_start?: string;
    mining_end?: string;
    valid_start?: string | null;
    valid_end?: string | null;
    validation_overlaps_mining?: boolean;
  };
  candidates: Array<{
    key: string;
    name: string;
    family?: string;
    expression: string;
    selected: boolean;
    reject_reason?: string;
    selection_score?: number | null;
    rank_ic_mean?: number | null;
    icir?: number | null;
    best_horizon?: number | null;
    oos_rank_ic?: number | null;
    oos_n_days?: number;
  }>;
}

export interface MiningTaskFactorResult {
  factor_id: string;
  factor_name: string;
  factor_formulation: string;
  status: string;
  rank_ic?: number | null;
  factor_revision_no?: number | null;
}

export interface MiningTaskResult {
  task_id: string;
  algorithm: 'rdagent' | 'statistical';
  status: string;
  result: StatisticalMiningResult | null;
  factors: MiningTaskFactorResult[];
}

export interface MiningTaskTrajectory {
  task_id: string;
  direction: string | null;
  market: string | null;
  status: string | null;
  partial: boolean;
  attribution: 'loop' | 'task_level';
  truncated: boolean;
  dropped_loops: number;
  flag_texts: Record<string, string>;
  factors?: Array<{ name: string; selected: boolean | null; outcome: string | null; reason: string | null }>;
  loops: Array<{
    loop: number;
    flags: string[];
    factors: Array<{ name: string; selected: boolean | null; outcome: string | null; reason: string | null }>;
    phases: Array<{
      phase: string;
      label: string;
      status: 'observed' | 'absent';
      first_seen: string | null;
      last_seen: string | null;
      start_time: string | null;
      end_time: string | null;
      duration_seconds: number | null;
      time_source: 'time_info' | 'file_mtime';
      artifact_count: number;
      count_truncated: boolean;
    }>;
  }>;
}

export async function getTaskHistory(
  params: { limit: number; offset: number; status?: string; includeArchived?: boolean },
  signal?: AbortSignal,
): Promise<MiningTaskHistoryPage> {
  const query = new URLSearchParams({ limit: String(params.limit), offset: String(params.offset) });
  if (params.status) query.set('status', params.status);
  if (params.includeArchived) query.set('include_archived', 'true');
  const response = await apiClient.get(`/alpha-agent/tasks/history?${query.toString()}`, { signal });
  return response.data?.data as MiningTaskHistoryPage;
}

export async function getTaskLog(
  taskId: string,
  signal?: AbortSignal,
): Promise<{ lines: string[]; total: number }> {
  const response = await apiClient.get(`/alpha-agent/tasks/${taskId}/log?tail=500&offset=0`, { signal });
  const data = response.data?.data ?? {};
  return { lines: data.lines ?? [], total: data.total ?? 0 };
}

export async function getMiningResult(
  taskId: string,
  signal?: AbortSignal,
): Promise<StatisticalMiningResult> {
  const taskResult = await getTaskMiningResult(taskId, signal);
  const result = taskResult.result;
  if (!result) throw new Error('统计挖掘结果暂不可用');
  return result;
}

export async function getTaskMiningResult(
  taskId: string,
  signal?: AbortSignal,
): Promise<MiningTaskResult> {
  const response = await apiClient.get(
    `/alpha-agent/tasks/${encodeURIComponent(taskId)}/result`,
    { signal },
  );
  const data = response.data?.data;
  if (
    !data
    || data.task_id !== taskId
    || !['rdagent', 'statistical'].includes(data.algorithm)
    || !Array.isArray(data.factors)
    || (data.result !== null && data.result !== undefined && (
      !Array.isArray(data.result.selected) || !Array.isArray(data.result.candidates)
    ))
  ) {
    throw new Error('挖掘任务结果格式无效');
  }
  return {
    task_id: data.task_id,
    algorithm: data.algorithm,
    status: typeof data.status === 'string' ? data.status : 'unknown',
    result: (data.result ?? null) as StatisticalMiningResult | null,
    factors: data.factors as MiningTaskFactorResult[],
  };
}

export async function getTaskTrajectory(
  taskId: string,
  signal?: AbortSignal,
): Promise<MiningTaskTrajectory> {
  const response = await apiClient.get(
    `/alpha-agent/tasks/${encodeURIComponent(taskId)}/trajectory`,
    { signal },
  );
  const data = response.data?.data;
  if (!data || data.task_id !== taskId || !Array.isArray(data.loops)) {
    throw new Error('挖掘轨迹格式无效');
  }
  return data as MiningTaskTrajectory;
}

export async function cancelMining(taskId: string, signal?: AbortSignal): Promise<void> {
  await apiClient.post(`/alpha-agent/tasks/${taskId}/cancel`, undefined, { signal });
}

export async function archiveMiningTask(taskId: string, signal?: AbortSignal): Promise<void> {
  await apiClient.post(
    `/alpha-agent/tasks/${encodeURIComponent(taskId)}/archive`,
    undefined,
    { signal },
  );
}

export async function restoreMiningTask(taskId: string, signal?: AbortSignal): Promise<void> {
  await apiClient.post(
    `/alpha-agent/tasks/${encodeURIComponent(taskId)}/restore`,
    undefined,
    { signal },
  );
}
