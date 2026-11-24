import React from 'react';
import { message } from 'antd';
import dayjs from 'dayjs';
import { clsx } from 'clsx';
import { Copy } from 'lucide-react';

export const MARKET_LABELS: Record<string, string> = {
  CN: 'A股',
  HK: '港股',
  US: '美股',
  CRYPTO: '区块链',
  FUTURES: '期货',
  CUSTOM: '自定义市场',
};

export const RUN_STATUS: Record<string, { label: string; chip: string; dot: string }> = {
  completed: {
    label: '已完成',
    chip: 'bg-emerald-50 text-emerald-700 ring-emerald-200',
    dot: 'bg-emerald-500',
  },
  failed: { label: '失败', chip: 'bg-rose-50 text-rose-700 ring-rose-200', dot: 'bg-rose-500' },
  cancelled: { label: '已取消', chip: 'bg-slate-100 text-slate-500 ring-slate-200', dot: 'bg-slate-400' },
  running: { label: '训练中', chip: 'bg-sky-50 text-sky-700 ring-sky-200', dot: 'bg-sky-500 animate-pulse' },
  provisioning: { label: '准备中', chip: 'bg-indigo-50 text-indigo-700 ring-indigo-200', dot: 'bg-indigo-500 animate-pulse' },
  pending: { label: '排队中', chip: 'bg-amber-50 text-amber-700 ring-amber-200', dot: 'bg-amber-500' },
  waiting_callback: { label: '等待回调', chip: 'bg-cyan-50 text-cyan-700 ring-cyan-200', dot: 'bg-cyan-500' },
};

export const statusMeta = (status?: string) =>
  RUN_STATUS[status || ''] || {
    label: status || '未知',
    chip: 'bg-slate-100 text-slate-500 ring-slate-200',
    dot: 'bg-slate-400',
  };

export const fmtTime = (value?: string | null): string =>
  value ? dayjs(value).format('YYYY-MM-DD HH:mm') : '—';

export const fmtNum = (value: number | null | undefined, digits = 4): string =>
  value === null || value === undefined || !Number.isFinite(Number(value))
    ? '—'
    : Number(value).toFixed(digits);

export const copyText = async (text: string, label = '已复制') => {
  try {
    await navigator.clipboard.writeText(text);
    message.success(label);
  } catch {
    message.warning('复制失败，请手动选择文本');
  }
};

export const Shell: React.FC<{
  children: React.ReactNode;
  className?: string;
  icon?: React.ReactNode;
  title?: React.ReactNode;
  hint?: React.ReactNode;
  action?: React.ReactNode;
  bodyClassName?: string;
}> = ({ children, className, icon, title, hint, action, bodyClassName }) => (
  <section
    className={clsx(
      'overflow-hidden rounded-2xl border border-slate-200/80 bg-white shadow-[0_1px_2px_rgba(15,23,42,0.04)]',
      className,
    )}
  >
    {title ? (
      <header className="flex items-start justify-between gap-3 border-b border-slate-100 px-4 py-2.5">
        <div className="flex min-w-0 items-start gap-2">
          {icon ? <span className="mt-0.5 shrink-0 text-indigo-500">{icon}</span> : null}
          <div className="min-w-0">
            <h3 className="text-[12px] font-black tracking-wide text-slate-800">{title}</h3>
            {hint ? <p className="mt-0.5 text-[10px] leading-relaxed text-slate-400">{hint}</p> : null}
          </div>
        </div>
        {action ? <div className="shrink-0">{action}</div> : null}
      </header>
    ) : null}
    <div className={clsx('px-4 py-3', bodyClassName)}>{children}</div>
  </section>
);

export const Fact: React.FC<{
  label: string;
  children: React.ReactNode;
  mono?: boolean;
  className?: string;
}> = ({ label, children, mono = false, className }) => (
  <div className={clsx('grid grid-cols-[78px_minmax(0,1fr)] items-start gap-2 border-b border-slate-100/80 py-1.5 last:border-0', className)}>
    <span className="pt-px text-[10px] font-bold text-slate-400">{label}</span>
    <span className={clsx('min-w-0 break-all text-[11px] leading-relaxed text-slate-700', mono && 'font-mono')}>
      {children}
    </span>
  </div>
);

export const CopyValue: React.FC<{ value?: string | null; display?: string; className?: string }> = ({
  value,
  display,
  className,
}) => {
  if (!value) return <span className="text-slate-400">—</span>;
  return (
    <button
      type="button"
      onClick={() => void copyText(value, '已复制')}
      title="点击复制"
      className={clsx(
        'group inline-flex max-w-full items-center gap-1 rounded px-1 py-0.5 text-left font-mono text-[11px] text-slate-700 transition-colors hover:bg-indigo-50 hover:text-indigo-700',
        className,
      )}
    >
      <span className="truncate">{display ?? value}</span>
      <Copy size={10} className="shrink-0 text-slate-300 group-hover:text-indigo-400" />
    </button>
  );
};

export const MetaChip: React.FC<{ icon?: React.ReactNode; children: React.ReactNode }> = ({ icon, children }) => (
  <span className="inline-flex items-center gap-1 rounded-md border border-slate-200/80 bg-white px-1.5 py-0.5 text-[10px] font-bold text-slate-500">
    {icon}
    {children}
  </span>
);

export const StatTile: React.FC<{
  label: string;
  value: string;
  hint?: string;
  tone?: string;
  accent?: string;
}> = ({ label, value, hint, tone = 'text-slate-800', accent = 'bg-slate-300' }) => (
  <div className="relative min-w-0 px-3 py-2 first:pl-0 last:pr-0">
    <span className={clsx('absolute left-0 top-2 h-6 w-0.5 rounded-full', accent)} />
    <div className="pl-2">
      <div className="text-[8px] font-black uppercase tracking-[0.16em] text-slate-400">{label}</div>
      <div className={clsx('mt-0.5 truncate font-mono text-[15px] font-black tabular-nums leading-tight', tone)}>
        {value}
      </div>
      {hint ? <div className="mt-0.5 truncate text-[9px] text-slate-400">{hint}</div> : null}
    </div>
  </div>
);
