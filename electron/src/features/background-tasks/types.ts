export type TaskKind = 'training' | 'inference' | 'mining' | 'sync';

export type TaskStatus = 'queued' | 'running' | 'done' | 'failed';

export interface BackgroundTask {
  id: string;
  kind: TaskKind;
  title: string;
  status: TaskStatus;
  /** 0–100；null 表示不确定进度（原生 <progress> 省略 value） */
  progress: number | null;
  startedAt?: string | null;
  etaSeconds?: number | null;
  href: string;
  detail?: string;
}

export interface ServerLoad {
  cpu_pct: number | null;
  mem_used_gb: number | null;
  mem_total_gb: number | null;
  load1: number | null;
  disk_used_pct: number | null;
  gpu: {
    name: string;
    util_pct: number;
    mem_used_mb: number;
    mem_total_mb: number;
  } | null;
}

export const TASK_KIND_META: Record<
  TaskKind,
  { label: string; short: string; href: string }
> = {
  training: { label: '训练', short: '训', href: '/admin/models' },
  inference: { label: '推理', short: '推', href: '/admin/inference' },
  mining: { label: '挖掘', short: '挖', href: '/quantbot' },
  sync: { label: '数据同步', short: '数', href: '/admin/data' },
};

export const TASK_KIND_ORDER: TaskKind[] = ['training', 'inference', 'mining', 'sync'];
