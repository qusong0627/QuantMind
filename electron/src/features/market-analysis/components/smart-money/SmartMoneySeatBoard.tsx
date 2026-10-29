/** 聪明钱 · 席位榜 v2（细案 §3.6 / §6.1：窗口 × 排序 × 类型三筛，服务端计算）。
 *
 * 口径：净买额=买入−卖出（亿元）；胜率/平均超额=该席位事件标的的已闭合前向 h5，
 * 池内基线随响应返回（对照展示）；win_rate 排序时窗内样本 < min_trades 沉底并标「样本不足」。
 * 纪律：失败显式报错不伪装空数据；缺失「—」不补 0。
 */

import React, { useCallback, useEffect, useRef, useState } from 'react';
import { AlertCircle, Store } from 'lucide-react';
import { Tooltip } from 'antd';
import { EmptyHint, FaceCard, PeriodChips } from '../../../market-analysis-shared/ui';
import { SeatScatter, TopSeatStrip } from './SmartMoneySeatCharts';
import {
  getSmartMoneySeats,
  type SmartSeatsResponse,
  type SmartSeatRow,
} from '../../services/smartMoneyService';
import {
  MISSING,
  SEAT_CLASS_ORDER,
  fmtBp,
  fmtPct,
  fmtSigned,
  seatClassLabel,
  signCls,
} from '../../smartMoneyVocabulary';

const WINDOW_OPTIONS = [
  { id: '20', label: '20 日' },
  { id: '60', label: '60 日' },
  { id: 'all', label: '全史' },
];

const SORT_OPTIONS = [
  { id: 'win_rate', label: '按胜率' },
  { id: 'net', label: '按净买' },
];

const LIMIT_OPTIONS = [50, 100, 300];

const CLASS_TONES: Record<string, string> = {
  inst: 'bg-purple-50 text-purple-700 border-purple-200',
  north: 'bg-blue-50 text-blue-700 border-blue-200',
  branch: 'bg-slate-100 text-slate-600 border-slate-200',
  other: 'bg-slate-50 text-slate-500 border-slate-200',
};

interface SmartMoneySeatBoardProps {
  /** 外部刷新信号（父级「刷新」按钮递增值触发重取） */
  refreshToken?: number;
  /** 外部选中的席位（详情页联动高亮，可选） */
  highlightSeat?: string | null;
  onPickSeat?: (seatName: string) => void;
  /** true = 无卡壳（统一大框内的标签面） */
  bare?: boolean;
  className?: string;
}

const SeatRowView: React.FC<{
  row: SmartSeatRow;
  window: string;
  minTrades: number;
  highlighted: boolean;
  onClick?: () => void;
}> = ({ row, window, minTrades, highlighted, onClick }) => {
  const w = row.windows?.[window] ?? null;
  const n = w?.n ?? null;
  const insufficient = n !== null && n < minTrades;
  return (
    <div
      onClick={onClick}
      className={`grid grid-cols-[minmax(140px,1.6fr)_74px_minmax(0,0.8fr)_minmax(0,0.7fr)_minmax(0,0.8fr)_56px_64px] gap-1.5 px-1 py-1 items-baseline border-b border-slate-50 last:border-0 ${
        onClick ? 'cursor-pointer hover:bg-purple-50/60 transition-colors' : ''
      } ${highlighted ? 'bg-purple-50/80' : ''}`}
    >
      <span className="min-w-0 flex items-baseline gap-1">
        <span className="text-[10px] font-bold text-slate-700 truncate" title={row.seat_name}>
          {row.seat_name}
        </span>
        {insufficient && (
          <Tooltip title={`窗内样本 ${n} < ${minTrades}，统计不稳已沉底`}>
            <span className="shrink-0 text-[8px] font-bold text-amber-600 border border-amber-200 bg-amber-50 rounded px-0.5">
              样本不足
            </span>
          </Tooltip>
        )}
      </span>
      <span
        className={`justify-self-start text-[9px] font-bold border rounded px-1 ${
          CLASS_TONES[row.class] ?? CLASS_TONES.other
        }`}
      >
        {seatClassLabel(row.class, row.class_label)}
      </span>
      <span className={`text-right text-[10px] font-mono font-bold ${signCls(row.total_net_yi)}`}>
        {fmtSigned(row.total_net_yi, 2)}
      </span>
      <span className="text-right text-[10px] font-mono text-slate-600">
        {fmtPct(w?.win_rate, 1)}
      </span>
      <span className={`text-right text-[10px] font-mono ${signCls(w?.avg_excess_bp)}`}>
        {fmtBp(w?.avg_excess_bp)}
      </span>
      <span className="text-right text-[10px] font-mono text-slate-400">{n ?? MISSING}</span>
      <span className="text-right text-[9px] font-mono text-slate-400">
        {row.last_seen ? String(row.last_seen).slice(5) : MISSING}
      </span>
    </div>
  );
};

