/**
 * 推理中心跨市场适配层。
 *
 * 三个市场的页面结构与状态机完全一致（顶栏 → 截面推理 / 个股预测两个顶层 Tab），
 * 差异只有：市场常量、货币符号、默认标的、交易日历、代码联想源、K 线取数来源。
 * 共享的 InferenceCenterShell 通过 useInferenceCenter() 取这些差异，
 * 市场特有的东西一律在各自目录里实现，共享组件里不写市场分支。
 */

import { createContext, useContext, useMemo, type ReactNode } from 'react';
import type { KlineItem } from '../../services/inferenceCenterService';

/** 代码联想项：CN 的 Stock 与港股/美股的 {symbol,name} 归一成这个形状 */
export interface SuggestionItem {
  /** 归一后的标的代码（CN: 600519.SH / HK: 0700.HK / US: AAPL） */
  symbol: string;
  name?: string;
  /** 仅 CN 用：纯代码与交易所前缀 */
  code?: string;
  market?: string;
}

export interface InferenceCenterAdapter {
  market: 'CN' | 'HK' | 'US';
  /** 顶栏 Tag 文案，如「A股市场」 */
  marketLabel: string;
  /** 交易日历代码，直接传给 /market-calendar/*（SSE / HKEX / NYSE） */
  calendar: string;
  /** 货币符号：¥ / HK$ / $ */
  currencySymbol: string;
  /** 个股预测的默认标的（CN: SH600519 / HK: 0700.HK / US: AAPL） */
  defaultSymbol: string;
  /** 个股预测输入框的占位提示（各市场举例不同） */
  searchPlaceholder: string;

  /** 预加载联想数据源（CN 读本地静态表、HK 拉名称表、US 拉标的池）；失败应静默降级 */
  preload(): Promise<void>;
  /** 内存/远端搜索联想项 */
  search(keyword: string): Promise<SuggestionItem[]>;
  /** 联想项展示文案（CN 显示「SH600519」，其余显示 symbol） */
  suggestionLabel(item: SuggestionItem): string;
  /** 联想项 → 归一代码 */
  toSymbol(item: SuggestionItem): string;
  /** 用户手输的代码 → 归一代码 */
  normalize(raw: string): string;

  /** 个股 K 线（CN/HK 走 /research/kline，US 走 /stock-terminal-us/kline） */
  fetchKline(symbol: string, days: number, endDate?: string, startDate?: string): Promise<KlineItem[]>;
  /** 多模型分数曲线需要的后缀式代码（CN: 600519.SH；US: 原样 ticker） */
  toSuffixSymbol(symbol: string): string;
}

const Ctx = createContext<InferenceCenterAdapter | null>(null);

export function InferenceCenterProvider({
  adapter,
  children,
}: {
  adapter: InferenceCenterAdapter;
  children: ReactNode;
}) {
  const value = useMemo(() => adapter, [adapter]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useInferenceCenter(): InferenceCenterAdapter {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error('useInferenceCenter 必须在 <InferenceCenterProvider> 内使用');
  return ctx;
}
