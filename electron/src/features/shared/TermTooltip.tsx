/**
 * 术语 Tooltip（T-FE-01）：包裹术语文本，hover 展示人话（简单模式）或人话+专业说明。
 *
 * 用法：<TermTooltip term="rank_pct">rank_pct</TermTooltip>
 * 纪律：term 必须存在于 glossary（覆盖度单测强校验）；未登记的 key 原样渲染（不拦 UI，
 * 但测试会红——修复方式是补 glossary，不是删工具提示）。
 */

import React from 'react';
import { Tooltip } from 'antd';
import { getTerm } from './glossary';
import { useUiMode } from './useUiMode';

interface TermTooltipProps {
  term: string;
  children?: React.ReactNode;
  className?: string;
}

export const TermTooltip: React.FC<TermTooltipProps> = ({ term, children, className }) => {
  const { isSimple } = useUiMode();
  const entry = getTerm(term);

  if (!entry) {
    return <span className={className}>{children ?? term}</span>;
  }

  const content = (
    <div className="max-w-[280px]">
      <div className="font-semibold">{entry.plain}</div>
      {!isSimple && (
        <div className="text-[11px] opacity-85 mt-1 leading-4">{entry.detail}</div>
      )}
    </div>
  );

  return (
    <Tooltip title={content} placement="top">
      <span
        className={`border-b border-dashed border-slate-300 cursor-help ${className || ''}`}
        data-term={term}
      >
        {children ?? term}
      </span>
    </Tooltip>
  );
};
