/**
 * 个股终端（港股）。
 *
 * 页面骨架、K 线卡、搜索框都在 stock-terminal-shared；本文件只声明港股主题、
 * 数据源配置与右侧详情体（HkDetailBody 自带 7 个 Tab）。
 */

import StockTerminalShell from '../../stock-terminal-shared/page/StockTerminalShell';
import { StockTerminalProvider, type TerminalTheme } from '../../stock-terminal-shared/adapter';
import { toPrefix } from '../../stock-terminal-shared/utils';
import { HkDetailBody } from '../components/HkDetailBody';

const HK_CONFIG = {
  klineMarket: 'HK',
  quoteMarket: 'HK',
  listMarket: 'HK',
  indexMaSymbol: 'HSI.HK',
} as const;

const HK_THEME: TerminalTheme = {
  title: '港股个股终端',
  accentFrom: 'from-blue-500',
  accentTo: 'to-violet-500',
  accentText: 'text-blue-600',
  klineCardBorder: 'border-purple-100/80',
  searchPlaceholder: '搜索港股代码 / 名称，如 0700 或 腾讯控股',
  searchHint: '支持代码（如 00700 / 0700.HK / 腾讯）、名称；不输入时不加载全量列表。',
  emptyTitle: '在上方搜索框输入代码或名称开始',
  emptyDesc: '不会预加载全量列表，输入关键词后联想最相关的 8 只股票；选中后左侧展示历史K线与默认模型推理分，右侧展示个股详情。',
  refLineLabel: '黄金线',
  klineBottomReserve: 26,
  sliderBottom: 2,
  sliderHeight: 16,
  adjusts: [
    { key: 'none', label: '不复权' },
    { key: 'qfq', label: '前复权' },
    { key: 'hfq', label: '后复权' },
  ],
  defaultAdjust: 'qfq',
};

export default function StockTerminalPage() {
  return (
    <StockTerminalProvider market="HK" theme={HK_THEME} config={HK_CONFIG} toWatchSymbol={toPrefix}>
      <StockTerminalShell
        renderDetail={(ctx) => <HkDetailBody symbol={ctx.symbol} name={ctx.name} close={ctx.close ?? undefined} />}
      />
    </StockTerminalProvider>
  );
}
