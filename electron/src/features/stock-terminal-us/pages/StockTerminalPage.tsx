/**
 * 个股终端（美股）。
 *
 * 页面骨架、K 线卡、搜索框都在 stock-terminal-shared；本文件只声明美股主题、
 * 数据源（/api/v1/stock-terminal-us，读本地 QuantUS parquet）与右侧详情体。
 *
 * 口径：日线是 yfinance 原始未复权价、amount 为美元原始成交额，因此复权切换只保留
 * 「不复权」，并用拆股事件在图上打橙色竖线解释价格跳变。
 */

import StockTerminalShell from '../../stock-terminal-shared/page/StockTerminalShell';
import { StockTerminalProvider, type TerminalTheme } from '../../stock-terminal-shared/adapter';
import { stockTerminalService } from '../services/stockTerminalService';
import { UsDetailBody } from '../components/UsDetailBody';

const US_THEME: TerminalTheme = {
  title: '美股个股终端',
  accentFrom: 'from-blue-500',
  accentTo: 'to-cyan-500',
  accentText: 'text-blue-600',
  klineCardBorder: 'border-blue-100/80',
  searchPlaceholder: '搜索美股代码 / 名称，如 AAPL 或 苹果',
  searchHint: '支持代码（如 AAPL / MSFT）与中文名（苹果 / 微软）；不输入时不加载全量列表。',
  emptyTitle: '在上方搜索框输入代码或名称开始',
  emptyDesc: '标的池为标普 500 + 纳指补充（约 500 只，非全市场）。选中后左侧展示历史K线，右侧展示估值、财务、分析师、内部人与机构持仓等美股特色面板。',
  refLineLabel: '参考线',
  klineBottomReserve: 26,
  sliderBottom: 2,
  sliderHeight: 16,
  // 库内日线本就是未复权原始价（yfinance auto_adjust=False），不提供前/后复权切换
  adjusts: [{ key: 'none', label: '不复权' }],
  defaultAdjust: 'none',
  klineNote: '未复权原始价 · 橙色竖线为拆股日',
};

export default function StockTerminalPage() {
  return (
    <StockTerminalProvider market="US" theme={US_THEME} service={stockTerminalService}>
      <StockTerminalShell renderDetail={(ctx) => <UsDetailBody symbol={ctx.symbol} />} />
    </StockTerminalProvider>
  );
}
