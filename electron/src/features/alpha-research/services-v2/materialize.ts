/**
 * 用户自助物化通道（POST /factors/materialize · GET /factors/materialize/status）
 *
 * 与 admin 面板共用服务端同一把 flock（物化独占同一座库）。前端纪律：
 * - 「运行中」只信服务端 `running`，本地不做任何猜测（完成判据见 RunQueueContext）；
 * - 409（锁忙/启动确认未完成）与 400（非法 id）的 FastAPI `detail` 原文必须上抛，
 *   否则用户只见「点不动」不见原因。
 */

import { apiClient } from '../../../services/aiStrategyClients';
import type { FactorMaterializationStatus } from '../types-v2';

/** 与服务端 normalize_factor_ids 上限对齐。 */
export const MATERIALIZE_MAX_IDS = 100;

export interface MaterializeStartResult {
  started: boolean;
  running: boolean;
  confirmed?: boolean;
  pid?: number;
  logPath?: string;
  requested: number;
  /** 真正送进物化器的 id */
  materializable: string[];
  /** id → 跳过原因（already_materialized / rejected_duplicate / rejected_gate / no_code / market_unsupported …） */
  skipped: Record<string, string>;
  rejected: Array<{ factorId: string; reason: string }>;
  message: string;
}

export interface MaterializeFactorEntry {
  factorId: string;
  status: FactorMaterializationStatus;
  at: string | null;
}

export interface MaterializeStatusResult {
  running: boolean;
  factors: MaterializeFactorEntry[];
  lastAt: string | null;
}

const KNOWN_STATUS: ReadonlySet<string> = new Set([
  'materialized',
  'rejected_duplicate',
  'rejected_gate',
  'error',
  'none',
]);

/** 从 Axios 错里取 FastAPI detail 原文；没有就退 err.message，再退 fallback。 */
export function extractApiDetail(err: unknown, fallback: string): string {
  const detail = (err as any)?.response?.data?.detail;
  if (typeof detail === 'string' && detail.trim()) return detail;
  if (detail != null && typeof detail === 'object') {
    try {
      return JSON.stringify(detail);
    } catch {
      /* fallthrough */
    }
  }
  const message = (err as any)?.message;
  return typeof message === 'string' && message.trim() ? message : fallback;
}

export async function startMaterialize(
  factorIds: string[],
  opts: { force?: boolean } = {},
): Promise<MaterializeStartResult> {
  // 空列表会被服务端 400（空列表会退化成全库物化，必须响亮拒绝）——调用方先禁按钮
  const res = await apiClient.post('/alpha-agent/factors/materialize', {
    factor_ids: factorIds,
    force: Boolean(opts.force),
  });
  const data = res.data?.data ?? {};
  const skipped: Record<string, string> = {};
  if (data.skipped && typeof data.skipped === 'object') {
    for (const [key, value] of Object.entries(data.skipped)) {
      skipped[key] = String(value);
    }
  }
  return {
    started: Boolean(data.started),
    running: Boolean(data.running),
    confirmed: typeof data.confirmed === 'boolean' ? data.confirmed : undefined,
    pid: typeof data.pid === 'number' ? data.pid : undefined,
    logPath: typeof data.log_path === 'string' ? data.log_path : undefined,
    requested: typeof data.requested === 'number' ? data.requested : factorIds.length,
    materializable: Array.isArray(data.materializable)
      ? data.materializable.map(String)
      : [],
    skipped,
    rejected: Array.isArray(data.rejected)
      ? data.rejected.map((r: any) => ({
          factorId: String(r?.factor_id ?? ''),
          reason: String(r?.reason ?? ''),
        }))
      : [],
    message: typeof data.message === 'string' ? data.message : '',
  };
}

export async function getMaterializeStatus(
  factorIds: string[],
): Promise<MaterializeStatusResult> {
  const res = await apiClient.get('/alpha-agent/factors/materialize/status', {
    params: { factor_ids: factorIds.join(',') },
  });
  const data = res.data?.data ?? {};
  return {
    running: Boolean(data.running),
    lastAt: typeof data.last_at === 'string' ? data.last_at : null,
    factors: Array.isArray(data.factors)
      ? data.factors.map((f: any): MaterializeFactorEntry => {
          const status = String(f?.status ?? 'none');
          return {
            factorId: String(f?.factor_id ?? ''),
            status: (KNOWN_STATUS.has(status)
              ? status
              : 'none') as FactorMaterializationStatus,
            at: typeof f?.at === 'string' ? f.at : null,
          };
        })
      : [],
  };
}
