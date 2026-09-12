/**
 * 港股个股终端服务实例。
 *
 * 实现是跨市场唯一的（stock-terminal-shared/service.ts）；本文件只提供港股的市场常量。
 * 顺带修掉了原先港股副本遗漏的 resolveWebSafeServiceBase（Web 端相对路径兼容）——
 * 那正是「复制粘贴副本各自漂移」的典型症状。
 */

import { StockTerminalService } from '../../stock-terminal-shared/service';

export const stockTerminalService = new StockTerminalService({
  klineMarket: 'HK',
  quoteMarket: 'HK',
  listMarket: 'HK',
  indexMaSymbol: 'HSI.HK',
});

export * from '../../stock-terminal-shared/service';
