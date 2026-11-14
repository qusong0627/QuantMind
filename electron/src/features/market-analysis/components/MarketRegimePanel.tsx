/** 市场阶段 · 五层判断（图表为主，可切换日期）。
 *
 * 读法：长期定天花板 → 中期定实际仓位 → 短期定买点 → 当日定执行 → 个股定买什么。
 * 颜色：绿=顺/可做，黄=中性，橙=偏弱/防守，红=逆/不做。
 */

import { useEffect, useMemo, useState } from 'react';
import { DatePicker, Select } from 'antd';
import dayjs from 'dayjs';
import ReactECharts from 'echarts-for-react';
import { Activity, ChevronDown, ChevronUp, Clock, Gauge, RefreshCw } from 'lucide-react';
import {
  fetchMarketRegime,
  fetchRegimeCompare,
  fetchSectorStocks,
  type CompareItem,
  type RegimeColor,
  type RegimeLayer,
  type RegimeResponse,
  type SectorStock,
} from '../services/regimeService';
import { IndicatorHealthCard } from './IndicatorHealthCard';

const DOT: Record<RegimeColor, string> = {
  green: '#16a34a',
  yellow: '#eab308',
  orange: '#f97316',
  red: '#dc2626',
  neutral: '#94a3b8',
};

const BORDER: Record<RegimeColor, string> = {
  green: 'border-emerald-200 bg-emerald-50/40',
  yellow: 'border-amber-200 bg-amber-50/40',
  orange: 'border-orange-200 bg-orange-50/40',
  red: 'border-rose-200 bg-rose-50/40',
  neutral: 'border-slate-200 bg-white',
};

const AXIS_LABEL = { fontSize: 9, color: '#94a3b8' } as const;
const SPLIT_LINE = { lineStyle: { color: '#f1f5f9' } } as const;

function retColor(v: number): string {
  if (v >= 3) return '#b91c1c';
  if (v >= 1) return '#ef4444';
  if (v > 0) return '#fca5a5';
  if (v <= -3) return '#15803d';
  if (v <= -1) return '#22c55e';
  if (v < 0) return '#86efac';
  return '#e2e8f0';
}

function quadColor(ret: number, rs: number): string {
  if (ret >= 0 && rs >= 0) return '#dc2626';
  if (ret >= 0 && rs < 0) return '#f59e0b';
  if (ret < 0 && rs >= 0) return '#0ea5e9';
  return '#16a34a';
}

const STATE_EXPLAIN: Record<string, string> = {
  防守: '趋势与广度同时走弱，优先控制回撤；只做超跌反抽，不做趋势加仓。',
  偏空: '多数维度偏弱，反弹持续性差；控制仓位，等趋势或广度修复。',
  震荡: '多空分歧、指数与广度不同步；轻仓做结构，不赌方向。',
  偏多: '趋势与广度共振向上；可持有或回踩加仓，但不追高。',
  风险偏好高: '多数维度同向偏强；可提高暴露，但仍需止损。',
};

function stateTone(state: string): { card: string; badge: string; text: string } {
  if (state === '偏多' || state === '风险偏好高') {
    return {
      card: 'border-rose-200 bg-rose-50/60',
      badge: 'bg-rose-600 text-white',
      text: 'text-rose-700',
    };
  }
  if (state === '防守' || state === '偏空') {
    return {
      card: 'border-emerald-200 bg-emerald-50/60',
      badge: 'bg-emerald-600 text-white',
      text: 'text-emerald-700',
    };
  }
  return {
    card: 'border-amber-200 bg-amber-50/60',
    badge: 'bg-amber-500 text-white',
    text: 'text-amber-700',
  };
}

function ChartCard({
  title,
  hint,
  height = 230,
  children,
  className = '',
}: {
  title: string;
  hint: string;
  height?: number;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div
      className={`rounded-2xl border border-slate-200/80 bg-white p-3.5 shadow-sm ${className}`}
    >
      <div className="flex items-baseline justify-between gap-2">
        <h3 className="text-xs font-extrabold text-slate-800">{title}</h3>
        <span className="text-[10px] text-slate-400">{hint}</span>
      </div>
      <div style={{ height }}>{children}</div>
    </div>
  );
}

function LayerCard({ layer }: { layer: RegimeLayer }) {
  return (
    <div className={`rounded-2xl border p-3 ${BORDER[layer.color]}`}>
      <div className="flex items-center gap-2">
        <span
          className="h-2.5 w-2.5 rounded-full shrink-0"
          style={{ background: DOT[layer.color] }}
        />
        <span className="text-xs font-extrabold text-slate-800">{layer.label}</span>
        <span className="ml-auto text-[10px] font-bold text-slate-500 text-right">
          {layer.stage}
        </span>
      </div>
      <div className="mt-2 space-y-1">
        {layer.metrics.map((m) => (
          <div key={m.label} className="flex items-center justify-between text-[11px]">
            <span className="text-slate-500">{m.label}</span>
            <span className="font-mono font-bold text-slate-800">
              {m.value ?? '—'}
              {m.unit}
            </span>
          </div>
        ))}
      </div>
      <p className="mt-2 text-[10px] leading-relaxed text-slate-500">{layer.action}</p>
    </div>
  );
}

function EmptyChart({ text }: { text: string }) {
  return (
    <div className="grid h-full place-items-center text-[11px] font-bold text-slate-300">
      {text}
    </div>
  );
}

function SparkGrid({
  history,
  blocks,
  columns = 2,
  selectedKey,
  onSelect,
}: {
  history: { date: string; scores: number[] }[];
  blocks: { key: string; label: string }[];
  columns?: number;
  selectedKey?: string | null;
  onSelect?: (key: string) => void;
}) {
  if (!history.length) return <EmptyChart text="—" />;
  return (
    <div
      className={`grid ${
        columns >= 6
          ? 'grid-cols-3 lg:grid-cols-6'
          : columns === 1
            ? 'grid-cols-1'
            : 'grid-cols-2'
      } gap-x-2 gap-y-1`}
    >
      {blocks.map((b, i) => {
        const vals = history.map((h) => h.scores[i] ?? 0);
        const last = vals.length ? vals[vals.length - 1] : 0;
        const color = last >= 0 ? '#dc2626' : '#16a34a';
        const option = {
          animation: false,
          grid: { left: 4, right: 6, top: 4, bottom: 4 },
          xAxis: {
            type: 'category',
            show: false,
            boundaryGap: false,
            data: history.map((h) => h.date),
          },
          yAxis: { type: 'value', show: false, min: -2.5, max: 2.5 },
          series: [
            {
              type: 'line',
              data: vals,
              step: 'end',
              symbol: 'none',
              lineStyle: { width: 2, color },
              areaStyle: {
                color:
                  last >= 0
                    ? 'rgba(220,38,38,0.10)'
                    : 'rgba(22,163,74,0.10)',
              },
              markLine: {
                symbol: 'none',
                silent: true,
                lineStyle: { color: '#cbd5e1', width: 1, type: 'dashed' },
                data: [{ yAxis: 0 }],
              },
              markPoint: {
                symbol: 'circle',
                symbolSize: 6,
                data: [
                  {
                    coord: [
                      history[history.length - 1].date,
                      last,
                    ],
                  },
                ],
                itemStyle: { color, borderColor: '#fff', borderWidth: 1.5 },
              },
            },
          ],
        };
        return (
          <button
            key={b.key}
            type="button"
            onClick={() => onSelect?.(b.key)}
            className={`flex w-full flex-col rounded-lg px-1 py-0.5 text-left transition ${
              selectedKey === b.key
                ? 'bg-purple-50 ring-1 ring-purple-300'
                : 'hover:bg-slate-50'
            }`}
          >
            <div className="flex items-center justify-between text-[9px]">
              <span className="text-slate-500">{b.label}</span>
              <span className="font-mono font-bold" style={{ color }}>
                {last > 0 ? `+${last}` : last}
              </span>
            </div>
            <ReactECharts
              option={option}
              notMerge
              lazyUpdate
              style={{ height: columns === 1 ? 55 : 50 }}
            />
          </button>
        );
      })}
    </div>
  );
}

