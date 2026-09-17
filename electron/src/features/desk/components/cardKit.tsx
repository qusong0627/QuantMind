/** 交易台卡片通用件：卡壳 / 卡头 / 微统计块（浅色专业金融风统一口径） */

import React from 'react';

export const CARD =
  'bg-white rounded-2xl border border-slate-200/80 shadow-[0_1px_2px_rgba(15,23,42,0.04)] p-4 flex flex-col';

/** 卡头：图标色块 + 标题 + 右侧扩展位 */
export const CardHeader: React.FC<{
  icon: React.ReactNode;
  title: string;
  meta?: React.ReactNode;
  extra?: React.ReactNode;
}> = ({ icon, title, meta, extra }) => (
  <header className="flex items-center gap-2 mb-3">
    <span className="flex h-7 w-7 items-center justify-center rounded-lg bg-blue-50 text-blue-600 shrink-0">
      {icon}
    </span>
    <h3 className="text-[13px] font-semibold text-slate-800 tracking-wide">{title}</h3>
    {meta}
    {extra && <span className="ml-auto shrink-0">{extra}</span>}
  </header>
);

export const TONES = {
  red: 'bg-red-50/70 border-red-100 text-red-600',
  green: 'bg-emerald-50/70 border-emerald-100 text-emerald-600',
  slate: 'bg-slate-50 border-slate-200/70 text-slate-600',
  amber: 'bg-amber-50/70 border-amber-100 text-amber-600',
  blue: 'bg-blue-50/70 border-blue-100 text-blue-600',
} as const;

/** 微统计块（卡片头部计数） */
export const StatTile: React.FC<{
  label: string;
  value: React.ReactNode;
  tone: keyof typeof TONES;
  className?: string;
}> = ({ label, value, tone, className = '' }) => (
  <div className={`rounded-xl border px-2.5 py-1.5 ${TONES[tone]} ${className}`}>
    <div className="text-[10px] font-semibold tracking-wide opacity-70">{label}</div>
    <div className="text-lg font-bold font-mono tabular-nums leading-6">{value ?? '—'}</div>
  </div>
);
