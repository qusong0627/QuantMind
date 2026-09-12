/**
 * 推理中心（美股）。
 *
 * 页面实现在 features/inference-center-shared（三市场共用）；本文件只声明美股的市场常量、
 * 代码联想源（美股终端标的池 /stock-terminal-us/list）与 K 线取数。
 *
 * 美股取数特意走**美股终端自己的端点**而不复用 A 股/港股的 `/research/kline`：
 * 后者只分了 `is_hk` 与 A 股两条分支，对美股会返回空 K 线（静默空图）。
 */

import { InferenceCenterProvider, type InferenceCenterAdapter } from '../features/inference-center-shared/adapter';
import { InferenceCenterShell } from '../features/inference-center-shared/pages/InferenceCenterShell';
import { usStockListService } from '../services/usStockListService';
import { stockTerminalService } from '../features/stock-terminal-us/services/stockTerminalService';

/**
 * 美股代码归一：大写去空白，容忍粘贴带 `.US` 后缀。
 * **不做 A 股式的前缀补全** —— ticker 是裸码，误用 SH/SZ 前缀规则会把 SHOP 改坏。
 */
function normalizeUsTicker(raw: string): string {
  return raw.trim().toUpperCase().replace(/\.US$/, '');
}

const US_ADAPTER: InferenceCenterAdapter = {
  market: 'US',
  marketLabel: '美股市场',
  calendar: 'NYSE',
  currencySymbol: '$',
  defaultSymbol: 'AAPL',
  searchPlaceholder: '输入代码/名称搜索 (如 AAPL 或 苹果)',

  preload: () => usStockListService.load(),
  search: async (kw) => {
    if (!usStockListService.isLoaded()) return [];
    return usStockListService.search(kw, 8).map((s) => ({ symbol: s.symbol, name: s.name }));
  },
  suggestionLabel: (s) => s.symbol,
  toSymbol: (s) => normalizeUsTicker(s.symbol),
  normalize: normalizeUsTicker,

  // 本地 QuantUS parquet（未复权原始价），与美股个股终端同源
  fetchKline: (symbol, days, _endDate, startDate) =>
    stockTerminalService.getDailyKline(symbol, days, 'none', startDate),
  // 美股 symbol 本身就是 `pred.parquet` 里的形式（AAPL / BRK.B 原样）
  toSuffixSymbol: (symbol) => normalizeUsTicker(symbol),
};

export const InferenceCenterUsPage = () => (
  <InferenceCenterProvider adapter={US_ADAPTER}>
    <InferenceCenterShell />
  </InferenceCenterProvider>
);

export default InferenceCenterUsPage;
