/** 串行补推任务的 API 类型（与后端 `model_inference_jobs` 路由同构）。 */

export type BackfillJobStatus =
  | 'queued'
  | 'running'
  | 'cancelling'
  | 'partial'
  | 'completed'
  | 'failed'
  | 'cancelled';

export interface BackfillPreviewModel {
  model_id: string;
  display_name: string;
  model_type: string;
  horizon_days: number | null;
  market: string;
  status: string;
  latest_data_date: string | null;
  gap_count: number;
  factor_source?: string | null;
  factor_latest_date?: string | null;
  factor_lag_days?: number;
  factor_stale?: boolean;
  factor_incomplete?: boolean;
  factor_missing_count?: number;
  estimated_seconds: number;
  eligible: boolean;
  reason: string | null;
}

export interface BackfillPreviewExcluded {
  model_id: string;
  reason: string;
}

export interface BackfillPreview {
  models: BackfillPreviewModel[];
  excluded: BackfillPreviewExcluded[];
  totals: {
    models: number;
    eligible_models: number;
    days: number;
    estimated_seconds: number;
  };
  warnings: string[];
}

export interface BackfillModelError {
  date: string;
  error: string;
}

export interface BackfillModelResult {
  model_id: string;
  display_name: string;
  status: string;
  days_total: number;
  days_completed: number;
  days_failed: number;
  days_skipped: number;
  days_merged: number;
  errors: BackfillModelError[];
  gap_dates: string[];
}

export interface BackfillJob {
  job_id: string;
  status: BackfillJobStatus;
  mode: string;
  /** 0–1 */
  progress: number;
  current: { model_id: string; date: string | null } | null;
  totals: {
    models: number;
    days: number;
    completed_days: number;
    failed_days: number;
    skipped_days: number;
  };
  models: BackfillModelResult[];
  logs: string[];
  error: string | null;
  cancel_requested: boolean;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  heartbeat_at: string | null;
}

export interface BackfillJobList {
  items: BackfillJob[];
  active_job_id: string | null;
}

export const ACTIVE_JOB_STATUSES: ReadonlySet<BackfillJobStatus> = new Set([
  'queued',
  'running',
  'cancelling',
]);

/** 排除/状态原因 → 人话（未登记的原因原样展示，不吞）。 */
export const BACKFILL_REASON_LABELS: Record<string, string> = {
  system_readonly: '系统模型只读',
  archived: '已归档',
  not_ready: '未就绪',
  missing_storage: '无产物目录',
  coverage_failed: '覆盖读取失败',
  not_found: '模型不存在',
  skipped_busy: '该模型正在别处推理',
  market_mismatch: '其他市场（本任务不含）',
  gap_too_large: '缺口过大',
  factor_incomplete: '因子源缺列',
};

export function backfillReasonLabel(reason: string | null | undefined): string {
  if (!reason) return '—';
  return BACKFILL_REASON_LABELS[reason] ?? reason;
}

/** 秒 → 人读时长（与后端预览文案同量级）。 */
export function formatDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return '—';
  if (seconds < 60) return `${Math.round(seconds)} 秒`;
  const minutes = seconds / 60;
  if (minutes < 60) return `${Math.round(minutes)} 分钟`;
  return `${(minutes / 60).toFixed(1)} 小时`;
}
