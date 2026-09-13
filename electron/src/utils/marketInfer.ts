/**
 * 标的代码 → 市场 推断（前端唯一实现）
 *
 * 与后端 `backend/services/simulation/services/market_rules.py::infer_market`
 * **同口径**，用于「接口没有市场字段、只能看代码形态」的场景：
 * - 成交/订单列表按市场过滤（表里没有 market 列）
 * - 站内通知按市场分流
 *
 * 改这里的判据之前，先同步改后端 market_rules（两边必须一致，否则前端过滤与
 * 引擎推断会给出两套市场归属）。
 *
 * 历史：本函数原为 `pages/trading/tabs/TradingHistory.tsx` 内的局部实现，
 * 首页六宫格接入市场维度时抽到公共工具，避免第三份副本。
 */

/** 与 uiSlice 的 AppMarket 对齐（此处不 import store，保持工具层零依赖） */
export type InferredMarket = 'CN' | 'HK' | 'US' | 'CRYPTO' | 'FUTURES';

const HK_RE = /^\d{1,5}\.HK$/;
const CN_SUFFIX_RE = /^\d{6}\.(SH|SZ|BJ)$/;
const CN_PREFIX_RE = /^(SH|SZ|BJ)\d{6}$/;
const CN_NUMERIC_RE = /^\d{6}$/;
const FUTURES_RE = /\.(CN|FUT)$/;
const FUTURES_SHFE_RE = /^[A-Z]{2}\d{2}\.\d{2}$/;
const CRYPTO_RE = /^[A-Z0-9]+USDT$/;
const US_TICKER_RE = /^[A-Z]{1,6}(\.[A-Z]{1,2})?$/;

/**
 * 由代码形态推断市场。判据**互斥且精确**：
 * 按「前缀即市场」的粗略写法会把美股 SHOP/SHW 当成上交所。
 * 无法判定时回退 `CN`（与后端 infer_market 的兜底一致）。
 *
 * 注意：裸 4-5 位数字（如 `0700`）**按 CN 兜底** —— 后端 infer_market 就是这么兜的，
 * 港股落库前会归一成 `0700.HK` 后缀式，不需要在这里为裸数字破例（破例会与后端漂移）。
 */
export function inferMarketOfSymbol(symbol: string | null | undefined): InferredMarket {
  const s = String(symbol || '').toUpperCase().trim();
  if (!s) return 'CN';
  if (HK_RE.test(s)) return 'HK';
  if (FUTURES_RE.test(s) || s.includes('(T+D)') || FUTURES_SHFE_RE.test(s)) return 'FUTURES';
  if (CN_SUFFIX_RE.test(s) || CN_PREFIX_RE.test(s) || CN_NUMERIC_RE.test(s)) return 'CN';
  if (CRYPTO_RE.test(s)) return 'CRYPTO';
  if (US_TICKER_RE.test(s)) return 'US';
  return 'CN';
}

/** 该标的是否属于指定市场（market 为空 = 全部市场放行） */
export function symbolMatchesMarket(symbol: string | null | undefined, market?: string | null): boolean {
  if (!market) return true;
  return inferMarketOfSymbol(symbol) === String(market).toUpperCase();
}

/** 按市场过滤任意带 symbol 字段的列表（market 为空时原样返回） */
export function filterByMarket<T extends { symbol?: string | null }>(
  items: T[],
  market?: string | null,
): T[] {
  if (!market) return items;
  return items.filter((item) => symbolMatchesMarket(item?.symbol, market));
}

/**
 * 从任意文本里抽取疑似标的代码（站内通知用：标题/正文/跳转链接里常带 600000.SH、00700.HK、BTCUSDT）。
 *
 * **只认明确的代码形态**，不做「大写单词即 ticker」的猜测 ——
 * 通知里出现 REAL-TIME / TOTAL 这类英文词很常见，猜错会把全局通知误判成某个市场。
 * 找不到返回 null，调用方按「全局通知」处理。
 */
export function extractSymbolFromText(text: string | null | undefined): string | null {
  const s = String(text || '').toUpperCase();
  if (!s) return null;
  const patterns = [
    /\b\d{6}\.(?:SH|SZ|BJ)\b/,        // A 股后缀式
    /\b(?:SH|SZ|BJ)\d{6}\b/,          // A 股前缀式
    /\b\d{1,5}\.HK\b/,                // 港股
    /\b[A-Z0-9]{2,10}USDT\b/,         // 加密
    /\b[A-Z]{1,3}\d{3,4}\.(?:CN|FUT)\b/, // 期货（RB2601.CN / CL.FUT 类）
    /(?:NASDAQ|NYSE|AMEX|美股)\s*[:：]?\s*([A-Z]{1,5})\b/, // 明确标注交易所的美股 ticker
  ];
  for (const re of patterns) {
    const m = s.match(re);
    if (m) {
      const hit = (m[1] || m[0]).trim();
      if (hit) return hit;
    }
  }
  return null;
}