export function MarketRegimePanel({
  onOpenStock,
}: {
  onOpenStock?: (symbol: string) => void;
}) {
  const [data, setData] = useState<RegimeResponse | null>(null);
  const [date, setDate] = useState<string | undefined>();
  const [universe, setUniverse] = useState('ALL');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [selectedSector, setSelectedSector] = useState('');
  const [sectorStocks, setSectorStocks] = useState<SectorStock[]>([]);
  const [sectorLoading, setSectorLoading] = useState(false);
  const [compareOpen, setCompareOpen] = useState(false);
  const [compareLoading, setCompareLoading] = useState(false);
  const [compareError, setCompareError] = useState('');
  const [compareItems, setCompareItems] = useState<CompareItem[] | null>(null);
  const [compareDate, setCompareDate] = useState('');
  const [selectedBlock, setSelectedBlock] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError('');
    fetchMarketRegime(date, universe)
      .then((d) => {
        if (!cancelled) setData(d);
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(e instanceof Error ? e.message : '加载失败');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [date, universe]);

  useEffect(() => {
    if (!data) return;
    setSelectedSector((cur) =>
      cur && data.sector.list.some((s) => s.name === cur)
        ? cur
        : (data.sector.list[0]?.name ?? ''),
    );
  }, [data]);

  useEffect(() => {
    if (!data || !selectedSector) return;
    let cancelled = false;
    setSectorLoading(true);
    fetchSectorStocks(selectedSector, data.date, data.universe)
      .then((r) => {
        if (!cancelled) setSectorStocks(r.stocks);
      })
      .catch(() => {
        if (!cancelled) setSectorStocks([]);
      })
      .finally(() => {
        if (!cancelled) setSectorLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [data, selectedSector]);

  useEffect(() => {
    if (!compareOpen || !data) return;
    if (compareItems && compareDate === data.date) return;
    let cancelled = false;
    setCompareLoading(true);
    setCompareError('');
    fetchRegimeCompare(data.date)
      .then((r) => {
        if (!cancelled) {
          setCompareItems(r.items);
          setCompareDate(r.date ?? data.date);
        }
      })
      .catch((e: unknown) => {
        if (!cancelled) {
          setCompareError(e instanceof Error ? e.message : '加载失败');
        }
      })
      .finally(() => {
        if (!cancelled) setCompareLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [compareOpen, data, compareItems, compareDate]);

  const allowedDates = useMemo(() => new Set(data?.dates ?? []), [data]);
  const hasRs = sectorStocks.some((s) => s.rs_1010 != null);
  const intraBreadth = data?.intraday.breadth[0] ?? null;
  const intraMcl = data?.intraday.mcl_chg5[0] ?? null;
  const prevScores =
    data && data.block_history.length > 20
      ? data.block_history[data.block_history.length - 21].scores
      : null;
  const dragBlocks = data
    ? [...data.blocks].filter((b) => b.score < 0).sort((a, b) => a.score - b.score)
    : [];
  const supportBlocks = data ? data.blocks.filter((b) => b.score > 0) : [];
  const blockDeltas = data
    ? data.blocks
        .map((b, i) => ({
          ...b,
          delta: prevScores ? b.score - prevScores[i] : 0,
        }))
        .filter((b) => b.delta !== 0)
    : [];
  const tone = stateTone(data?.summary.state ?? '');
  const fwdMax = Math.max(
    1,
    ...(compareItems ?? []).flatMap((it) =>
      (['5', '10', '20'] as const).map((h) => Math.abs(it.fwd[h].mean ?? 0)),
    ),
  );
  const selectedBlockData =
    data?.blocks.find((b) => b.key === selectedBlock) ?? null;
  const selectedIndex = data
    ? data.blocks.findIndex((b) => b.key === selectedBlock)
    : -1;
  const selectedPrev =
    selectedIndex >= 0 && prevScores ? prevScores[selectedIndex] : null;
  const selectedDelta =
    selectedBlockData && selectedPrev != null
      ? selectedBlockData.score - selectedPrev
      : null;

  const longOption = useMemo<Record<string, unknown> | null>(() => {
    if (!data) return null;
    const c = data.charts;
    const regime = c.close.map((v, i) =>
      v != null && c.ma250[i] != null ? (v >= (c.ma250[i] as number) ? 1 : 0) : null,
    );
    const areaColor = (v: number | null) =>
      v === 1
        ? 'rgba(220,38,38,0.05)'
        : v === 0
          ? 'rgba(22,163,74,0.05)'
          : 'rgba(148,163,184,0.05)';
    const areas: Array<Array<Record<string, unknown>>> = [];
    let start = 0;
    for (let i = 1; i < regime.length; i += 1) {
      if (regime[i] !== regime[start]) {
        areas.push([
          { xAxis: c.dates[start], itemStyle: { color: areaColor(regime[start]) } },
          { xAxis: c.dates[i - 1] },
        ]);
        start = i;
      }
    }
    if (regime.length) {
      areas.push([
        { xAxis: c.dates[start], itemStyle: { color: areaColor(regime[start]) } },
        { xAxis: c.dates[c.dates.length - 1] },
      ]);
    }
    return {
      grid: { left: 44, right: 14, top: 20, bottom: 24 },
      tooltip: { trigger: 'axis' },
      xAxis: {
        type: 'category',
        data: c.dates,
        boundaryGap: false,
        axisLabel: AXIS_LABEL,
        axisLine: { lineStyle: { color: '#e2e8f0' } },
      },
      yAxis: {
        type: 'value',
        scale: true,
        axisLabel: AXIS_LABEL,
        splitLine: SPLIT_LINE,
      },
      series: [
        {
          name: data.universe_label,
          type: 'line',
          data: c.close,
          symbol: 'none',
          lineStyle: { width: 1.6, color: '#7c3aed' },
          areaStyle: { color: 'rgba(124,58,237,0.04)' },
          markArea: { silent: true, data: areas },
        },
        {
          name: 'MA250',
          type: 'line',
          data: c.ma250,
          symbol: 'none',
          lineStyle: { width: 1.2, color: '#f59e0b', type: 'dashed' },
        },
      ],
    };
  }, [data]);

  const phaseOption = useMemo<Record<string, unknown> | null>(() => {
    if (!data) return null;
    const c = data.charts;
    const n = Math.min(60, c.dates.length);
    const xs = c.above60.slice(-n);
    const ys = c.adl20.slice(-n);
    const pts = xs
      .map((x, i) => (x != null && ys[i] != null ? [x, ys[i]] : null))
      .filter((p): p is number[] => p !== null);
    const cur = pts.length ? pts[pts.length - 1] : null;
    return {
      grid: { left: 48, right: 18, top: 24, bottom: 38 },
      tooltip: {
        formatter: (p: unknown) => {
          const v = (p as { value?: number[] }).value;
          if (!v) return '';
          return `站上MA60 ${v[0]}%<br/>ADL20 ${v[1]}`;
        },
      },
      graphic: [
        { type: 'text', right: 18, top: 26, style: { text: '修复', fill: '#dc2626', fontSize: 10, fontWeight: 'bold' } },
        { type: 'text', left: 56, top: 26, style: { text: '早修复', fill: '#0ea5e9', fontSize: 10, fontWeight: 'bold' } },
        { type: 'text', right: 18, bottom: 42, style: { text: '晚周期', fill: '#f59e0b', fontSize: 10, fontWeight: 'bold' } },
        { type: 'text', left: 56, bottom: 42, style: { text: '中继', fill: '#16a34a', fontSize: 10, fontWeight: 'bold' } },
      ],
      xAxis: {
        type: 'value',
        name: '站上MA60 %',
        nameLocation: 'middle',
        nameGap: 24,
        nameTextStyle: AXIS_LABEL,
        min: 0,
        max: 100,
        axisLabel: AXIS_LABEL,
        splitLine: SPLIT_LINE,
      },
      yAxis: {
        type: 'value',
        name: 'ADL20',
        nameTextStyle: AXIS_LABEL,
        axisLabel: AXIS_LABEL,
        splitLine: SPLIT_LINE,
      },
      series: [
        {
          type: 'line',
          data: pts,
          symbol: 'circle',
          symbolSize: 3,
          lineStyle: { color: '#cbd5e1', width: 1 },
          itemStyle: { color: '#cbd5e1' },
          markLine: {
            symbol: 'none',
            silent: true,
            lineStyle: { color: '#e2e8f0', type: 'dashed' },
            data: [{ xAxis: 50 }, { yAxis: 0 }],
          },
        },
        {
          type: 'scatter',
          data: cur ? [cur] : [],
          symbolSize: 13,
          itemStyle: { color: '#7c3aed', borderColor: '#fff', borderWidth: 2 },
          label: {
            show: true,
            formatter: '当前',
            position: 'top',
            fontSize: 9,
            color: '#7c3aed',
            fontWeight: 'bold',
          },
          z: 10,
        },
      ],
    };
  }, [data]);

  const shortOption = useMemo<Record<string, unknown> | null>(() => {
    if (!data) return null;
    const c = data.charts;
    const n = 60;
    const dates = c.dates.slice(-n);
    const vals = c.up_ratio.slice(-n);
    return {
      grid: { left: 40, right: 14, top: 20, bottom: 22 },
      tooltip: {
        trigger: 'axis',
        formatter: (p: unknown) => {
          const arr = p as Array<{ axisValue: string; value: number }>;
          const v = arr?.[0]?.value;
          return `${arr?.[0]?.axisValue ?? ''}<br/>涨家占比 ${v ?? '—'}%`;
        },
      },
      xAxis: {
        type: 'category',
        data: dates,
        axisLabel: { ...AXIS_LABEL, interval: 9 },
        axisLine: { lineStyle: { color: '#e2e8f0' } },
      },
      yAxis: {
        type: 'value',
        min: 0,
        max: 100,
        axisLabel: { ...AXIS_LABEL, formatter: '{value}%' },
        splitLine: SPLIT_LINE,
      },
      series: [
        {
          name: '涨家占比',
          type: 'bar',
          data: vals.map((v) => ({
            value: v,
            itemStyle: { color: (v ?? 0) >= 50 ? '#dc2626' : '#16a34a' },
          })),
          barWidth: '62%',
          markLine: {
            symbol: 'none',
            label: { fontSize: 9, color: '#64748b' },
            data: [
              { yAxis: 15, lineStyle: { color: '#dc2626', type: 'dashed' }, label: { formatter: '超卖15%' } },
              { yAxis: 80, lineStyle: { color: '#16a34a', type: 'dashed' }, label: { formatter: '过热80%' } },
            ],
          },
        },
      ],
    };
  }, [data]);

  const valuationOption = useMemo<Record<string, unknown> | null>(() => {
    if (!data) return null;
    const c = data.charts;
    return {
      grid: { left: 40, right: 14, top: 20, bottom: 22 },
      tooltip: { trigger: 'axis' },
      xAxis: {
        type: 'category',
        data: c.dates,
        axisLabel: { ...AXIS_LABEL, interval: 24 },
        axisLine: { lineStyle: { color: '#e2e8f0' } },
      },
      yAxis: {
        type: 'value',
        min: 0,
        max: 1,
        axisLabel: AXIS_LABEL,
        splitLine: SPLIT_LINE,
      },
      series: [
        {
          name: 'PB便宜分位',
          type: 'line',
          data: c.pb_cheap,
          symbol: 'none',
          lineStyle: { width: 1.6, color: '#7c3aed' },
          areaStyle: { color: 'rgba(124,58,237,0.07)' },
          markLine: {
            symbol: 'none',
            label: { fontSize: 9, color: '#64748b' },
            data: [
              { yAxis: 0.2, lineStyle: { color: '#16a34a', type: 'dashed' }, label: { formatter: '贵20%' } },
              { yAxis: 0.8, lineStyle: { color: '#dc2626', type: 'dashed' }, label: { formatter: '便宜80%' } },
            ],
          },
        },
      ],
    };
  }, [data]);

  const quadrantOption = useMemo<Record<string, unknown> | null>(() => {
    if (!data) return null;
    const q = data.quadrant;
    const points = q.x.map((x, i) => [x, q.y[i], q.ret[i] ?? 0]);
    return {
      grid: { left: 40, right: 14, top: 18, bottom: 44 },
      tooltip: {
        formatter: (p: unknown) => {
          const arr = p as { value?: [number, number, number] };
          const v = arr?.value;
          if (!v) return '';
          return `距年线 ${v[0]}%<br/>PB便宜分位 ${v[1]}<br/>未来250日 ${v[2]}%`;
        },
      },
      xAxis: {
        type: 'value',
        name: '距年线 %',
        nameTextStyle: AXIS_LABEL,
        axisLabel: AXIS_LABEL,
        splitLine: SPLIT_LINE,
      },
      yAxis: {
        type: 'value',
        name: 'PB便宜分位',
        nameTextStyle: AXIS_LABEL,
        axisLabel: AXIS_LABEL,
        splitLine: SPLIT_LINE,
      },
      visualMap: {
        min: -25,
        max: 25,
        calculable: false,
        orient: 'horizontal',
        left: 'center',
        bottom: 0,
        itemWidth: 10,
        itemHeight: 60,
        textStyle: { fontSize: 9, color: '#64748b' },
        inRange: {
          color: ['#15803d', '#86efac', '#f8fafc', '#fca5a5', '#b91c1c'],
        },
      },
      series: [
        {
          type: 'scatter',
          data: points,
          symbolSize: 7,
          itemStyle: {
            opacity: 0.82,
            borderColor: 'rgba(255,255,255,0.75)',
            borderWidth: 0.6,
          },
          emphasis: { scale: 1.6 },
          markLine: {
            silent: true,
            symbol: 'none',
            label: { show: false },
            lineStyle: { color: '#94a3b8', width: 1, type: 'dashed' },
            data: [{ xAxis: 0 }, { yAxis: 0.5 }],
          },
        },
        {
          type: 'scatter',
          data: [[q.current.x, q.current.y, 0]],
          symbol: 'diamond',
          symbolSize: 14,
          itemStyle: { color: '#7c3aed', borderColor: '#fff', borderWidth: 2 },
          label: {
            show: true,
            formatter: '当前',
            position: 'top',
            fontSize: 9,
            color: '#7c3aed',
            fontWeight: 'bold',
          },
          z: 10,
        },
      ],
    };
  }, [data]);

  const intradayOption = useMemo<Record<string, unknown> | null>(() => {
    if (!data?.intraday.available) return null;
    const it = data.intraday;
    const hasMcl = it.mcl_chg5.some((v) => v !== null);
    return {
      grid: { left: 40, right: hasMcl ? 40 : 14, top: 22, bottom: 22 },
      tooltip: { trigger: 'axis' },
      legend: {
        top: 0,
        right: 0,
        itemWidth: 10,
        itemHeight: 6,
        textStyle: { fontSize: 9, color: '#64748b' },
      },
      xAxis: {
        type: 'category',
        data: it.times,
        axisLabel: AXIS_LABEL,
        axisLine: { lineStyle: { color: '#e2e8f0' } },
      },
      yAxis: [
        { type: 'value', min: 0, max: 100, name: '%', nameTextStyle: AXIS_LABEL, axisLabel: AXIS_LABEL, splitLine: SPLIT_LINE },
        ...(hasMcl
          ? [{ type: 'value', name: 'MCL', nameTextStyle: AXIS_LABEL, axisLabel: AXIS_LABEL, splitLine: { show: false } }]
          : []),
      ],
      series: [
        {
          name: '10:10广度',
          type: 'line',
          data: it.breadth,
          symbol: 'circle',
          symbolSize: 4,
          lineStyle: { width: 1.8, color: '#7c3aed' },
        },
        ...(hasMcl
          ? [
              {
                name: 'MCL5',
                type: 'line',
                yAxisIndex: 1,
                data: it.mcl_chg5,
                symbol: 'none',
                lineStyle: { width: 1.3, color: '#f59e0b' },
              },
            ]
          : []),
      ],
    };
  }, [data]);

  const radarOption = useMemo<Record<string, unknown> | null>(() => {
    if (!data) return null;
    const hist = data.block_history;
    const prev = hist.length > 20 ? hist[hist.length - 21] : null;
    return {
      radar: {
        indicator: data.blocks.map((b) => ({ name: b.label, max: 2, min: -2 })),
        shape: 'polygon',
        radius: '80%',
        center: ['50%', '50%'],
        axisName: { fontSize: 13, color: '#334155', fontWeight: 'bold' },
        splitLine: { lineStyle: { color: '#e2e8f0' } },
        splitArea: { areaStyle: { color: ['#f8fafc', '#ffffff'] } },
      },
      tooltip: { trigger: 'item' },
      series: [
        {
          type: 'radar',
          name: '当前',
          symbolSize: 5,
          data: [
            {
              value: data.blocks.map((b) => b.score),
              name: '当前',
              areaStyle: { color: 'rgba(124,58,237,0.32)' },
              lineStyle: { color: '#7c3aed', width: 3.5 },
              itemStyle: { color: '#7c3aed' },
            },
          ],
        },
        ...(prev
          ? [
              {
                type: 'radar',
                name: '20日前',
                symbolSize: 4,
                data: [
                  {
                    value: prev.scores,
                    name: '20日前',
                    areaStyle: { color: 'rgba(100,116,139,0.10)' },
                    lineStyle: { color: '#64748b', width: 2, type: 'dashed' },
                    itemStyle: { color: '#64748b' },
                  },
                ],
              },
            ]
          : []),
      ],
    };
  }, [data]);


  const sectorTreemapOption = useMemo<Record<string, unknown> | null>(() => {
    if (!data) return null;
    return {
      tooltip: {
        formatter: (p: unknown) => {
          const d = (
            p as {
              data?: {
                name?: string;
                ret?: number;
                up?: number;
                value?: number;
                n?: number;
              };
            }
          ).data;
          if (!d) return '';
          return `${d.name ?? ''}<br/>板块涨跌 ${d.ret ?? '—'}%<br/>上涨占比 ${
            d.up ?? '—'
          }%<br/>成交 ${d.value ?? '—'} 亿<br/>成分 ${d.n ?? '—'}`;
        },
      },
      series: [
        {
          type: 'treemap',
          roam: false,
          nodeClick: false,
          breadcrumb: { show: false },
          width: '100%',
          height: '100%',
          label: {
            show: true,
            fontSize: 9,
            color: '#fff',
            formatter: (p: unknown) => {
              const d = (p as { data?: { name?: string; value?: number } }).data;
              return (d?.value ?? 0) >= 8 ? (d?.name ?? '') : '';
            },
            overflow: 'truncate',
          },
          itemStyle: { borderWidth: 1, gapWidth: 1 },
          data: data.sector.list.map((s) => ({
            name: s.name,
            value: Math.max(s.amount_yi, 0.5),
            n: s.n,
            ret: s.ret,
            up: s.up_share,
            itemStyle: {
              color: retColor(s.ret),
              borderColor:
                s.up_share >= 70 ? '#ffffff' : s.up_share >= 50 ? '#f1f5f9' : '#cbd5e1',
              borderWidth: s.up_share >= 70 ? 2 : 1,
            },
          })),
        },
      ],
    };
  }, [data]);

  const stockBarOption = useMemo<Record<string, unknown> | null>(() => {
    const rows = sectorStocks.slice(0, 15).reverse();
    if (!rows.length) return null;
    return {
      grid: { left: 64, right: 32, top: 8, bottom: 16 },
      tooltip: {
        formatter: (p: unknown) => {
          const d = (
            p as { data?: { name?: string; value?: number; amount?: number } }
          ).data;
          return `${d?.name ?? ''}<br/>涨跌 ${d?.value ?? '—'}%<br/>成交 ${
            d?.amount ?? '—'
          } 亿`;
        },
      },
      xAxis: { type: 'value', axisLabel: AXIS_LABEL, splitLine: SPLIT_LINE },
      yAxis: {
        type: 'category',
        data: rows.map((s) => s.name),
        axisLabel: { ...AXIS_LABEL, width: 58, overflow: 'truncate' },
      },
      series: [
        {
          type: 'bar',
          data: rows.map((s) => ({
            value: s.ret,
            name: s.name,
            symbol: s.symbol,
            amount: s.amount_yi,
            itemStyle: { color: s.ret >= 0 ? '#dc2626' : '#16a34a' },
          })),
          barWidth: 10,
          label: {
            show: true,
            position: 'right',
            fontSize: 9,
            color: '#64748b',
            formatter: '{c}%',
          },
        },
      ],
    };
  }, [sectorStocks]);

  const stockScatterOption = useMemo<Record<string, unknown> | null>(() => {
    const rows = sectorStocks.filter((s) => s.rs_1010 != null);
    if (!rows.length) return null;
    return {
      grid: { left: 52, right: 20, top: 20, bottom: 38 },
      tooltip: {
        formatter: (p: unknown) => {
          const d = (
            p as {
              data?: {
                name?: string;
                value?: number[];
                amount?: number;
              };
            }
          ).data;
          if (!d?.value) return '';
          return `${d.name ?? ''}<br/>日收益 ${d.value[0]}%<br/>10:10相对强弱 ${
            d.value[1]
          }%<br/>成交 ${d.amount ?? '—'} 亿`;
        },
      },
      graphic: [
        { type: 'text', right: 20, top: 22, style: { text: '强中强', fill: '#dc2626', fontSize: 10, fontWeight: 'bold' } },
        { type: 'text', left: 58, top: 22, style: { text: '弱中强', fill: '#f59e0b', fontSize: 10, fontWeight: 'bold' } },
        { type: 'text', right: 20, bottom: 44, style: { text: '强中弱', fill: '#0ea5e9', fontSize: 10, fontWeight: 'bold' } },
        { type: 'text', left: 58, bottom: 44, style: { text: '弱中弱', fill: '#16a34a', fontSize: 10, fontWeight: 'bold' } },
      ],
      xAxis: {
        type: 'value',
        name: '日收益 %',
        nameLocation: 'middle',
        nameGap: 22,
        nameTextStyle: AXIS_LABEL,
        axisLabel: AXIS_LABEL,
        splitLine: SPLIT_LINE,
      },
      yAxis: {
        type: 'value',
        name: '10:10相对强弱 %',
        nameTextStyle: AXIS_LABEL,
        axisLabel: AXIS_LABEL,
        splitLine: SPLIT_LINE,
      },
      series: [
        {
          type: 'scatter',
          data: rows.map((s) => ({
            value: [s.ret, s.rs_1010 as number, s.amount_yi],
            name: s.name,
            symbol: s.symbol,
            amount: s.amount_yi,
            itemStyle: { color: quadColor(s.ret, s.rs_1010 as number) },
          })),
          symbolSize: (v: number[]) =>
            Math.min(18, 5 + Math.sqrt(Math.max(v[2] ?? 0, 0)) * 2),
          markLine: {
            symbol: 'none',
            silent: true,
            lineStyle: { color: '#e2e8f0', type: 'dashed' },
            data: [{ xAxis: 0 }, { yAxis: 0 }],
          },
        },
      ],
    };
  }, [sectorStocks]);

  const onTreemapClick = (params: unknown) => {
    const p = params as { name?: string };
    if (p?.name) setSelectedSector(p.name);
  };
  const onStockClick = (params: unknown) => {
    const p = params as { data?: { symbol?: string } };
    if (p?.data?.symbol && onOpenStock) onOpenStock(p.data.symbol);
  };

  return (
    <div className="flex flex-col gap-3.5">
      <div className="flex flex-wrap items-center gap-2.5 rounded-2xl border border-purple-100/80 bg-white px-3.5 py-2.5 shadow-sm">
        <div className="flex items-center gap-2">
          <span className="grid h-8 w-8 place-items-center rounded-xl bg-gradient-to-br from-purple-600 to-indigo-600 text-white">
            <Gauge className="h-4 w-4" />
          </span>
          <div>
            <h2 className="text-sm font-extrabold text-slate-800">市场阶段 · 五层判断</h2>
            <p className="text-[10px] text-slate-400">
              长期定天花板 · 中期定仓位 · 短期定买点 · 当日定执行 · 个股定买什么
            </p>
            <p className="text-[10px] font-bold text-purple-600">
              当前口径：{data?.universe_label ?? '全A等权'} 成分股 · 估值分位=成分股内
            </p>
          </div>
        </div>

        <div className="flex items-center gap-1.5">
          <span className="text-[10px] font-bold text-slate-400">指数</span>
          <Select
            size="small"
            value={universe}
            onChange={(v) => setUniverse(v)}
            options={(data?.universes ?? []).map((u) => ({
              value: u.code,
              label: u.label,
            }))}
            className="!w-32"
            popupMatchSelectWidth={false}
          />
        </div>

        <div className="ml-auto flex items-center gap-2">
          {data && (
            <span className="flex items-center gap-1 rounded-full border border-slate-200/70 bg-slate-50 px-2.5 py-1 text-[10px] font-bold text-slate-600">
              <Clock className="h-3 w-3 text-purple-500" />
              数据日期 <span className="font-mono text-purple-700">{data.date}</span>
            </span>
          )}
          <DatePicker
            allowClear
            size="small"
            value={date ? dayjs(date) : null}
            onChange={(d) => setDate(d ? d.format('YYYY-MM-DD') : undefined)}
            disabledDate={(d) =>
              allowedDates.size > 0 && !allowedDates.has(d.format('YYYY-MM-DD'))
            }
            placeholder="选择日期（默认最新）"
            className="!w-40"
          />
          <button
            type="button"
            onClick={() => setDate(undefined)}
            className="flex items-center gap-1 rounded-full border border-slate-200 bg-white px-2.5 py-1 text-[10px] font-bold text-slate-600 hover:border-purple-300 hover:text-purple-600"
          >
            <RefreshCw className={`h-3 w-3 ${loading ? 'animate-spin' : ''}`} />
            最新
          </button>
        </div>
      </div>

      <div className="rounded-2xl border border-slate-200/80 bg-white shadow-sm">
        <button
          type="button"
          onClick={() => setCompareOpen((v) => !v)}
          className="flex w-full items-center gap-2 px-3.5 py-2.5 text-left"
        >
          <span className="text-xs font-extrabold text-slate-800">多指数对比</span>
          <span className="text-[10px] text-slate-400">
            6 个指数 · 压力条 / 分位 / T+5·10·20
          </span>
          <span className="ml-auto text-[10px] text-slate-400">
            {compareLoading ? '计算中…' : compareOpen ? '收起' : '展开'}
          </span>
          {compareOpen ? (
            <ChevronUp className="h-3.5 w-3.5 text-slate-400" />
          ) : (
            <ChevronDown className="h-3.5 w-3.5 text-slate-400" />
          )}
        </button>
        {compareOpen && (
          <div className="border-t border-slate-100 px-3.5 pb-3 pt-2">
            {compareLoading ? (
              <div className="py-6 text-center text-[11px] font-bold text-slate-400">
                首次计算约 20-30 秒（之后走缓存）…
              </div>
            ) : compareError ? (
              <div className="py-4 text-[11px] font-bold text-rose-600">
                加载失败：{compareError}
              </div>
            ) : compareItems ? (
              <div className="flex flex-col gap-1.5">
                <div className="flex items-center justify-between px-1 text-[9px] text-slate-400">
                  <span>压力分 = 100 − 状态分（越高越承压）</span>
                  <span className="flex items-center gap-1.5">
                    <span>0</span>
                    <span className="h-1.5 w-16 rounded-full bg-gradient-to-r from-emerald-400 via-amber-400 to-rose-500" />
                    <span>100</span>
                  </span>
                </div>
                {compareItems.map((it) => {
                  const chg = it.chg;
                  const pressureColor =
                    it.pressure >= 70
                      ? '#e11d48'
                      : it.pressure >= 50
                        ? '#d97706'
                        : '#16a34a';
                  const selected = it.code === data.universe;
                  return (
                    <button
                      key={it.code}
                      type="button"
                      onClick={() => setUniverse(it.code)}
                      className={`grid w-full grid-cols-1 gap-2 rounded-xl border px-3 py-2 text-left transition sm:grid-cols-12 ${
                        selected
                          ? 'border-purple-300 bg-purple-50/70 shadow-sm'
                          : 'border-slate-100 bg-white hover:border-purple-200 hover:bg-purple-50/30'
                      }`}
                    >
                      <div className="min-w-0 sm:col-span-3">
                        <div className="flex items-center gap-1.5">
                          <span
                            className="h-2 w-2 shrink-0 rounded-full"
                            style={{ background: pressureColor }}
                          />
                          <span className="truncate text-[11px] font-extrabold text-slate-800">
                            {it.label}
                          </span>
                          <span
                            className={`rounded-full px-1.5 py-0.5 text-[9px] font-black ${
                              chg != null && chg >= 0
                                ? 'bg-rose-50 text-rose-600'
                                : 'bg-emerald-50 text-emerald-600'
                            }`}
                          >
                            {chg == null ? '—' : `${chg > 0 ? '+' : ''}${chg}%`}
                          </span>
                        </div>
                        <div className="mt-0.5 text-[9px] text-slate-400">
                          {it.state} · 评分 {it.score > 0 ? `+${it.score}` : it.score}
                        </div>
                      </div>

                      <div className="sm:col-span-4">
                        <div className="flex items-center gap-2">
                          <div className="relative h-2.5 flex-1 rounded-full bg-slate-100">
                            <div
                              className="absolute inset-y-0 left-0 rounded-full"
                              style={{
                                width: `${it.pressure}%`,
                                background:
                                  'linear-gradient(90deg,#34d399 0%,#f59e0b 55%,#f43f5e 100%)',
                              }}
                            />
                            <div
                              className="absolute w-[2px] bg-slate-800"
                              style={{
                                top: '-3px',
                                bottom: '-3px',
                                left: `${it.pressure}%`,
                              }}
                            />
                          </div>
                          <span
                            className="w-7 text-right font-mono text-[11px] font-black"
                            style={{ color: pressureColor }}
                          >
                            {it.pressure}
                          </span>
                        </div>
                        <div className="mt-0.5 flex justify-between text-[8px] text-slate-400">
                          <span>压力分</span>
                          <span>
                            {it.pctile == null
                              ? '—'
                              : `P${Math.round(it.pctile * 100)}`}
                          </span>
                        </div>
                      </div>

                      <div className="grid grid-cols-3 gap-2 sm:col-span-5">
                        {(['5', '10', '20'] as const).map((h) => {
                          const f = it.fwd[h];
                          const mean = f.mean ?? 0;
                          const width = Math.min(
                            50,
                            (Math.abs(mean) / fwdMax) * 50,
                          );
                          return (
                            <div key={h}>
                              <div className="flex items-center justify-between text-[8px] text-slate-400">
                                <span>T+{h}</span>
                                <span>{f.win == null ? '—' : `${f.win}%`}</span>
                              </div>
                              <div className="relative mt-1 h-1.5 rounded-full bg-slate-100">
                                <div className="absolute inset-y-0 left-1/2 w-[1px] bg-slate-200" />
                                <div
                                  className="absolute inset-y-0 rounded-full"
                                  style={{
                                    left: mean >= 0 ? '50%' : `${50 - width}%`,
                                    width: `${width}%`,
                                    background: mean >= 0 ? '#dc2626' : '#16a34a',
                                  }}
                                />
                              </div>
                              <div
                                className="mt-0.5 text-right font-mono text-[10px] font-bold"
                                style={{
                                  color:
                                    f.mean == null
                                      ? '#94a3b8'
                                      : f.mean >= 0
                                        ? '#dc2626'
                                        : '#16a34a',
                                }}
                              >
                                {f.mean == null
                                  ? '—'
                                  : `${f.mean > 0 ? '+' : ''}${f.mean}%`}
                                <span className="ml-0.5 text-[8px] font-normal text-slate-400">
                                  n{f.n}
                                </span>
                              </div>
                            </div>
                          );
                        })}
                      </div>
                    </button>
                  );
                })}
              </div>
            ) : (
              <div className="py-4 text-center text-[11px] text-slate-400">暂无数据</div>
            )}
            <p className="mt-1.5 text-[9px] leading-relaxed text-slate-400">
              压力分 = 100 − 状态分（越高越承压）；分位 = 近 560 日扩张分位；
              T+5/10/20 = 同压力分位桶条件收益 / 胜率（近 560 日，样本有限）。
              点击行切换主面板指数。
            </p>
          </div>
        )}
      </div>

      {error && (
        <div className="rounded-xl border border-rose-200 bg-rose-50 px-3 py-2 text-[11px] font-bold text-rose-700">
          加载失败：{error}
        </div>
      )}

      {!data ? (
        <div className="grid h-60 place-items-center text-[11px] font-bold text-slate-300">
          {loading ? '加载中…' : '暂无数据'}
        </div>
      ) : (
        <>
          <div className="grid grid-cols-2 gap-3 lg:grid-cols-5">
            {data.layers.map((l) => (
              <LayerCard key={l.key} layer={l} />
            ))}
          </div>

          <div className="grid grid-cols-1 gap-3 lg:grid-cols-12">
            <div className="lg:col-span-4">
              <ChartCard
                title="当前状态雷达（六块评分）"
                hint="紫=当前，灰虚线=20日前；点底部标签"
                height={480}
                className="h-full"
              >
                <div className="flex h-full flex-col">
                  <div className="min-h-[340px] flex-1">
                    {radarOption ? (
                      <ReactECharts
                        option={radarOption}
                        notMerge
                        lazyUpdate
                        style={{ height: '100%' }}
                      />
                    ) : (
                      <EmptyChart text="—" />
                    )}
                  </div>
                  {selectedBlockData && (
                    <div className="mb-2 rounded-lg border border-purple-200 bg-purple-50/60 px-2.5 py-1.5">
                      <div className="flex flex-wrap items-baseline gap-x-2 text-[10px]">
                        <span className="font-bold text-purple-700">
                          {selectedBlockData.label}
                        </span>
                        <span className="text-slate-600">
                          {selectedBlockData.read}：{selectedBlockData.value}
                          {selectedBlockData.unit}
                        </span>
                        <span className="text-slate-400">
                          20日前 {selectedPrev ?? '—'} → 当前{' '}
                          {selectedBlockData.score}
                          {selectedDelta != null
                            ? `（${selectedDelta > 0 ? '+' : ''}${selectedDelta}）`
                            : ''}
                        </span>
                      </div>
                    </div>
                  )}
                  <div className="mt-auto grid grid-cols-3 gap-1.5 border-t border-slate-100 pt-2">
                    {data.blocks.map((b) => {
                      const active = selectedBlock === b.key;
                      return (
                        <button
                          key={b.key}
                          type="button"
                          onClick={() =>
                            setSelectedBlock(active ? null : b.key)
                          }
                          className={`w-full rounded-lg border px-1 py-1.5 text-center text-[10px] font-bold transition ${
                            active
                              ? 'border-purple-400 bg-purple-50 text-purple-700 ring-1 ring-purple-300'
                              : b.score >= 0
                                ? 'border-rose-100 bg-rose-50 text-rose-700 hover:border-rose-300'
                                : 'border-emerald-100 bg-emerald-50 text-emerald-700 hover:border-emerald-300'
                          }`}
                        >
                          <span className="block text-[9px] font-medium text-slate-500">
                            {b.label}
                          </span>
                          <span className="text-[12px]">
                            {b.score > 0 ? `+${b.score}` : b.score}
                          </span>
                        </button>
                      );
                    })}
                  </div>
                </div>
              </ChartCard>
            </div>
            <div className="lg:col-span-4">
              <ChartCard
                title="六块趋势（60日）"
                hint="点任一块选中；红=当前正，绿=当前负"
                height={480}
                className="h-full"
              >
                <SparkGrid
                  history={data.block_history}
                  blocks={data.blocks}
                  columns={1}
                  selectedKey={selectedBlock}
                  onSelect={(k) =>
                    setSelectedBlock(selectedBlock === k ? null : k)
                  }
                />
              </ChartCard>
            </div>
            <div className="lg:col-span-4">
              <div className={`flex h-full flex-col rounded-2xl border p-4 ${tone.card}`}>
                <div className="flex items-start justify-between gap-2">
                  <div>
                    <div className="text-[10px] font-bold uppercase tracking-wide text-slate-400">
                      综合状态
                    </div>
                    <div className="mt-1 flex items-baseline gap-2">
                      <span className={`text-3xl font-black ${tone.text}`}>
                        {data.summary.state}
                      </span>
                      <span className="font-mono text-[11px] font-bold text-slate-500">
                        {data.summary.score > 0 ? '+' : ''}
                        {data.summary.score} / ±12
                      </span>
                    </div>
                  </div>
                  <span className={`rounded-full px-2.5 py-1 text-[10px] font-black ${tone.badge}`}>
                    {data.summary.state}
                  </span>
                </div>
                <p className="mt-2 text-[11px] leading-relaxed text-slate-700">
                  {data.summary.one_liner}
                </p>
                <p className="mt-1 text-[10px] leading-relaxed text-slate-500">
                  {STATE_EXPLAIN[data.summary.state] ?? ''}
                </p>

                {selectedBlockData && (
                  <div className="mt-2 rounded-lg border border-purple-200 bg-white/80 px-2 py-1">
                    <div className="text-[9px] font-bold text-purple-600">
                      选中：{selectedBlockData.label}
                    </div>
                    <div className="text-[9px] text-slate-600">
                      {selectedBlockData.read}；当前 {selectedBlockData.value}
                      {selectedBlockData.unit}；20日前 {selectedPrev ?? '—'} →{' '}
                      {selectedBlockData.score}
                    </div>
                  </div>
                )}

                <div className="mt-3 grid grid-cols-2 gap-1.5">
                  {data.layers
                    .filter((l) => l.key !== 'stock')
                    .map((l) => (
                      <div
                        key={l.key}
                        className="rounded-xl border border-white/80 bg-white/70 px-2 py-1.5"
                      >
                        <div className="flex items-center gap-1.5">
                          <span
                            className="h-1.5 w-1.5 rounded-full"
                            style={{ background: DOT[l.color] }}
                          />
                          <span className="text-[10px] font-bold text-slate-700">
                            {l.label}
                          </span>
                          <span className="ml-auto max-w-[62%] truncate text-[9px] text-slate-400">
                            {l.stage}
                          </span>
                        </div>
                        <div className="mt-0.5 truncate text-[9px] text-slate-500">
                          {l.action}
                        </div>
                      </div>
                    ))}
                </div>

                <div className="mt-3 grid grid-cols-2 gap-2">
                  <div>
                    <div className="text-[9px] font-bold text-slate-400">拖累项</div>
                    <div className="mt-1 flex flex-wrap gap-1">
                      {dragBlocks.length ? (
                        dragBlocks.slice(0, 3).map((b) => (
                          <span
                            key={b.key}
                            className="rounded-full bg-emerald-100 px-1.5 py-0.5 text-[9px] font-bold text-emerald-700"
                          >
                            {b.label} {b.score}
                          </span>
                        ))
                      ) : (
                        <span className="text-[9px] text-slate-400">无</span>
                      )}
                    </div>
                  </div>
                  <div>
                    <div className="text-[9px] font-bold text-slate-400">支撑项</div>
                    <div className="mt-1 flex flex-wrap gap-1">
                      {supportBlocks.length ? (
                        supportBlocks.slice(0, 3).map((b) => (
                          <span
                            key={b.key}
                            className="rounded-full bg-rose-100 px-1.5 py-0.5 text-[9px] font-bold text-rose-700"
                          >
                            {b.label} +{b.score}
                          </span>
                        ))
                      ) : (
                        <span className="text-[9px] text-slate-400">无</span>
                      )}
                    </div>
                  </div>
                </div>

                {blockDeltas.length > 0 && (
                  <div className="mt-2">
                    <div className="text-[9px] font-bold text-slate-400">较20日前</div>
                    <div className="mt-1 flex flex-wrap gap-1">
                      {blockDeltas.map((b) => (
                        <span
                          key={b.key}
                          className={`rounded-full px-1.5 py-0.5 text-[9px] font-bold ${
                            b.delta > 0
                              ? 'bg-rose-50 text-rose-700'
                              : 'bg-emerald-50 text-emerald-700'
                          }`}
                        >
                          {b.label} {b.delta > 0 ? '+' : ''}
                          {b.delta}
                        </span>
                      ))}
                    </div>
                  </div>
                )}

                <div className="mt-auto rounded-xl border border-purple-100 bg-white/80 px-2.5 py-2">
                  <div className="text-[9px] font-bold text-purple-600">当前怎么做</div>
                  <ul className="mt-1 space-y-0.5 text-[9px] leading-relaxed text-slate-600">
                    <li>
                      · 基础暴露 <b>{data.exposure.actual}</b>（天花板{' '}
                      {data.exposure.ceiling}）
                    </li>
                    <li>
                      · 短期：{data.layers.find((l) => l.key === 'short')?.stage} →{' '}
                      {data.layers.find((l) => l.key === 'short')?.action}
                    </li>
                    <li>· 顶风险：{data.top_risk.text}</li>
                    <li>· 底部：{data.bottom.text}</li>
                  </ul>
                </div>
              </div>
            </div>
          </div>

          <div className="grid grid-cols-1 gap-3 lg:grid-cols-12">
            <div className="lg:col-span-7">
              <ChartCard
                title="板块热力图"
                hint={`${data.universe_label} 内 · 上涨板块 ${data.sector.up_share}% · 离散度 ${data.sector.dispersion} · 点板块看个股`}
                height={330}
              >
                {sectorTreemapOption ? (
                  <ReactECharts
                    option={sectorTreemapOption}
                    notMerge
                    lazyUpdate
                    onEvents={{ click: onTreemapClick }}
                    style={{ height: 330 }}
                  />
                ) : (
                  <EmptyChart text="该日期无板块数据" />
                )}
              </ChartCard>
            </div>
            <div className="lg:col-span-5">
              <ChartCard
                title={`板块个股 · ${selectedSector || '—'}`}
                hint={
                  sectorLoading
                    ? '加载中…'
                    : hasRs
                      ? 'x=日收益，y=10:10相对强弱；右上=强中强，点击打开股票终端'
                      : '该日无10:10数据，按日收益排序；点击条形打开股票终端'
                }
                height={330}
              >
                {hasRs && stockScatterOption ? (
                  <ReactECharts
                    option={stockScatterOption}
                    notMerge
                    lazyUpdate
                    onEvents={{ click: onStockClick }}
                    style={{ height: 330 }}
                  />
                ) : stockBarOption ? (
                  <ReactECharts
                    option={stockBarOption}
                    notMerge
                    lazyUpdate
                    onEvents={{ click: onStockClick }}
                    style={{ height: 330 }}
                  />
                ) : (
                  <EmptyChart text={sectorLoading ? '加载中…' : '该板块无个股数据'} />
                )}
              </ChartCard>
            </div>
          </div>

          <div className="grid grid-cols-1 gap-3 lg:grid-cols-12">
            <div className="lg:col-span-8">
              <ChartCard
                title={`长期：${data.universe_label} vs 年线（MA250）`}
                hint="紫=指数，橙虚线=年线；背景红=年线上，绿=年线下"
                height={250}
              >
                {longOption ? (
                  <ReactECharts option={longOption} notMerge lazyUpdate style={{ height: 250 }} />
                ) : (
                  <EmptyChart text="—" />
                )}
              </ChartCard>
            </div>
            <div className="lg:col-span-4">
              <ChartCard
                title="基础暴露"
                hint={`实际 ${data.exposure.actual} / 天花板 ${data.exposure.ceiling}`}
                height={250}
              >
                <div className="px-2 pt-4">
                  <div className="relative h-8 w-full rounded-full bg-slate-100">
                    <div
                      className="absolute inset-y-0 left-0 rounded-full bg-purple-600/90"
                      style={{
                        width: `${Math.min(100, Math.max(0, data.exposure.actual * 100))}%`,
                      }}
                    />
                    <div
                      className="absolute w-[2px] bg-slate-700"
                      style={{
                        top: '-4px',
                        bottom: '-4px',
                        left: `${Math.min(100, Math.max(0, data.exposure.ceiling * 100))}%`,
                      }}
                    />
                  </div>
                  <div className="mt-2 flex items-center justify-between text-[9px] font-mono text-slate-400">
                    <span>0</span>
                    <span className="text-purple-700 font-bold">
                      实际 {data.exposure.actual}
                    </span>
                    <span>天花板 {data.exposure.ceiling}</span>
                    <span>1.0</span>
                  </div>
                  <div className="mt-1 text-[9px] text-slate-400">
                    紫条=实际仓位；黑线=天花板；灰底=满仓轨道
                  </div>
                </div>
                <div className="space-y-1 text-[10px] text-slate-500">
                  <p className="flex items-center justify-between">
                    <span>实际仓位</span>
                    <span className="font-mono font-bold text-slate-800">{data.exposure.actual}</span>
                  </p>
                  <p className="flex items-center justify-between">
                    <span>天花板</span>
                    <span className="font-mono font-bold text-slate-800">{data.exposure.ceiling}</span>
                  </p>
                  <p className="pt-1 text-[10px] leading-relaxed text-slate-400">
                    {data.top_risk.flag ? `⚠ ${data.top_risk.text}` : '顶风险：无 ADL 背离'}
                  </p>
                </div>
              </ChartCard>
            </div>

            <div className="lg:col-span-5">
              <ChartCard
                title="长期 2×2：趋势 × 估值"
                hint="2018 起隔日取样；颜色=未来250日收益"
                height={300}
              >
                {quadrantOption ? (
                  <ReactECharts option={quadrantOption} notMerge lazyUpdate style={{ height: 300 }} />
                ) : (
                  <EmptyChart text="—" />
                )}
              </ChartCard>
            </div>
            <div className="lg:col-span-7">
              <ChartCard
                title="中期：广度相位图"
                hint="x=站上MA60，y=ADL20；轨迹=最近60日；右上=修复，左下=中继"
                height={300}
              >
                {phaseOption ? (
                  <ReactECharts
                    option={phaseOption}
                    notMerge
                    lazyUpdate
                    style={{ height: 300 }}
                  />
                ) : (
                  <EmptyChart text="—" />
                )}
              </ChartCard>
            </div>

            <div className="lg:col-span-6">
              <ChartCard
                title="短期：收盘涨家占比"
                hint="<15% 超卖（反抽候选）；>80% 过热（不追）"
                height={230}
              >
                {shortOption ? (
                  <ReactECharts option={shortOption} notMerge lazyUpdate style={{ height: 230 }} />
                ) : (
                  <EmptyChart text="—" />
                )}
              </ChartCard>
            </div>
            <div className="lg:col-span-6">
              <ChartCard
                title="估值：PB 便宜分位"
                hint=">0.8 便宜（底部区）；<0.2 贵（防守）"
                height={230}
              >
                {valuationOption ? (
                  <ReactECharts option={valuationOption} notMerge lazyUpdate style={{ height: 230 }} />
                ) : (
                  <EmptyChart text="—" />
                )}
              </ChartCard>
            </div>

            <div className="lg:col-span-12">
              <ChartCard
                title="当日：10:10 广度 + MCL"
                hint={
                  data.intraday.available
                    ? `${data.intraday.source ?? ''} · ${data.intraday.note}`
                    : data.intraday.note
                }
                height={230}
              >
                {!data.intraday.available ? (
                  <EmptyChart text="该日期无盘中数据" />
                ) : data.intraday.times.length <= 1 ? (
                  <div className="flex h-full flex-col justify-center gap-3 px-4">
                    <div>
                      <div className="text-[10px] text-slate-400">10:10 广度</div>
                      <div
                        className="text-3xl font-black"
                        style={{
                          color:
                            (intraBreadth ?? 0) >= 50 ? '#dc2626' : '#16a34a',
                        }}
                      >
                        {intraBreadth ?? '—'}%
                      </div>
                      <div className="relative mt-2 h-4 w-full rounded-full bg-slate-100">
                        <div
                          className="absolute inset-y-0 left-0 rounded-full bg-purple-600/90"
                          style={{
                            width: `${Math.min(100, Math.max(0, intraBreadth ?? 0))}%`,
                          }}
                        />
                        <div
                          className="absolute w-[2px] bg-slate-700"
                          style={{ top: '-3px', bottom: '-3px', left: '30%' }}
                        />
                        <div
                          className="absolute w-[2px] bg-slate-700"
                          style={{ top: '-3px', bottom: '-3px', left: '60%' }}
                        />
                      </div>
                      <div className="mt-1 flex justify-between text-[9px] text-slate-400">
                        <span>0</span>
                        <span>弱 30</span>
                        <span>强 60</span>
                        <span>100</span>
                      </div>
                    </div>
                    <div className="text-[11px] text-slate-500">
                      MCL5：
                      <span className="font-mono font-bold text-slate-800">
                        {intraMcl ?? '—'}
                      </span>
                    </div>
                  </div>
                ) : intradayOption ? (
                  <ReactECharts
                    option={intradayOption}
                    notMerge
                    lazyUpdate
                    style={{ height: 230 }}
                  />
                ) : (
                  <EmptyChart text="该日期无盘中数据" />
                )}
              </ChartCard>
            </div>
          </div>

          <div className="rounded-2xl border border-slate-200/80 bg-white px-3.5 py-2.5 text-[10px] leading-relaxed text-slate-500">
            <p className="flex items-center gap-1.5 font-bold text-slate-700">
              <Activity className="h-3.5 w-3.5 text-purple-500" />
              一句话读法
            </p>
            <p className="mt-1">
              长期定天花板，中期定实际仓位，短期定买点，当日定今天做不做，个股定买什么；
              上层压下层，下层不能反顶上。当前：{data.layers[0]?.stage} / {data.layers[1]?.stage} /{' '}
              {data.layers[2]?.stage} / {data.layers[3]?.stage}。
            </p>
            <p className="mt-1 text-slate-400">
              {data.universe_note}。概率判断，不是预言；未扣费；底部组合/ADL
              背离等样本有限，按“风险提示”用，不重仓外推。
            </p>
          </div>

          <IndicatorHealthCard data={data} />
        </>
      )}
    </div>
  );
}
