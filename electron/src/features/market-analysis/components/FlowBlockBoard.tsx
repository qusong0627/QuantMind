/**
 * 资金流全景「板块 / 个股方块图」：把下方表格换成可下钻的矩形块。
 *
 * - 面积 = |周期主力净额|（板块层=板块主力净额，个股层=个股主力净额）；
 * - 颜色 = T+N 概率模型（板块=成分聚合概率，个股=未校准排序分）；模型不可用时
 *   退回「红=净流入 / 绿=净流出」；
 * - 板块模式点块 → 打开板块明细；个股模式为「板块块内嵌个股块」→ 点个股卡片；
 * - 模型只覆盖最新交易日；历史快照不拿最新概率冒充当时，只按净额着色。
 */
import React, { useEffect, useMemo, useRef, useState } from 'react';
import * as echarts from 'echarts';
import { Boxes, Info } from 'lucide-react';
import type {
  FlowPanoramaResponse,
  FlowProbabilityResponse,
  FlowProbabilitySector,
  FlowProbabilityStock,
  FlowSpeedRow,
} from '../services/moneyFlowService';
import { fmtYi, fmtPct } from '../services/moneyFlowService';
import {
  blockColor,
  blockLabel,
  buildSectorBlocks,
  buildStockBlocks,
  type FlowBlockDatum,
} from './flowBlockModel';

const HORIZONS = [3, 5, 10, 20] as const;
type BlockMode = 'sector' | 'stock';

const fmtSpeed = (v: number | null | undefined): string =>
  v === null || v === undefined || Number.isNaN(v)
    ? '—'
    : `${v >= 0 ? '+' : ''}${(v / 1e8).toFixed(2)}亿/日`;
const fmtScore = (v: number | null | undefined): string =>
  v === null || v === undefined || Number.isNaN(v) ? '—' : (v * 100).toFixed(1);
const stockScore = (r: FlowProbabilityStock, h: number): number | null => {
  const raw = r[`raw${h}` as keyof FlowProbabilityStock];
  const p = r[`p${h}` as keyof FlowProbabilityStock];
  return typeof raw === 'number' ? raw : typeof p === 'number' ? p : null;
};
const sectorScore = (r: FlowProbabilitySector, h: number): number | null => {
  const raw = r[`raw${h}` as keyof FlowProbabilitySector];
  const p = r[`p${h}` as keyof FlowProbabilitySector];
  return typeof raw === 'number' ? raw : typeof p === 'number' ? p : null;
};

interface EChartsBlockNode {
  name: string;
  value: number;
  _d: FlowBlockDatum;
  itemStyle: { color: string; borderColor: string; borderWidth: number; gapWidth: number };
  label: { formatter: string };
  children?: EChartsBlockNode[];
}

export interface FlowBlockBoardProps {
  panorama: FlowPanoramaResponse | null;
  probability: FlowProbabilityResponse | null;
  date?: string;
  onSelectSector: (name: string) => void;
  onSelectStock: (symbol: string, name: string) => void;
}

const RailSection: React.FC<{ title: string; hint?: string; children: React.ReactNode }> = ({
  title,
  hint,
  children,
}) => (
  <section className="rounded-xl border border-slate-100 bg-slate-50/50 px-2 py-1.5 flex flex-col gap-0.5">
    <div className="flex items-center justify-between gap-1">
      <span className="text-[10px] font-extrabold text-slate-600">{title}</span>
      {hint && <span className="text-[9px] text-slate-400">{hint}</span>}
    </div>
    {children}
  </section>
);

const RailRow: React.FC<{
  label: string;
  sub?: string;
  value: string;
  tone?: 'up' | 'down' | 'neutral';
  onClick?: () => void;
}> = ({ label, sub, value, tone = 'neutral', onClick }) => (
  <button
    onClick={onClick}
    disabled={!onClick}
    className={`flex items-center gap-1 rounded px-1 py-0.5 text-left transition-colors ${
      onClick ? 'hover:bg-white cursor-pointer' : 'cursor-default'
    }`}
  >
    <span className="text-[10px] font-bold text-slate-700 truncate">{label}</span>
    {sub && <span className="text-[8px] text-slate-400 truncate">{sub}</span>}
    <span className="flex-1" />
    <span
      className={`text-[10px] font-mono font-extrabold shrink-0 ${
        tone === 'up' ? 'text-red-600' : tone === 'down' ? 'text-emerald-600' : 'text-slate-600'
      }`}
    >
      {value}
    </span>
  </button>
);

