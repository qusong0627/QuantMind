/** 港股市场分析 · 共享 UI 小组件（卡片 / 涨跌读数 / 排行行 / 空态） */

import React from 'react';

/** 涨跌颜色文本（港股与 A 股同规则：红涨绿跌） */
export function PctText({
  value,
  suffix = '%',
  className = '',
}: {
  value: number | null | undefined;
  suffix?: string;
  className?: string;
}) {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return <span className={`text-slate-400 ${className}`}>--</span>;
  }
  const v = Number(value);
  const color = v > 0 ? 'text-red-600' : v < 0 ? 'text-green-600' : 'text-slate-500';
  const sign = v > 0 ? '+' : '';
  return (
    <span className={`font-extrabold font-mono ${color} ${className}`}>
      {sign}
      {v.toFixed(2)}
      {suffix}
    </span>
  );
}

/** 白底卡片容器（对齐 A 股市场分析卡片视觉） */
export function SectionCard({
  title,
  icon,
  extra,
  children,
  className = '',
}: {
  title: React.ReactNode;
  icon?: React.ReactNode;
  extra?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div
      className={`bg-white/90 backdrop-blur-md rounded-2xl p-4 border border-slate-200/80 shadow-sm flex flex-col gap-3 ${className}`}
    >
      <div className="flex items-center justify-between gap-2">
        <h3 className="text-xs font-extrabold text-slate-800 flex items-center gap-1.5">
          {icon}
          <span>{title}</span>
        </h3>
        {extra && <div className="flex items-center gap-2 flex-shrink-0">{extra}</div>}
      </div>
      {children}
    </div>
  );
}

/** 通用排行表行（表格行二三列对齐名/值） */
export function RankRow({
  rank,
  name,
  nameSub,
  main,
  right,
}: {
  rank: number;
  name: string;
  nameSub?: string;
  main: React.ReactNode;
  right?: React.ReactNode;
}) {
  return (
    <div className="flex items-center gap-2.5 px-1 py-1.5 border-b border-slate-50 last:border-0">
      <span
        className={`w-5 h-5 rounded-md grid place-items-center text-[10px] font-extrabold ${
          rank <= 3 ? 'bg-purple-600 text-white' : 'bg-slate-100 text-slate-500'
        }`}
      >
        {rank}
      </span>
      <div className="flex-1 min-w-0 flex items-center gap-2">
        <span className="text-xs font-bold text-slate-800 truncate">{name}</span>
        {nameSub && <span className="text-[10px] font-mono text-slate-400 truncate">{nameSub}</span>}
        {main}
      </div>
      {right}
    </div>
  );
}

/** 加载骨架/空态 */
export function EmptyHint({ text = '暂无数据', loading = false }: { text?: string; loading?: boolean }) {
  return (
    <div className="py-8 text-center text-xs text-slate-400 font-medium">
      {loading ? '加载中…' : text}
    </div>
  );
}

/** 周期 chip 切换 */
export function PeriodChips({
  options,
  value,
  onChange,
}: {
  options: Array<{ id: string; label: string }>;
  value: string;
  onChange: (id: string) => void;
}) {
  return (
    <div className="flex items-center gap-1 rounded-full bg-slate-100 p-0.5">
      {options.map((opt) => (
        <button
          key={opt.id}
          onClick={() => onChange(opt.id)}
          className={`px-3 py-1 rounded-full text-[11px] font-extrabold transition-all ${
            value === opt.id
              ? 'bg-white text-purple-700 shadow-2xs border border-purple-200'
              : 'text-slate-500 hover:text-slate-800'
          }`}
        >
          {opt.label}
        </button>
      ))}
    </div>
  );
}

/** 千分位数字 */
export function fmtInt(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return '--';
  return Number(n).toLocaleString('zh-CN', { maximumFractionDigits: 2 });
}