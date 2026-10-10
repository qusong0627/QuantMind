/**
 * 持仓行情新鲜度口径（T6-1 审计 H7a，前端唯一谓词，镜像 `backend/shared/freshness.py`）。
 *
 * 服务端每条 quote 已带 `is_stale`（推送时刻按 freshness 谓词判级）与 `timestamp`（行情时刻）；
 * 前端不重复分级，只把「你现在看的这个价到底有多新」如实标出来：
 * - **已陈旧**：仅 A 股交易时段内按最后一条行情的时间戳计时——推送判重只比价格
 *   （`quote_pusher._has_quote_changed`），安静标的没推送 ≠ 断流，非时段不标；
 * - **日线兜底**：`data_source=quantdb` 的行（时间戳是零点，按秒计时无意义）独立徽标，不按秒计时。
 */
import { isCnTradingHours } from '../../../utils/timeBeijing';

/** 新鲜线（秒）：与服务端 `QM_QUOTE_FRESH_WITHIN_S` 默认一致（freshness.py DEFAULT_FRESH_WITHIN_S）。 */
export const QUOTE_FRESH_WITHIN_S = 60;

/** 日线兜底源标识（stream `quantdb_source.py` 写出的 data_source 值）。 */
export const DAILY_FALLBACK_SOURCE = 'quantdb';

/** 一条 WS 行情消息里与新鲜度有关的全部信息 */
export interface QuoteMeta {
  price: number;
  /** 服务端 freshness 谓词判定（推送时刻的 is_stale 原样透传） */
  isStale: boolean;
  /** 数据来源：tdx_bridge / qmt_big / remote_redis / quantdb…（未知为空串） */
  source: string;
  /** 行情时刻 epoch ms；缺失/不可解析 → null */
  tsMs: number | null;
}

export type QuoteFreshKind = 'fresh' | 'stale' | 'daily';

export interface QuoteFreshness {
  kind: QuoteFreshKind;
  /** 行情年龄（秒）；quantdb（daily）或时间戳缺失时为 null */
  ageSec: number | null;
  source: string;
}

/** epoch 秒/毫秒自适应解析（行情 timestamp 可能是 ISO 字符串或数字）。 */
export function parseQuoteTs(ts: unknown): number | null {
  if (ts === null || ts === undefined) return null;
  if (typeof ts === 'number') {
    if (!Number.isFinite(ts) || ts <= 0) return null;
    return ts < 1e12 ? ts * 1000 : ts;
  }
  const t = Date.parse(String(ts));
  return Number.isNaN(t) ? null : t;
}

/**
 * 单条行情 → 展示级新鲜度。
 * 判定顺序：日线兜底源 → 独立 kind；否则 时段内 且（服务端判 stale 或 年龄 > 60s）→ stale；
 * 其余 fresh（时间戳缺失且服务端没说 stale 时按 fresh——无从判断，不吓人）。
 */
export function classifyQuote(
  meta: QuoteMeta,
  nowMs: number,
  inSession: boolean = isCnTradingHours(),
): QuoteFreshness {
  if (meta.source === DAILY_FALLBACK_SOURCE) {
    return { kind: 'daily', ageSec: null, source: meta.source };
  }
  const ageSec = meta.tsMs !== null ? Math.max(0, Math.round((nowMs - meta.tsMs) / 1000)) : null;
  const stale = inSession && (meta.isStale || (ageSec !== null && ageSec > QUOTE_FRESH_WITHIN_S));
  return { kind: stale ? 'stale' : 'fresh', ageSec, source: meta.source };
}

/** 行情年龄 → 紧凑文案：秒 / 分 / 小时（徽标与 tooltip 共用）。 */
export function quoteAgeText(ageSec: number): string {
  if (ageSec < 90) return `${ageSec}s`;
  if (ageSec < 3600) return `${Math.floor(ageSec / 60)}min`;
  return `${(ageSec / 3600).toFixed(1)}h`;
}