export const FlowBlockBoard: React.FC<FlowBlockBoardProps> = ({
  panorama,
  probability,
  date,
  onSelectSector,
  onSelectStock,
}) => {
  const [mode, setMode] = useState<BlockMode>('sector');
  const [h, setH] = useState<number>(10);
  const [limit, setLimit] = useState<number | 'all'>(30);
  const chartRef = useRef<HTMLDivElement>(null);
  const instRef = useRef<echarts.ECharts | null>(null);
  const clickRef = useRef({ onSelectSector, onSelectStock });
  clickRef.current = { onSelectSector, onSelectStock };

  const probabilityAvailable = !!probability?.available && !!probability.artifact?.latest && !date;
  const thresholds = probability?.artifact?.thresholds?.[String(h)];

  const blocks = useMemo(() => {
    if (mode === 'sector') {
      return buildSectorBlocks(panorama?.sectors ?? [], probabilityAvailable ? probability : null, h);
    }
    return buildStockBlocks(panorama, probabilityAvailable ? probability : null, h);
  }, [mode, panorama, probability, probabilityAvailable, h]);

  // 板块聚合概率的分布比个股窄：用板块自身分位做红/绿阈值；个股用产物阈值
  const threshold = useMemo(() => {
    if (mode !== 'sector' || !probabilityAvailable) {
      return { hi: thresholds?.hi ?? 0.5, lo: thresholds?.lo ?? 0.42 };
    }
    const vals = blocks
      .map((b) => b.score)
      .filter((v): v is number => typeof v === 'number' && Number.isFinite(v))
      .sort((a, b) => a - b);
    if (vals.length < 5) return { hi: 0.5, lo: 0.42 };
    const q = (p: number) => vals[Math.min(vals.length - 1, Math.max(0, Math.floor(p * (vals.length - 1))))];
    return { hi: q(0.75), lo: q(0.25) };
  }, [mode, blocks, probabilityAvailable, thresholds]);
  const { hi, lo } = threshold;

  const visibleBlocks = useMemo(
    () => (limit === 'all' ? blocks : blocks.slice(0, limit)),
    [blocks, limit],
  );

  const probStocks = probability?.artifact?.latest?.stocks;
  const stockList = (prefix: string): FlowProbabilityStock[] => {
    const bag = probStocks as unknown as
      | Record<string, FlowProbabilityStock[] | undefined>
      | undefined;
    return bag?.[`${prefix}_${h}`] ?? [];
  };
  const topStocks = stockList('top');
  const bottomStocks = stockList('bottom');
  const reverseStocks = stockList('outflow_strong');
  const weakStocks = stockList('inflow_weak');
  const reverseSet = useMemo(() => new Set(reverseStocks.map((r) => r.symbol)), [reverseStocks]);
  const weakSet = useMemo(() => new Set(weakStocks.map((r) => r.symbol)), [weakStocks]);

  const sectorAgg = useMemo(() => {
    const list = (probability?.artifact?.latest?.sectors ?? []).slice();
    list.sort((a, b) => (sectorScore(b, h) ?? 0) - (sectorScore(a, h) ?? 0));
    return list;
  }, [probability, h]);

  const speed = panorama?.speed;
  const speedUp = (mode === 'sector' ? speed?.sectors_up : speed?.stocks_up) ?? [];
  const speedDown = (mode === 'sector' ? speed?.sectors_down : speed?.stocks_down) ?? [];
  const metrics = probability?.artifact?.metrics?.[String(h)];

  const chartData = useMemo<EChartsBlockNode[]>(() => {
    const toNode = (d: FlowBlockDatum): EChartsBlockNode => ({
      name: d.name,
      value: d.value,
      _d: d,
      itemStyle: {
        color: d.kind === 'sector' && d.children?.length
          ? '#f8fafc'
          : blockColor(d, probabilityAvailable, hi, lo),
        borderColor: '#ffffff',
        borderWidth: 1,
        gapWidth: 1,
      },
      label: {
        formatter: d.children?.length
          ? d.name
          : `${blockLabel(d, probabilityAvailable)}${
              d.kind === 'stock' && d.symbol
                ? reverseSet.has(d.symbol)
                  ? ' ⟳'
                  : weakSet.has(d.symbol)
                    ? ' ⚠'
                    : ''
                : d.accel !== null
                  ? d.accel > 0
                    ? ' ↑'
                    : ' ↓'
                  : ''
            }`,
      },
      children: d.children?.map(toNode),
    });
    return visibleBlocks.map(toNode);
  }, [visibleBlocks, probabilityAvailable, hi, lo, reverseSet, weakSet]);

  useEffect(() => {
    if (!chartRef.current) return;
    if (!instRef.current) {
      instRef.current = echarts.init(chartRef.current);
      instRef.current.on('click', (params: any) => {
        const d: FlowBlockDatum | undefined = params?.data?._d;
        if (!d) return;
        if (d.kind === 'stock' && d.symbol) clickRef.current.onSelectStock(d.symbol, d.name);
        else if (d.kind === 'sector') clickRef.current.onSelectSector(d.name);
      });
    }
    const chart = instRef.current;
    if (chartData.length === 0) {
      chart.clear();
      return;
    }
    chart.setOption(
      {
        tooltip: {
          trigger: 'item',
          formatter: (params: any) => {
            const d: FlowBlockDatum | undefined = params?.data?._d;
            if (!d) return '';
            const rows: string[] = [
              `<div style="font-weight:800;font-size:12px;margin-bottom:4px">${d.name}${
                d.sector && d.kind === 'stock' ? ` <span style="color:#94a3b8;font-weight:400">· ${d.sector}</span>` : ''
              }</div>`,
              `周期主力净额：<b style="color:${d.net >= 0 ? '#e11d48' : '#059669'}">${fmtYi(d.net)}</b>`,
              `周期涨幅：${fmtPct(d.pct)}`,
            ];
            if (probabilityAvailable && d.score !== null) {
              rows.push(
                d.kind === 'sector'
                  ? `T+${h} 聚合概率：<b>${(d.prob! * 100).toFixed(1)}%</b>（成分股概率成交额加权，不是独立板块模型）`
                  : `T+${h} 模型分：<b>${(d.score * 100).toFixed(1)}</b>（排序分，不是真实胜率）`,
              );
            } else if (probabilityAvailable) {
              rows.push(`T+${h} 模型：该标的未覆盖（—）`);
            } else {
              rows.push(`<span style="color:#94a3b8">概率模型只覆盖最新交易日；当前按净额着色</span>`);
            }
            if (d.accel !== null) {
              rows.push(
                `资金加速度：<b style="color:${d.accel >= 0 ? '#e11d48' : '#059669'}">${
                  d.accel >= 0 ? '+' : ''
                }${(d.accel / 1e8).toFixed(2)} 亿/日</b>`,
              );
            }
            rows.push(
              `<div style="margin-top:4px;color:#94a3b8">${
                d.kind === 'stock' ? '点击打开个股资金流详情' : '点击打开板块明细'
              }</div>`,
            );
            return `<div style="font-size:11px;line-height:1.7;max-width:260px">${rows.join('<br/>')}</div>`;
          },
        },
        series: [
          {
            id: 'flow-blocks',
            type: 'treemap',
            left: 0,
            right: 0,
            top: 0,
            bottom: 0,
            roam: false,
            nodeClick: false,
            breadcrumb: { show: false },
            animationDuration: 650,
            animationDurationUpdate: 520,
            animationEasing: 'quarticOut',
            animationEasingUpdate: 'cubicInOut',
            data: chartData,
            levels: [
              {
                itemStyle: { borderColor: '#fff', borderWidth: 2, gapWidth: 2 },
                label: { show: true, fontSize: 10, lineHeight: 13, color: '#fff', fontWeight: 'bold', overflow: 'truncate' },
              },
              {
                itemStyle: { borderColor: '#fff', borderWidth: 1, gapWidth: 1 },
                label: { show: true, fontSize: 9, lineHeight: 12, color: '#fff', fontWeight: 'bold', overflow: 'truncate' },
              },
            ],
            upperLabel: {
              show: mode === 'stock',
              height: 16,
              fontSize: 9,
              color: '#334155',
              backgroundColor: 'rgba(248,250,252,0.85)',
              padding: [1, 3],
            },
          },
        ],
      },
      { notMerge: false, replaceMerge: ['series'] },
    );
  }, [chartData, mode, h, probabilityAvailable]);

  useEffect(() => {
    const el = chartRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => instRef.current?.resize());
    ro.observe(el);
    return () => {
      ro.disconnect();
      instRef.current?.dispose();
      instRef.current = null;
    };
  }, []);

  const total = visibleBlocks.length;

  return (
    <div className="bg-white/95 backdrop-blur-md rounded-2xl border border-purple-100/80 shadow-md shadow-purple-500/5 p-3.5 flex flex-col gap-2 min-w-0">
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <h3 className="text-xs font-extrabold text-slate-800 flex items-center gap-1.5">
          <Boxes className="w-3.5 h-3.5 text-purple-600" />
          板块 / 个股方块图
          <span className="text-[10px] font-normal text-slate-400">面积=周期净额 · 点击下钻</span>
        </h3>
        <div className="flex items-center gap-1.5 flex-wrap">
          <div className="flex rounded-full bg-slate-100 p-0.5">
            {(['sector', 'stock'] as BlockMode[]).map((m) => (
              <button
                key={m}
                onClick={() => setMode(m)}
                className={`px-2.5 py-0.5 rounded-full text-[10px] font-extrabold transition-colors ${
                  mode === m ? 'bg-white text-purple-700 shadow-2xs' : 'text-slate-500 hover:text-slate-800'
                }`}
              >
                {m === 'sector' ? '板块' : '个股'}
              </button>
            ))}
          </div>
          <div className="flex rounded-full bg-slate-100 p-0.5">
            {HORIZONS.map((x) => (
              <button
                key={x}
                onClick={() => setH(x)}
                className={`px-2 py-0.5 rounded-full text-[10px] font-extrabold transition-colors ${
                  h === x ? 'bg-purple-600 text-white' : 'text-slate-500 hover:text-slate-800'
                }`}
              >
                T+{x}
              </button>
            ))}
          </div>
          <div className="flex rounded-full bg-slate-100 p-0.5">
            {(['30', 'all'] as const).map((x) => (
              <button
                key={x}
                onClick={() => setLimit(x === 'all' ? 'all' : 30)}
                className={`px-2 py-0.5 rounded-full text-[10px] font-extrabold transition-colors ${
                  (x === 'all' ? limit === 'all' : limit === 30)
                    ? 'bg-white text-purple-700 shadow-2xs'
                    : 'text-slate-500 hover:text-slate-800'
                }`}
                title={x === 'all' ? '显示全部板块' : '只显示净额前 30 的板块'}
              >
                {x === 'all' ? '全部' : '前30'}
              </button>
            ))}
          </div>
        </div>
      </div>

      <div className="flex items-center gap-1.5 text-[10px] text-slate-400 flex-wrap">
        <Info className="w-3 h-3 shrink-0" />
        <span>
          {probabilityAvailable
            ? `颜色=模型 T+${h}（红=强 / 绿=弱，灰=中性）· ↑加速流入 ↓加速流出 · ⟳逆流候选 · ⚠流入但弱`
            : '概率模型只覆盖最新交易日；当前按净额着色（红=净流入 / 绿=净流出）'}
        </span>
        {probabilityAvailable && thresholds && (
          <span className="font-mono">
            阈值 {lo.toFixed(2)} ~ {hi.toFixed(2)}
          </span>
        )}
        <span className="ml-auto font-mono">{total} 个块</span>
      </div>

      <div className="flex flex-col xl:flex-row gap-2.5 min-w-0">
        <div className="flex-1 min-w-0">
          <div ref={chartRef} className="w-full min-w-0" style={{ height: 420 }} />
        </div>
        <aside className="w-full xl:w-[300px] shrink-0 flex flex-col gap-1.5 max-h-[420px] overflow-y-auto custom-scrollbar pr-0.5">
          <RailSection title="资金流速率" hint={mode === 'sector' ? '板块' : '个股'}>
            {speedUp.slice(0, 3).map((r: FlowSpeedRow, i) => (
              <RailRow
                key={`su-${r.name || r.symbol}-${i}`}
                label={r.name || r.symbol || '—'}
                sub={r.sector ?? undefined}
                value={fmtSpeed(r.accel)}
                tone="up"
                onClick={() =>
                  mode === 'sector' && r.name
                    ? onSelectSector(r.name)
                    : r.symbol && onSelectStock(r.symbol, r.name || r.symbol)
                }
              />
            ))}
            {speedUp.length === 0 && <span className="text-[9px] text-slate-300">—</span>}
            <div className="h-px bg-slate-100 my-0.5" />
            {speedDown.slice(0, 3).map((r: FlowSpeedRow, i) => (
              <RailRow
                key={`sd-${r.name || r.symbol}-${i}`}
                label={r.name || r.symbol || '—'}
                sub={r.sector ?? undefined}
                value={fmtSpeed(r.accel)}
                tone="down"
                onClick={() =>
                  mode === 'sector' && r.name
                    ? onSelectSector(r.name)
                    : r.symbol && onSelectStock(r.symbol, r.name || r.symbol)
                }
              />
            ))}
            {speedDown.length === 0 && <span className="text-[9px] text-slate-300">—</span>}
          </RailSection>

          <RailSection title={mode === 'sector' ? '板块聚合概率' : '模型强弱'} hint={`T+${h}`}>
            {mode === 'sector' ? (
              <>
                {sectorAgg.slice(0, 3).map((r) => (
                  <RailRow
                    key={`sa-up-${r.name}`}
                    label={r.name}
                    sub={`n=${r.n}`}
                    value={fmtScore(sectorScore(r, h))}
                    tone="up"
                    onClick={() => onSelectSector(r.name)}
                  />
                ))}
                <div className="h-px bg-slate-100 my-0.5" />
                {sectorAgg.slice(-3).reverse().map((r) => (
                  <RailRow
                    key={`sa-dn-${r.name}`}
                    label={r.name}
                    sub={`n=${r.n}`}
                    value={fmtScore(sectorScore(r, h))}
                    tone="down"
                    onClick={() => onSelectSector(r.name)}
                  />
                ))}
              </>
            ) : (
              <>
                {topStocks.slice(0, 3).map((r) => (
                  <RailRow
                    key={`st-up-${r.symbol}`}
                    label={r.name || r.symbol}
                    sub={r.industry ?? undefined}
                    value={fmtScore(stockScore(r, h))}
                    tone="up"
                    onClick={() => onSelectStock(r.symbol, r.name || r.symbol)}
                  />
                ))}
                <div className="h-px bg-slate-100 my-0.5" />
                {bottomStocks.slice(0, 3).map((r) => (
                  <RailRow
                    key={`st-dn-${r.symbol}`}
                    label={r.name || r.symbol}
                    sub={r.industry ?? undefined}
                    value={fmtScore(stockScore(r, h))}
                    tone="down"
                    onClick={() => onSelectStock(r.symbol, r.name || r.symbol)}
                  />
                ))}
              </>
            )}
            {!probabilityAvailable && <span className="text-[9px] text-slate-300">模型产物未覆盖</span>}
          </RailSection>

          <RailSection title="逆流候选 / 流入但弱" hint={`T+${h}`}>
            {reverseStocks.slice(0, 3).map((r) => (
              <RailRow
                key={`rv-${r.symbol}`}
                label={r.name || r.symbol}
                sub="流出·高"
                value={fmtScore(stockScore(r, h))}
                tone="up"
                onClick={() => onSelectStock(r.symbol, r.name || r.symbol)}
              />
            ))}
            <div className="h-px bg-slate-100 my-0.5" />
            {weakStocks.slice(0, 3).map((r) => (
              <RailRow
                key={`wk-${r.symbol}`}
                label={r.name || r.symbol}
                sub="流入·弱"
                value={fmtScore(stockScore(r, h))}
                tone="down"
                onClick={() => onSelectStock(r.symbol, r.name || r.symbol)}
              />
            ))}
            {reverseStocks.length === 0 && weakStocks.length === 0 && (
              <span className="text-[9px] text-slate-300">—</span>
            )}
          </RailSection>

          <RailSection title="模型 OOS" hint={`T+${h}`}>
            {metrics ? (
              <div className="text-[9px] text-slate-500 leading-snug font-mono">
                AUC {metrics.auc?.toFixed(3) ?? '—'} · top 均值{' '}
                {metrics.top_mean == null ? '—' : `${metrics.top_mean > 0 ? '+' : ''}${metrics.top_mean.toFixed(2)}%`} · 胜率{' '}
                {metrics.top_hit == null ? '—' : `${(metrics.top_hit * 100).toFixed(1)}%`}（基准{' '}
                {metrics.base_hit == null ? '—' : `${(metrics.base_hit * 100).toFixed(1)}%`}） · bottom{' '}
                {metrics.bot_mean == null ? '—' : `${metrics.bot_mean > 0 ? '+' : ''}${metrics.bot_mean.toFixed(2)}%`}
              </div>
            ) : (
              <span className="text-[9px] text-slate-300">该窗口无 2025+ 留出样本</span>
            )}
          </RailSection>
        </aside>
      </div>
    </div>
  );
};
