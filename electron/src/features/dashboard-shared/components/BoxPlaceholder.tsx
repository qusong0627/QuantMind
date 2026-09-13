import React from 'react';
import { AlertCircle, Inbox, Loader2, RefreshCw, Wallet } from 'lucide-react';

import type { BoxContent, BoxDataState } from '../types';

interface BoxPlaceholderProps {
  content: BoxContent;
  state: BoxDataState;
  /** 失败态重试 */
  onRetry?: () => void;
  /** 未开通时的一键开通（资金/成交类卡片传入） */
  onOpenAccount?: () => void;
  opening?: boolean;
}

/**
 * 卡片数据态占位（加载 / 失败 / 未开通 / 空）。
 *
 * 这里统一「空就是空」的口径：任何市场缺数据都走本组件的空态，
 * 不允许回落到其它市场的数据（历史问题：港股卡片显示 A 股账户余额）。
 */
export const BoxPlaceholder: React.FC<BoxPlaceholderProps> = ({
  content,
  state,
  onRetry,
  onOpenAccount,
  opening = false,
}) => {
  const { loading, hasData, notInitialized, error } = state;
  if (hasData) return null;

  if (loading) {
    return (
      <div className="flex-1 min-h-[120px] flex flex-col items-center justify-center gap-2 text-slate-400">
        <Loader2 size={18} className="animate-spin" />
        <span className="text-xs">加载中…</span>
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex-1 min-h-[120px] flex flex-col items-center justify-center gap-2 text-center px-4">
        <AlertCircle size={18} className="text-[var(--error)]" />
        <span className="text-xs font-bold text-slate-700">数据加载失败</span>
        <span className="text-[11px] text-slate-400 leading-4 break-all">{String(error).slice(0, 80)}</span>
        {onRetry && (
          <button
            onClick={onRetry}
            className="mt-1 inline-flex items-center gap-1 rounded-full border border-slate-200 bg-white px-3 py-1 text-[11px] font-bold text-slate-600 hover:bg-slate-50"
          >
            <RefreshCw size={11} />
            重试
          </button>
        )}
      </div>
    );
  }

  const Icon = notInitialized ? Wallet : Inbox;
  const canOpen = Boolean(notInitialized && content.canOpenAccount && onOpenAccount);

  return (
    <div className="flex-1 min-h-[120px] flex flex-col items-center justify-center gap-2 text-center px-4">
      <Icon size={18} className="text-slate-300" />
      <span className="text-xs font-bold text-slate-600">{content.emptyTitle}</span>
      <span className="text-[11px] text-slate-400 leading-4">{content.emptyHint}</span>
      {canOpen && (
        <button
          onClick={onOpenAccount}
          disabled={opening}
          className="mt-1 inline-flex items-center gap-1.5 rounded-full bg-gradient-to-r from-indigo-600 to-sky-600 px-3.5 py-1.5 text-[11px] font-extrabold text-white shadow-sm transition-all hover:from-indigo-500 hover:to-sky-500 active:scale-95 disabled:cursor-wait disabled:opacity-70"
        >
          {opening ? <Loader2 size={12} className="animate-spin" /> : <Wallet size={12} />}
          {opening ? '开通中…' : '开通模拟盘'}
        </button>
      )}
    </div>
  );
};
