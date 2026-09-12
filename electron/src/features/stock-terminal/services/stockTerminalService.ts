/**
 * A 股个股终端服务实例。
 *
 * 实现是跨市场唯一的（stock-terminal-shared/service.ts）；本文件只提供 A 股的
 * 市场常量。原先三份各约 300 行的复制品差异只有十几行赋值，且已经漂移。
 */

import { StockTerminalService } from '../../stock-terminal-shared/service';

export const stockTerminalService = new StockTerminalService({
  klineMarket: 'A',
  quoteMarket: 'CN',
  indexMaSymbol: '000001.SH',
});

export * from '../../stock-terminal-shared/service';
