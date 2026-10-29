/**
 * 资金流速率卡：当前窗口日均净额 vs 前一窗口日均净额。
 *
 * 口径：加速度 = 当前 N 日日均主力净额 − 前一 N 日日均（元/日）；
 * 正=加速流入，负=加速流出。速率只看“变化方向/快慢”，不单独构成买卖建议。
 */
import React, { useMemo, useState } from 'react';
import { Gauge, TrendingDown, TrendingUp } from 'lucide-react';
import type { FlowPeriod, FlowSpeedRow } from '../services/moneyFlowService';

const fmtDaily = (v: number | null | undefined): string => {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  return `${v >= 0 ? '+' : ''}${(v / 1e8).toFixed(2)}亿/日`;
};
const fmtRatio = (v: number | null | undefined): string =>
  v === null || v === undefined ? '—' : `${(v * 100).toFixed(2)}%`;

const Row: React.FC<{
  row: FlowSpeedRow;
  kind: 'sector' | 'stock';
  onSelectSector?: (name: string) => void;
  onSelectStock?: (symbol: string, name: string) => void;
}> = ({ row, kind, onSelectSector, onSelectStock }) => {
  const up = (row.accel ?? 0) >= 0;
  const click = () => {
    if (kind === 'sector' && row.name) onSelectSector?.(row.name);
    if (kind === 'stock' && row.symbol) onSelectStock?.(row.symbol, row.name ?? '');
  };
  return (
    <button
      onClick={click}
      className="flex items-center gap-1.5 py-0.5 text-left hover:bg-slate-50 rounded px-1 -mx-1 transition-colors w-full"
      title={`当前日均 ${fmtDaily(row.net_daily)} · 前一日均 ${fmtDaily(row.prev_net_daily)} · 净额比 ${fmtRatio(row.net_ratio)}（前 ${fmtRatio(row.prev_net_ratio)}）`}
    >
      <span className="text-[10px] font-mono text-slate-400 w-10 shrink-0">
        {row.symbol ?? ''}
      </span>
      <span className="text-[10px] font-bold text-slate-700 truncate flex-1">
        {row.name || '—'}
        {row.sector && <span className="ml-1 text-[8px] text-slate-400">{row.sector}</span>}
      </span>
      <span className="text-[9px] font-mono text-slate-400 w-14 text-right">
        {fmtDaily(row.net_daily)}
      </span>
      <span className={`text-[10px] font-mono font-extrabold w-16 text-right ${up ? 'text-red-600' : 'text-emerald-600'}`}>
        {(() => {
          const yi = (row.accel ?? 0) / 1e8;
          return `${yi > 0 ? '+' : ''}${yi.toFixed(2)}亿`;
        })()}
      </span>
    </button>
  );
};

export const FlowSpeedCard: React.FC<{
  speed?: {
    sectors_up: FlowSpeedRow[];
    sectors_down: FlowSpeedRow[];
    stocks_up: FlowSpeedRow[];
    stocks_down: FlowSpeedRow[];
  };
  period: FlowPeriod;
  onSelectSector?: (name: string) => void;
  onSelectStock?: (symbol: string, name: string) => void;
}> = ({ speed, period, onSelectSector, onSelectStock }) => {
  const [kind, setKind] = useState<'sector' | 'stock'>('sector');
  const [dir, setDir] = useState<'up' | 'down'>('up');
  const rows = useMemo(() => {
    if (!speed) return [];
    if (kind === 'sector') return dir === 'up' ? speed.sectors_up : speed.sectors_down;
    return dir === 'up' ? speed.stocks_up : speed.stocks_down;
  }, [speed, kind, dir]);
  if (!speed) return null;
  return (
    <div className="bg-white/95 backdrop-blur-md rounded-2xl border border-slate-200 shadow-xs px-3.5 py-2.5">
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <div className="flex items-center gap-1.5">
          <Gauge className="w-3.5 h-3.5 text-amber-500" />
          <span className="text-xs font-extrabold text-slate-800">资金流速率</span>
          <span className="text-[10px] text-slate-400">当前 {period.replace('d', '')} 日日均 − 前一窗口日均</span>
        </div>
        <div className="flex items-center gap-1">
          {(['sector', 'stock'] as const).map((k) => (
            <button
              key={k}
              onClick={() => setKind(k)}
              className={`px-2.5 py-0.5 rounded-full text-[10px] font-extrabold ${kind === k ? 'bg-purple-600 text-white' : 'bg-slate-100 text-slate-600'}`}
            >
              {k === 'sector' ? '板块' : '个股'}
            </button>
          ))}
          <button
            onClick={() => setDir('up')}
            className={`ml-2 px-2.5 py-0.5 rounded-full text-[10px] font-extrabold inline-flex items-center gap-0.5 ${dir === 'up' ? 'bg-red-50 text-red-600 border border-red-200' : 'bg-slate-100 text-slate-600'}`}
          >
            <TrendingUp className="w-3 h-3" /> 加速流入
          </button>
          <button
            onClick={() => setDir('down')}
            className={`px-2.5 py-0.5 rounded-full text-[10px] font-extrabold inline-flex items-center gap-0.5 ${dir === 'down' ? 'bg-emerald-50 text-emerald-600 border border-emerald-200' : 'bg-slate-100 text-slate-600'}`}
          >
            <TrendingDown className="w-3 h-3" /> 加速流出
          </button>
        </div>
      </div>
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-x-4 mt-1">
        {rows.map((r, i) => (
          <Row key={`${r.symbol ?? r.name}-${i}`} row={r} kind={kind} onSelectSector={onSelectSector} onSelectStock={onSelectStock} />
        ))}
        {!rows.length && <span className="text-[10px] text-slate-300">该窗口无速率数据</span>}
      </div>
      <p className="text-[9px] text-slate-400 mt-1">
        加速度=当前窗口日均主力净额 − 前一窗口日均；正=越流越快/加速进，负=加速跑。速率是变化量，不单独构成买卖建议。
      </p>
    </div>
  );
};
