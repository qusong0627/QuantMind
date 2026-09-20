/**
 * 交易黑名单（候选列表排除名单）的载荷类型。
 *
 * 与后端 `backend/services/api/routers/exclusion_admin.py` 的响应逐字段对应。
 * 三个字段承担了「这一行为什么长这样」的全部信息，缺一不可：
 *
 * - `sources` —— 机器来源（基本面/事件/新闻/操作员）还是本人手工；
 * - `blocking` —— **当前是否真的拦买**（放行后为 false，但行仍会返回）；
 * - `manual` —— 本人对这条做过什么（null = 没动过）。
 */

/** 单个来源的逐源说明（悬停/下钻用） */
export interface BlacklistSourceDetail {
  flags?: string[];
  reason?: string;
  expire?: string | null;
  label?: string;
}

/** 本人对某条名单的改动（null = 没动过） */
export interface BlacklistManualEntry {
  symbol: string;
  action: BlacklistAction;
  action_label?: string;
  reason: string;
  note: string;
  expire: string | null;
  operator: string;
  created_at: string;
  updated_at: string;
}

export type BlacklistAction = 'block' | 'allow';

/** 表格一行 */
export interface BlacklistRow {
  symbol: string;
  name?: string;
  sources: string[];
  source_labels?: string[];
  flags?: string[];
  reason?: string;
  expire?: string | null;
  /** 当前是否拦买（放行后 false，行仍在表里） */
  blocking: boolean;
  expired?: boolean;
  by_source?: Record<string, BlacklistSourceDetail>;
  manual?: BlacklistManualEntry | null;
}

/** 机器名单的元信息（基准日 / 陈旧度 / 逐源条数） */
export interface BlacklistListMeta {
  imported?: boolean;
  market?: string;
  asof?: string;
  generated_at?: string;
  stale_days?: number | null;
  stale?: boolean;
  blocking_now?: number;
  counts?: { total?: number; blocking?: number; by_source?: Record<string, number> };
  sources?: Record<string, { label?: string; count?: number; asof?: string; blocking?: boolean }>;
  /** 用户层摘要：手工 N 条 / 有效放行 N 条 / **放行未命中** N 条 */
  overlay?: { updated_at?: string; manual?: number; allow?: number; allow_miss?: number } | null;
}

/** 用户层整体（`/meta` 端点返回） */
export interface BlacklistOverlay {
  market: string;
  updated_at: string;
  counts: { total: number; block: number; allow: number };
  items: BlacklistManualEntry[];
}

export interface BlacklistListResponse {
  imported: boolean;
  reason?: string;
  total: number;
  page: number;
  page_size: number;
  items: BlacklistRow[];
  meta?: BlacklistListMeta | null;
  overlay?: BlacklistOverlay | null;
}

export interface BlacklistMetaResponse {
  market: string;
  imported: boolean;
  reason?: string;
  meta?: BlacklistListMeta | null;
  overlay: BlacklistOverlay;
  sources: Record<string, { label?: string; count?: number; asof?: string; blocking?: boolean }>;
}

/** 表格的「动作」过滤档 */
export type ActionFilter = 'all' | 'manual' | 'allow' | 'machine';
