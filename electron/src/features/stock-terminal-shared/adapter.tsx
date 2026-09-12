/**
 * 个股终端跨市场适配层。
 *
 * 三个市场的终端页面结构完全一致（顶栏搜索 → 左 K 线 + 推理分副图 → 右详情），
 * 差异只有：主题文案、市场常量、复权选项、自选表代码格式、右侧详情体组件。
 * 共享组件通过 useStockTerminal() 取这些差异，不再各自 import 一份市场服务。
 */

import { createContext, useContext, useMemo, type ReactNode } from 'react';
import { StockTerminalService, type KlineAdjust, type TerminalMarketConfig } from './service';

/** 终端主题与文案（各市场不同，其余组件逻辑共用） */
export interface TerminalTheme {
  /** 页面标题 */
  title: string;
  /** 图标底色渐变（Tailwind 类名） */
  accentFrom: string;
  accentTo: string;
  /** 主色文字（Tailwind 类名） */
  accentText: string;
  /** K 线卡边框色 */
  klineCardBorder: string;
  /** 搜索框占位文案 */
  searchPlaceholder: string;
  /** 搜索框空态提示 */
  searchHint: string;
  /** 空态主文案 */
  emptyTitle: string;
  /** 空态说明 */
  emptyDesc: string;
  /** K 线默认参考线标签 */
  refLineLabel: string;
  /** K 线图底部为缩放条预留的高度 */
  klineBottomReserve: number;
  /** 缩放条底距与高度 */
  sliderBottom: number;
  sliderHeight: number;
  /** 可用的复权选项与默认值（美股/港股库内只有未复权价，只给 none） */
  adjusts: { key: KlineAdjust; label: string }[];
  defaultAdjust: KlineAdjust;
  /** 数据口径提示（展示在 K 线卡头部，可空） */
  klineNote?: string;
}

export interface StockTerminalAdapter {
  market: 'CN' | 'HK' | 'US';
  theme: TerminalTheme;
  service: StockTerminalService;
  /**
   * 自选表代码格式：A 股/港股用 prefix（SH600519）；美股用裸 ticker（AAPL）。
   */
  toWatchSymbol(symbol: string): string;
}

const TerminalContext = createContext<StockTerminalAdapter | null>(null);

export function StockTerminalProvider({
  market,
  theme,
  config,
  service,
  toWatchSymbol,
  children,
}: {
  market: StockTerminalAdapter['market'];
  theme: TerminalTheme;
  /** 数据源配置；与 service 二选一（传 service 时忽略） */
  config?: TerminalMarketConfig;
  /** 预构造的服务实例（市场需要子类扩展时用，如美股的 /detail 与拆股标记） */
  service?: StockTerminalService;
  toWatchSymbol?: (symbol: string) => string;
  children: ReactNode;
}) {
  const adapter = useMemo<StockTerminalAdapter>(
    () => ({
      market,
      theme,
      service: service ?? new StockTerminalService(config as TerminalMarketConfig),
      toWatchSymbol: toWatchSymbol ?? ((s: string) => s),
    }),
    [market, theme, config, service, toWatchSymbol],
  );
  return <TerminalContext.Provider value={adapter}>{children}</TerminalContext.Provider>;
}

export function useStockTerminal(): StockTerminalAdapter {
  const ctx = useContext(TerminalContext);
  if (!ctx) throw new Error('useStockTerminal 必须在 <StockTerminalProvider> 内使用');
  return ctx;
}
