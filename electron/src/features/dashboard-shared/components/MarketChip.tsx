import React from 'react';

import { getMarketContent } from '../marketContent';
import type { AppMarket } from '../types';

interface MarketChipProps {
  market: AppMarket | string | undefined;
  /** 数据源标注（QuantDB / QuantHK …），给了就在徽标后补一个弱化小标 */
  source?: string;
  className?: string;
}

/**
 * 六宫格卡头统一的市场徽标。
 *
 * 存在意义：市场切换后，用户要能一眼看出「这一格是哪个市场的数」——
 * 之前只有标题里嵌市场名，切换后极易被忽略（历史问题：标题写港股、数字是 A 股）。
 */
export const MarketChip: React.FC<MarketChipProps> = ({ market, source, className = '' }) => {
  const content = getMarketContent(market);
  return (
    <span className={`inline-flex items-center gap-1.5 ${className}`}>
      <span
        className={`inline-flex items-center rounded-full border px-2 py-[1px] text-[10px] font-bold leading-4 ${content.accent}`}
        title={`当前市场：${content.label}`}
      >
        {content.label}
      </span>
      {source && (
        <span className="text-[10px] font-normal text-slate-400 leading-4" title="数据来源">
          {source}
        </span>
      )}
    </span>
  );
};
