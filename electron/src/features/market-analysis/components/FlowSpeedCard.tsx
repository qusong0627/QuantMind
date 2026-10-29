/**
 * 资金流速率卡（v2 可视化）：当前窗口日均净额 vs 前一窗口日均净额。
 *
 * 口径：加速度 = 当前 N 日日均主力净额 − 前一 N 日日均（元/日）；
 * 正=加速流入，负=加速流出。速率只看“变化方向/快慢”，不单独构成买卖建议。
 *
 * 版式：以 0 为轴的**双向条形图**，红条向右=加速流入、绿条向左=加速流出；
 * 一屏同时看到两侧，不再用「先选方向再看列表」的两段式。
 */
import React, { useMemo, useState } from 'react';
import { Gauge, TrendingDown, TrendingUp } from 'lucide-react';
import type { FlowPeriod, FlowSpeedRow } from '../services/moneyFlowService';

type Kind = 'sector' | 'stock';
type DirFilter = 'all' | 'up' | 'down';

const fmtDaily = (v: number | null | undefined): string => {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  return `${v >= 0 ? '+' : ''}${(v / 1e8).toFixed(2)}亿/日`;
};
const fmtAccel = (v: number | null | undefined): string => {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  return `${v >= 0 ? '+' : ''}${(v / 1e8).toFixed(2)}亿`;
};
const fmtRatio = (v: number | null | undefined): string =>
  v === null || v === undefined ? '—' : `${(v * 100).toFixed(2)}%`;

