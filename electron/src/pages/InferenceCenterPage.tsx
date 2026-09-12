/**
 * 推理中心（A 股）。
 *
 * 页面实现在 features/inference-center-shared（三市场共用）；本文件只声明 A 股的
 * 市场常量、代码联想源与 K 线取数 —— 改共享层一处，三市场同时生效。
 */

import { InferenceCenterProvider, type InferenceCenterAdapter, type SuggestionItem } from '../features/inference-center-shared/adapter';
import { InferenceCenterShell } from '../features/inference-center-shared/pages/InferenceCenterShell';
import { inferenceCenterService } from '../services/inferenceCenterService';
import { stockListService, type Stock } from '../services/stockListService';
import { normalizeStockCode, toSuffixCode } from '../utils/portfolioUtils';

const toItem = (s: Stock): SuggestionItem => ({
  symbol: s.symbol,
  name: s.name,
  code: s.code,
  market: s.market,
});

const CN_ADAPTER: InferenceCenterAdapter = {
  market: 'CN',
  marketLabel: 'A股市场',
  calendar: 'SSE',
  currencySymbol: '¥',
  defaultSymbol: 'SH600519',
  searchPlaceholder: '输入代码/名称搜索 (如 600519 或 茅台)',

  preload: () => stockListService.load(),
  search: async (kw) => {
    if (!stockListService.isLoaded()) return [];
    const hit = stockListService.search(kw, 8);
    if (hit.length) return hit.map(toItem);
    // 输入是前缀式(如 SH600036)时，再按纯数字部分匹配一次本地 code 字段
    const digits = kw.replace(/^(SH|SZ|BJ)/i, '').replace(/[^\dA-Za-z]/g, '');
    if (!digits || digits === kw) return [];
    return stockListService.search(digits, 8).map(toItem);
  },
  suggestionLabel: (s) => `${s.market ?? ''}${s.code ?? ''}`,
  // 本地索引 symbol 为后缀式(600000.SH)，统一转前缀式(SH600000)
  toSymbol: (s) => normalizeStockCode(`${s.market ?? ''}${s.code ?? ''}`),
  normalize: (raw) => normalizeStockCode(raw),

  fetchKline: (symbol, days, endDate, startDate) =>
    inferenceCenterService.getStockKline(symbol, days, endDate, startDate),
  toSuffixSymbol: (symbol) => toSuffixCode(symbol),
};

export const InferenceCenterPage = () => (
  <InferenceCenterProvider adapter={CN_ADAPTER}>
    <InferenceCenterShell />
  </InferenceCenterProvider>
);

export default InferenceCenterPage;
