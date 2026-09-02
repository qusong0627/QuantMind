/** 大盘均线过滤卡：恒生指数 MA5/10/20/30/60 + 可持仓判断（随日历点选基准日联动） */

import { useEffect, useState } from 'react';
import { TrendingUp, TrendingDown } from 'lucide-react';
import { stockTerminalService, IndexMa } from '../services/stockTerminalService';

interface Props {
  /** 基准日（列表信号日），缺省最新交易日 */
  date?: string | null;
}

function fmt(v: number | null): string {
  return v != null ? v.toFixed(2) : '—';
}

const ROWS: { key: keyof Pick<IndexMa, 'ma5' | 'ma10' | 'ma20' | 'ma30' | 'ma60'>; label: string; hot?: boolean }[] = [
  { key: 'ma5', label: 'MA5' },
  { key: 'ma10', label: 'MA10' },
  { key: 'ma20', label: 'MA20', hot: true },
  { key: 'ma30', label: 'MA30' },
  { key: 'ma60', label: 'MA60' },
];

export function IndexMaCard({ date }: Props) {
  const [ma, setMa] = useState<IndexMa | null>(null);

  useEffect(() => {
    let cancelled = false;
    stockTerminalService.getIndexMa(date ?? undefined).then(m => {
      if (!cancelled) setMa(m);
    }).catch(() => { if (!cancelled) setMa(null); });
    return () => { cancelled = true; };
  }, [date]);

  if (!ma) {
    return (
      <div className="h-full flex flex-col items-center justify-center gap-1 text-[10px] text-slate-400">
        <TrendingUp className="w-4 h-4 opacity-40" />
        大盘均线加载中…
      </div>
    );
  }

  const up = ma.above_ma20;
  return (
    <div className="h-full flex flex-col gap-2 overflow-y-auto">
      {/* 标题：日期 · 恒生指数 + 持仓判断 */}
      <div className="flex items-center justify-between gap-1 shrink-0">
        <div className="flex items-center gap-1.5 min-w-0">
          <span className={`w-6 h-6 rounded-lg flex items-center justify-center shrink-0 ${up ? 'bg-rose-50 text-rose-500' : 'bg-emerald-50 text-emerald-500'}`}>
            {up ? <TrendingUp className="w-3.5 h-3.5" /> : <TrendingDown className="w-3.5 h-3.5" />}
          </span>
          <div className="min-w-0">
            <span className="text-[11px] font-black text-slate-700 block truncate">{ma.name}</span>
            <span className="text-[9px] text-slate-400 font-mono">{ma.trade_date}</span>
          </div>
        </div>
        <span className={`text-[10px] font-black shrink-0 ${up ? 'text-rose-500' : 'text-emerald-600'}`}>
          {up ? 'MA20 上方 · 可持仓' : 'MA20 下方 · 观望'}
        </span>
      </div>

      {/* 收盘 vs MA20 主数字 */}
      <div className="flex items-end justify-between shrink-0">
        <div>
          <span className="text-[9px] text-slate-400 font-bold">收盘</span>
          <span className="text-lg font-black font-mono text-slate-800 block leading-none">{fmt(ma.close)}</span>
        </div>
        <div className="text-right">
          <span className="text-[9px] text-slate-400 font-bold">MA20</span>
          <span className={`text-lg font-black font-mono block leading-none ${ma.close != null && ma.ma20 != null ? (ma.close > ma.ma20 ? 'text-rose-500' : 'text-emerald-600') : 'text-slate-500'}`}>
            {fmt(ma.ma20)}
          </span>
        </div>
      </div>

      {/* MA5-60 列表（MA20 行突出） */}
      <div className="flex flex-col gap-0.5 shrink-0">
        {ROWS.map(r => (
          <div key={r.key} className={`flex items-center justify-between px-1.5 py-0.5 rounded-md ${r.hot ? 'bg-blue-50' : ''}`}>
            <span className={`text-[9px] font-bold ${r.hot ? 'text-blue-600' : 'text-slate-400'}`}>{r.label}</span>
            <span className={`text-[10px] font-mono font-bold ${r.hot ? 'text-blue-600' : 'text-slate-600'}`}>{fmt(ma[r.key])}</span>
          </div>
        ))}
      </div>

      {/* 结论文案 */}
      <span className={`text-[9px] font-bold px-1.5 py-1 rounded-md shrink-0 ${up ? 'bg-rose-50 text-rose-500' : 'bg-emerald-50 text-emerald-600'}`}>
        {ma.status}
      </span>
    </div>
  );
}
