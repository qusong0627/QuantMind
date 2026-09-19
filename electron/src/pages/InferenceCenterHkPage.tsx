/**
 * 推理中心（港股）。
 *
 * 页面实现在 features/inference-center-shared（三市场共用）；本文件只声明港股的市场常量、
 * 代码联想源（quanthk security_master 全市场名称表）与 K 线取数。
 */

import { InferenceCenterProvider, type InferenceCenterAdapter } from '../features/inference-center-shared/adapter';
import { InferenceCenterShell } from '../features/inference-center-shared/pages/InferenceCenterShell';
import { inferenceCenterService } from '../services/inferenceCenterService';
import { hkStockListService } from '../services/hkStockListService';
import { normalizeHkCode } from '../utils/marketSymbol';

const HK_ADAPTER: InferenceCenterAdapter = {
  market: 'HK',
  marketLabel: '港股市场',
  calendar: 'HKEX',
  currencySymbol: 'HK$',
  defaultSymbol: '0700.HK',
  searchPlaceholder: '输入代码/名称搜索 (如 00700 或 腾讯)',

  preload: () => hkStockListService.load(),
  search: async (kw) => {
    if (!hkStockListService.isLoaded()) return [];
    return hkStockListService.search(kw, 8).map((s) => ({ symbol: s.symbol, name: s.name }));
  },
  suggestionLabel: (s) => s.symbol,
  // 港股规范形态是 4 位 + .HK（0700.HK）。此处**不能**用 A 股口径的
  // normalizeStockCode：排名榜给的 4 位裸码（2057）会被原样透传，而手输 6 位
  // （000700）会被补成 SZ000700 —— 跨市场串号查出一只深市股票。
  toSymbol: (s) => normalizeHkCode(s.symbol),
  normalize: (raw) => normalizeHkCode(raw),

  fetchKline: (symbol, days, endDate, startDate) =>
    inferenceCenterService.getStockKline(symbol, days, endDate, startDate),
  toSuffixSymbol: (symbol) => normalizeHkCode(symbol),
};

export const InferenceCenterHkPage = () => (
  <InferenceCenterProvider adapter={HK_ADAPTER}>
    <InferenceCenterShell />
  </InferenceCenterProvider>
);

export default InferenceCenterHkPage;
