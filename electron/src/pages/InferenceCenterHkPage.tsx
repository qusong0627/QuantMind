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
import { normalizeStockCode } from '../utils/portfolioUtils';

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
  toSymbol: (s) => normalizeStockCode(s.symbol),
  normalize: (raw) => normalizeStockCode(raw),

  fetchKline: (symbol, days, endDate, startDate) =>
    inferenceCenterService.getStockKline(symbol, days, endDate, startDate),
  // 港股代码本身就是后缀式（0700.HK），无需转换
  toSuffixSymbol: (symbol) => normalizeStockCode(symbol),
};

export const InferenceCenterHkPage = () => (
  <InferenceCenterProvider adapter={HK_ADAPTER}>
    <InferenceCenterShell />
  </InferenceCenterProvider>
);

export default InferenceCenterHkPage;
