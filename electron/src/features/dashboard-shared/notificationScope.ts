/**
 * 站内通知的市场分流（纯函数，便于单测）。
 *
 * 背景：`notifications` 表**没有 market 列**，`/api/v1/notifications?market=` 参数被忽略，
 * 所以本市场分流只能在端上按内容里的标的代码做：
 * - 通知里出现**明确的**标的代码（600000.SH / 00700.HK / BTCUSDT / RB2601.CN）→ 归该市场
 * - 没有任何标的代码（如「回测已完成」「系统维护」）→ 归「全局」
 *
 * 为什么不猜「大写单词即 ticker」：通知里 REAL-TIME / TOTAL 这类词很常见，
 * 猜错会把全局通知误判成某个市场。宁可少分，不可错分。
 * 后端补上 `notifications.market` 列后，本文件退化为兜底（见方案 B3）。
 */

import { extractSymbolFromText, inferMarketOfSymbol } from '../../utils/marketInfer';

export interface ScopedNotification {
  title?: string;
  content?: string;
  action_url?: string;
}

export interface NotificationBucket<T extends ScopedNotification> {
  /** 与当前市场相关的通知 */
  market: T[];
  /** 与市场无关的全局通知（系统/回测/账户类） */
  global: T[];
}

export function splitNotificationsByMarket<T extends ScopedNotification>(
  items: T[],
  market: string | null | undefined,
): NotificationBucket<T> {
  const target = String(market || '').toUpperCase().trim();
  if (!target) return { market: [], global: Array.isArray(items) ? items : [] };

  const bucket: NotificationBucket<T> = { market: [], global: [] };
  for (const item of Array.isArray(items) ? items : []) {
    const symbol = extractSymbolFromText(
      `${item?.title || ''} ${item?.content || ''} ${item?.action_url || ''}`,
    );
    if (symbol && inferMarketOfSymbol(symbol) === target) {
      bucket.market.push(item);
    } else if (!symbol) {
      bucket.global.push(item);
    }
    // 有代码但不属于当前市场 → 两边都不进（就是「别的市场的通知」）
  }
  return bucket;
}
