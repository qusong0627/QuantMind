/**
 * 个股终端（A 股）。
 *
 * 页面骨架、K 线卡、搜索框都在 stock-terminal-shared；本文件只声明 A 股主题、
 * 数据源配置与右侧详情体。改共享层一处，三市场同时生效。
 */

import { Info } from 'lucide-react';
import StockTerminalShell from '../../stock-terminal-shared/page/StockTerminalShell';
import { StockTerminalProvider, type TerminalTheme } from '../../stock-terminal-shared/adapter';
import { toPrefix } from '../../stock-terminal-shared/utils';
import { CnDetailBody } from '../components/CnDetailBody';

const CN_CONFIG = {
  klineMarket: 'A',
  quoteMarket: 'CN',
  indexMaSymbol: '000001.SH',
} as const;

const CN_THEME: TerminalTheme = {
  title: '个股终端',
  accentFrom: 'from-blue-500',
  accentTo: 'to-violet-500',
  accentText: 'text-blue-600',
  klineCardBorder: 'border-purple-100/80',
  searchPlaceholder: '搜索股票代码 / 名称',
  searchHint: '支持代码、名称、拼音首字母；不输入时不加载全量列表。',
  emptyTitle: '在上方搜索框输入代码或名称开始',
  emptyDesc: '不会预加载全量列表，输入关键词后联想最相关的 8 只股票；选中后左侧展示历史K线与默认模型推理分，右侧展示个股详情。',
  refLineLabel: '参考线',
  klineBottomReserve: 52,
  sliderBottom: 22,
  sliderHeight: 20,
  adjusts: [
    { key: 'none', label: '不复权' },
    { key: 'qfq', label: '前复权' },
    { key: 'hfq', label: '后复权' },
  ],
  defaultAdjust: 'qfq',
};

export default function StockTerminalPage() {
  return (
    <StockTerminalProvider market="CN" theme={CN_THEME} config={CN_CONFIG} toWatchSymbol={toPrefix}>
      <StockTerminalShell
        renderDetail={(ctx) => <CnDetailBody symbol={ctx.symbol} profile={ctx.profile} signalDate={ctx.signalDate} />}
        renderBanner={() => (
          <div className="mx-0 px-4 py-2 bg-gradient-to-r from-blue-50/60 via-violet-50/40 to-transparent border-b border-slate-100 flex items-center gap-2.5">
            <div className="w-6 h-6 rounded-lg bg-blue-500/10 flex items-center justify-center shrink-0">
              <Info className="w-3.5 h-3.5 text-blue-500" />
            </div>
            <p className="text-xs leading-none text-slate-600 flex-1">
              <span className="font-bold text-slate-700">模型推理分说明：</span>
              基于历史行情与模型权重离线推导，用于刻画个股在全市场截面中的<span className="font-semibold text-slate-700">相对收益与风险分位</span>；与 K 线价格绝对走势相反，受全市场分布与波动共同影响，<span className="font-semibold text-slate-700">不以绝对值论高低</span>，宜作横向对比与趋势参考。
            </p>
          </div>
        )}
      />
    </StockTerminalProvider>
  );
}
