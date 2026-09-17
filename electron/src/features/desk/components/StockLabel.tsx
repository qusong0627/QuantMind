/**
 * 标的展示（名称为主、代码辅）：交易台列表统一口径。
 *
 * 「同仁堂 600085」——名称为正常字号，代码浅灰小字等宽；
 * 后端未收录名称（name 缺省/空串）时回退只显示代码（不伪造名称）。
 */

import React from 'react';

interface StockLabelProps {
  symbol: string;
  name?: string | null;
  /** 代码是否展示（默认展示；下钻弹窗等窄场景仍保留，便于核对） */
  showCode?: boolean;
  className?: string;
}

export const StockLabel: React.FC<StockLabelProps> = ({
  symbol,
  name,
  showCode = true,
  className = '',
}) => {
  const displayName = String(name || '').trim();
  if (!displayName) {
    return (
      <span className={`text-[12px] font-mono font-medium text-slate-700 truncate ${className}`}>
        {symbol}
      </span>
    );
  }
  return (
    <span className={`inline-flex items-baseline gap-1.5 min-w-0 ${className}`}>
      <span className="text-[13px] font-medium text-slate-800 truncate">{displayName}</span>
      {showCode && <span className="text-[11px] font-mono text-slate-400 shrink-0">{symbol}</span>}
    </span>
  );
};