const SpeedBar: React.FC<{
  row: FlowSpeedRow;
  kind: Kind;
  maxAbs: number;
  onSelectSector?: (name: string) => void;
  onSelectStock?: (symbol: string, name: string) => void;
}> = ({ row, kind, maxAbs, onSelectSector, onSelectStock }) => {
  const accel = row.accel ?? 0;
  const up = accel >= 0;
  const width = maxAbs > 0 ? Math.min(50, (Math.abs(accel) / maxAbs) * 50) : 0;
  const click = () => {
    if (kind === 'sector' && row.name) onSelectSector?.(row.name);
    if (kind === 'stock' && row.symbol) onSelectStock?.(row.symbol, row.name ?? '');
  };
  return (
    <button
      onClick={click}
      className="group flex items-center gap-1.5 py-[3px] text-left hover:bg-slate-50 rounded px-1 -mx-1 transition-colors w-full"
      title={`当前日均 ${fmtDaily(row.net_daily)} · 前一窗口日均 ${fmtDaily(row.prev_net_daily)} · 净额比 ${fmtRatio(row.net_ratio)}（前 ${fmtRatio(row.prev_net_ratio)}）`}
    >
      <span className="w-24 shrink-0 min-w-0">
        <span className="block text-[10px] font-bold text-slate-700 truncate">{row.name || '—'}</span>
        <span className="block text-[8px] font-mono text-slate-400 truncate">
          {row.symbol ?? ''}
          {row.sector ? ` ${row.sector}` : ''}
        </span>
      </span>
      <span className="relative h-2.5 flex-1 min-w-0 rounded-full bg-slate-100/80 overflow-hidden">
        <span className="absolute top-0 bottom-0 left-1/2 w-px bg-slate-300/90 z-10" />
        <span
          className={`absolute top-0 bottom-0 rounded-full transition-[width] duration-500 ${
            up ? 'left-1/2 bg-red-500/80' : 'right-1/2 bg-emerald-500/80'
          }`}
          style={{ width: `${width}%` }}
        />
      </span>
      <span className={`w-[68px] shrink-0 text-right text-[10px] font-mono font-extrabold ${up ? 'text-red-600' : 'text-emerald-600'}`}>
        {fmtAccel(accel)}
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
  const [kind, setKind] = useState<Kind>('sector');
  const [dir, setDir] = useState<DirFilter>('all');

  const { upRows, downRows, rows, maxAbs } = useMemo(() => {
    const empty = { upRows: [], downRows: [], rows: [], maxAbs: 0 };
    if (!speed) return empty;
    const up = (kind === 'sector' ? speed.sectors_up : speed.stocks_up) ?? [];
    const down = (kind === 'sector' ? speed.sectors_down : speed.stocks_down) ?? [];
    const pickedUp = up.slice(0, 6);
    const pickedDown = down.slice(0, 6);
    const merged =
      dir === 'up' ? pickedUp : dir === 'down' ? pickedDown : [...pickedUp, ...pickedDown];
    const scale = Math.max(...merged.map((r) => Math.abs(r.accel ?? 0)), 1);
    return { upRows: up, downRows: down, rows: merged, maxAbs: scale };
  }, [speed, kind, dir]);

  if (!speed) return null;

  return (
    <div className="bg-white/95 backdrop-blur-md rounded-2xl border border-slate-200 shadow-xs px-3.5 py-2.5 flex flex-col gap-1">
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <div className="flex items-center gap-1.5 flex-wrap">
          <Gauge className="w-3.5 h-3.5 text-amber-500" />
          <span className="text-xs font-extrabold text-slate-800">资金流速率</span>
          <span className="text-[10px] text-slate-400">
            当前 {period.replace('d', '')} 日日均 − 前一窗口日均 · 两侧同屏
          </span>
          <span className="text-[9px] font-mono text-slate-400">
            加速流入 {upRows.length} · 加速流出 {downRows.length}
          </span>
        </div>
        <div className="flex items-center gap-1">
          <div className="flex rounded-full bg-slate-100 p-0.5">
            {(['sector', 'stock'] as const).map((k) => (
              <button
                key={k}
                onClick={() => setKind(k)}
                className={`px-2.5 py-0.5 rounded-full text-[10px] font-extrabold transition-colors ${
                  kind === k ? 'bg-white text-purple-700 shadow-2xs' : 'text-slate-500 hover:text-slate-800'
                }`}
              >
                {k === 'sector' ? '板块' : '个股'}
              </button>
            ))}
          </div>
          <div className="flex rounded-full bg-slate-100 p-0.5">
            <button
              onClick={() => setDir('all')}
              className={`px-2.5 py-0.5 rounded-full text-[10px] font-extrabold ${
                dir === 'all' ? 'bg-white text-slate-800 shadow-2xs' : 'text-slate-500 hover:text-slate-800'
              }`}
            >
              全部
            </button>
            <button
              onClick={() => setDir('up')}
              className={`px-2.5 py-0.5 rounded-full text-[10px] font-extrabold inline-flex items-center gap-0.5 ${
                dir === 'up' ? 'bg-red-50 text-red-600 border border-red-200' : 'text-slate-500 hover:text-slate-800'
              }`}
            >
              <TrendingUp className="w-3 h-3" /> 加速流入
            </button>
            <button
              onClick={() => setDir('down')}
              className={`px-2.5 py-0.5 rounded-full text-[10px] font-extrabold inline-flex items-center gap-0.5 ${
                dir === 'down' ? 'bg-emerald-50 text-emerald-600 border border-emerald-200' : 'text-slate-500 hover:text-slate-800'
              }`}
            >
              <TrendingDown className="w-3 h-3" /> 加速流出
            </button>
          </div>
        </div>
      </div>

      <div className="relative flex items-center gap-1.5 text-[8px] text-slate-400 font-mono px-1">
        <span className="w-24 shrink-0" />
        <span className="flex-1 flex justify-between">
          <span className="text-emerald-600">← 加速流出</span>
          <span>0</span>
          <span className="text-red-600">加速流入 →</span>
        </span>
        <span className="w-[68px] shrink-0 text-right">加速度</span>
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-2 gap-x-5">
        {rows.map((r, i) => (
          <SpeedBar
            key={`${r.symbol ?? r.name}-${i}`}
            row={r}
            kind={kind}
            maxAbs={maxAbs}
            onSelectSector={onSelectSector}
            onSelectStock={onSelectStock}
          />
        ))}
        {!rows.length && <span className="text-[10px] text-slate-300">该窗口无速率数据</span>}
      </div>

      <p className="text-[9px] text-slate-400">
        加速度=当前窗口日均主力净额 − 前一窗口日均；正=越流越快/加速进，负=加速跑。速率是变化量，不单独构成买卖建议。
      </p>
    </div>
  );
};