export const SmartMoneySeatBoard: React.FC<SmartMoneySeatBoardProps> = ({
  refreshToken = 0,
  highlightSeat,
  onPickSeat,
  bare = false,
  className = '',
}) => {
  const [win, setWin] = useState('all');
  const [sort, setSort] = useState('win_rate');
  const [seatClass, setSeatClass] = useState<string | null>(null);
  const [limit, setLimit] = useState(50);
  const [view, setView] = useState<'chart' | 'table'>('chart');
  const [data, setData] = useState<SmartSeatsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const seq = useRef(0);

  const load = useCallback(async () => {
    const my = ++seq.current;
    setLoading(true);
    setError(null);
    try {
      const resp = await getSmartMoneySeats({
        window: win as '20' | '60' | 'all',
        sort: sort as 'net' | 'win_rate',
        seatClass,
        limit,
      });
      if (my !== seq.current) return; // 过期响应丢弃（快速连点不看错批）
      setData(resp);
    } catch (e) {
      if (my !== seq.current) return;
      // B4 纪律：失败进显式错误条，不伪装空数据
      setError(e instanceof Error ? e.message : '席位数据加载失败');
    } finally {
      if (my === seq.current) setLoading(false);
    }
  }, [win, sort, seatClass, limit, refreshToken]);

  useEffect(() => {
    void load();
  }, [load]);

  const classes = data?.classes ?? [];
  const items = data?.items ?? [];
  const minTrades = data?.min_trades ?? 5;
  const baseline = data?.baseline_win_rate ?? null;

  return (
    <FaceCard
      bare={bare}
      className={className}
      icon={<Store className="w-3.5 h-3.5 text-purple-600" />}
      title={
        <span className="flex items-center gap-1.5">
          席位榜
          <span className="text-[10px] font-normal text-slate-400">
            龙虎榜席位行为统计 · h{data?.horizon ?? 5} 已闭合前向
          </span>
        </span>
      }
      extra={
        <div className="flex items-center gap-1.5 flex-wrap justify-end">
          <PeriodChips options={WINDOW_OPTIONS} value={win} onChange={setWin} />
          <PeriodChips options={SORT_OPTIONS} value={sort} onChange={setSort} />
          <div className="flex rounded-full bg-slate-100 p-0.5">
            {(['chart', 'table'] as const).map((v) => (
              <button
                key={v}
                type="button"
                onClick={() => setView(v)}
                aria-pressed={view === v}
                className={`px-2 py-0.5 rounded-full text-[10px] font-extrabold transition-colors ${
                  view === v ? 'bg-white text-purple-700 shadow-2xs' : 'text-slate-500'
                }`}
              >
                {v === 'chart' ? '图表' : '明细'}
              </button>
            ))}
          </div>
        </div>
      }
    >
      <div className="flex flex-col gap-2 min-w-0">
        {/* 类型筛选 + 池内基线 */}
        <div className="flex items-center gap-1.5 flex-wrap px-1">
          <button
            onClick={() => setSeatClass(null)}
            className={`px-2 py-0.5 rounded-full text-[10px] font-bold border transition-colors ${
              seatClass === null
                ? 'bg-purple-600 text-white border-purple-600'
                : 'bg-white text-slate-500 border-slate-200 hover:border-purple-300'
            }`}
          >
            全部类型
          </button>
          {SEAT_CLASS_ORDER.map((cls) => {
            const stat = classes.find((c) => c.class === cls);
            const label = seatClassLabel(cls, stat?.label);
            return (
              <button
                key={cls}
                onClick={() => setSeatClass(seatClass === cls ? null : cls)}
                title={
                  stat
                    ? `${label}：事件 ${stat.n_events} · 席位 ${stat.n_seats} · 胜率 ${fmtPct(stat.win_rate, 1)} · 净买 ${fmtSigned(stat.total_net_yi, 1)} 亿`
                    : label
                }
                className={`px-2 py-0.5 rounded-full text-[10px] font-bold border transition-colors ${
                  seatClass === cls
                    ? 'bg-purple-600 text-white border-purple-600'
                    : 'bg-white text-slate-500 border-slate-200 hover:border-purple-300'
                }`}
              >
                {label}
                {stat ? <span className="font-mono font-normal ml-1">({stat.n_seats})</span> : null}
              </button>
            );
          })}
          <span className="ml-auto text-[9px] text-slate-400">
            池内基线胜率（选中窗）{' '}
            <span className="font-mono font-bold text-slate-600">{fmtPct(baseline, 1)}</span>
            {' · '}事件 {data?.n_events ?? MISSING}
          </span>
        </div>

        {error && (
          <div className="mx-1 flex items-center gap-2 rounded-lg border border-amber-200 bg-amber-50 px-2 py-1.5">
            <AlertCircle className="w-3.5 h-3.5 text-amber-600 shrink-0" />
            <span className="text-[10px] text-amber-800 min-w-0 truncate">{error}</span>
            <button
              onClick={() => void load()}
              className="ml-auto shrink-0 text-[10px] font-bold text-amber-700 hover:text-amber-900"
            >
              重试
            </button>
          </div>
        )}

        {loading && !data ? (
          <EmptyHint loading />
        ) : !items.length && !error ? (
          <EmptyHint text="该筛选条件下暂无席位记录" />
        ) : view === 'chart' ? (
          <div className="flex flex-col gap-3">
            <TopSeatStrip items={items} window={data?.window ?? win} />
            <div className="rounded-xl border border-slate-100 bg-white/60 p-1.5">
              <SeatScatter items={items} window={data?.window ?? win} baseline={baseline} />
            </div>
            <div className="text-[9px] text-slate-400 px-1">
              图表按当前窗口（{win}）展示前 50 席；样本不足 {minTrades} 的席位仍可见但统计不稳，完整逐列数字切「明细」。
            </div>
          </div>
        ) : (
          <>
            {/* 限高滚动（表头吸顶）：50 席/页全展开会拉长整面，框内滚动阅读 */}
            <div className="max-h-[420px] overflow-auto custom-scrollbar rounded-lg border border-slate-100/80">
              <div className="min-w-[560px] flex flex-col">
                <div className="grid grid-cols-[minmax(140px,1.6fr)_74px_minmax(0,0.8fr)_minmax(0,0.7fr)_minmax(0,0.8fr)_56px_64px] gap-1.5 px-1 py-1 text-[9px] font-extrabold text-slate-400 border-b border-slate-100 sticky top-0 bg-white/95 backdrop-blur z-[5]">
                  <span>席位</span>
                  <span>类型</span>
                  <span className="text-right">净买(亿)</span>
                  <span className="text-right">胜率</span>
                  <span className="text-right">超额bp</span>
                  <span className="text-right">n</span>
                  <span className="text-right">最近</span>
                </div>
                <div className={`flex flex-col ${loading ? 'opacity-60' : ''} transition-opacity`}>
                  {items.map((row) => (
                    <SeatRowView
                      key={`${row.class}-${row.seat_name}`}
                      row={row}
                      window={data?.window ?? win}
                      minTrades={minTrades}
                      highlighted={!!highlightSeat && row.seat_name === highlightSeat}
                      onClick={onPickSeat ? () => onPickSeat(row.seat_name) : undefined}
                    />
                  ))}
                </div>
              </div>
            </div>
            <div className="flex items-center gap-1.5 px-1 flex-wrap">
              <span className="text-[9px] text-slate-400">
                共 {data?.total ?? items.length} 席 · 显示 {items.length} · 胜率=该席位事件标的
                h{data?.horizon ?? 5} 超额&gt;0 占比（对照池内基线）· 净买为全史窗口外累计
              </span>
              <div className="ml-auto flex items-center gap-1">
                {LIMIT_OPTIONS.map((n) => (
                  <button
                    key={n}
                    onClick={() => setLimit(n)}
                    className={`px-1.5 py-0.5 rounded text-[9px] font-bold border transition-colors ${
                      limit === n
                        ? 'bg-slate-700 text-white border-slate-700'
                        : 'bg-white text-slate-500 border-slate-200 hover:border-slate-400'
                    }`}
                  >
                    {n}
                  </button>
                ))}
              </div>
            </div>
          </>
        )}
      </div>
    </FaceCard>
  );
};
