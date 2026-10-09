/** 因子研究 —— 共享小组件：KPI 磁贴 / 净值图 / IC 图 / 分布条 / 标签 / 区间选择器 */
import React from 'react';
import { EChartsChart } from '../../../components/common/EChartsChart';
import type { DistRow, FactorKpi, SeriesPoint } from '../types/factorResearch';

/** 多因子对比配色（首个为站内主色，后续为高区分度序列） */
export const FACTOR_COLORS = ['#2563eb', '#e11d48', '#059669', '#d97706', '#7c3aed', '#0891b2', '#be185d', '#4d7c0f', '#b45309', '#1d4ed8', '#9f1239', '#065f46'];

export const fmtPct = (v?: number | null, digits = 1): string =>
  v === null || v === undefined || Number.isNaN(v) ? '—' : `${(v * 100).toFixed(digits)}%`;

export const fmtNum = (v?: number | null, digits = 2): string =>
  v === null || v === undefined || Number.isNaN(v) ? '—' : v.toFixed(digits);

interface KpiTilesProps {
  kpi: FactorKpi;
  compact?: boolean;
}

/** KPI 磁贴：年化 / 夏普 / 最大回撤 / 月胜率 / IC 均值 / ICIR */
export const KpiTiles: React.FC<KpiTilesProps> = ({ kpi, compact }) => {
  const items: Array<{ label: string; value: string; tone?: 'pos' | 'neg' }> = [
    { label: '年化收益', value: fmtPct(kpi.annual_return), tone: (kpi.annual_return || 0) >= 0 ? 'pos' : 'neg' },
    { label: '夏普', value: fmtNum(kpi.sharpe), tone: (kpi.sharpe || 0) >= 0 ? 'pos' : 'neg' },
    { label: '最大回撤', value: fmtPct(kpi.max_drawdown) },
    { label: '月胜率', value: fmtPct(kpi.win_rate) },
    { label: 'IC 均值', value: fmtNum(kpi.ic_mean, 3), tone: (kpi.ic_mean || 0) >= 0 ? 'pos' : 'neg' },
    { label: 'ICIR', value: fmtNum(kpi.ic_ir, 2) },
  ];
  return (
    <div className={`grid ${compact ? 'grid-cols-3' : 'grid-cols-3 lg:grid-cols-6'} gap-2`}>
      {items.map((it) => (
        <div key={it.label} className="rounded-xl border border-slate-200/80 bg-white px-3 py-2">
          <div className="text-[10px] font-bold text-slate-400">{it.label}</div>
          <div
            className={`text-sm font-extrabold font-mono ${
              it.tone === 'pos' ? 'text-rose-600' : it.tone === 'neg' ? 'text-emerald-600' : 'text-slate-700'
            }`}
          >
            {it.value}
          </div>
        </div>
      ))}
    </div>
  );
};

interface NavChartProps {
  series: Array<{ name: string; data: SeriesPoint[]; color?: string; dashed?: boolean }>;
  height?: number;
}

/** 净值曲线（多序列 + 可选基准虚线） */
export const NavChart: React.FC<NavChartProps> = ({ series, height = 180 }) => {
  const dates = series[0]?.data.map((p) => p.date) || [];
  const option = {
    grid: { left: 44, right: 12, top: 24, bottom: 22 },
    tooltip: { trigger: 'axis' },
    legend: series.length > 1 ? { top: 0, textStyle: { fontSize: 10, color: '#64748b' } } : undefined,
    xAxis: { type: 'category', data: dates, axisLabel: { fontSize: 9, color: '#94a3b8' }, axisTick: { show: false } },
    yAxis: { type: 'value', scale: true, axisLabel: { fontSize: 9, color: '#94a3b8' }, splitLine: { lineStyle: { color: '#f1f5f9' } } },
    series: series.map((s, i) => ({
      name: s.name,
      type: 'line',
      data: s.data.map((p) => p.value),
      showSymbol: false,
      lineStyle: {
        width: s.dashed ? 1.2 : i === 0 ? 1.8 : 1.4,
        color: s.color || FACTOR_COLORS[i % FACTOR_COLORS.length],
        type: s.dashed ? 'dashed' : 'solid',
      },
      itemStyle: { color: s.color || FACTOR_COLORS[i % FACTOR_COLORS.length] },
      emphasis: { focus: 'series' },
    })),
  };
  return (
    <div style={{ height }}>
      <EChartsChart option={option} />
    </div>
  );
};

interface IcChartProps {
  points: SeriesPoint[];
  height?: number;
}

/** 月频 IC：柱（红正/绿负）+ 12 期滚动均值线 */
export const IcChart: React.FC<IcChartProps> = ({ points, height = 160 }) => {
  const dates = points.map((p) => p.date);
  const vals = points.map((p) => p.value);
  const roll: number[] = vals.map((_, i) => {
    const w = vals.slice(Math.max(0, i - 11), i + 1);
    return w.reduce((a, b) => a + b, 0) / w.length;
  });
  const option = {
    grid: { left: 44, right: 12, top: 16, bottom: 22 },
    tooltip: { trigger: 'axis' },
    xAxis: { type: 'category', data: dates, axisLabel: { fontSize: 9, color: '#94a3b8' }, axisTick: { show: false } },
    yAxis: { type: 'value', axisLabel: { fontSize: 9, color: '#94a3b8' }, splitLine: { lineStyle: { color: '#f1f5f9' } } },
    series: [
      {
        name: 'IC',
        type: 'bar',
        data: vals.map((v) => ({ value: v, itemStyle: { color: v >= 0 ? 'rgba(225,29,72,0.65)' : 'rgba(5,150,105,0.65)' } })),
        barMaxWidth: 8,
      },
      { name: '12期均值', type: 'line', data: roll, showSymbol: false, lineStyle: { width: 1.5, color: '#2563eb' } },
    ],
  };
  return (
    <div style={{ height }}>
      <EChartsChart option={option} />
    </div>
  );
};

/** 持仓数扫描：期末净值 vs N（折线） */
export const NScanChart: React.FC<{ rows: Array<{ n: number; final_nav: number }>; height?: number }> = ({
  rows,
  height = 160,
}) => {
  const option = {
    grid: { left: 44, right: 12, top: 14, bottom: 22 },
    tooltip: {
      trigger: 'axis',
      formatter: (ps: Array<{ dataIndex: number }>) => {
        const r = rows[ps[0]?.dataIndex ?? 0];
        return r ? `Top-${r.n}<br/>期末净值 ${r.final_nav.toFixed(3)}` : '';
      },
    },
    xAxis: { type: 'category', data: rows.map((r) => String(r.n)), axisLabel: { fontSize: 9, color: '#94a3b8' }, axisTick: { show: false } },
    yAxis: { type: 'value', scale: true, axisLabel: { fontSize: 9, color: '#94a3b8' }, splitLine: { lineStyle: { color: '#f1f5f9' } } },
    series: [
      {
        type: 'line',
        data: rows.map((r) => r.final_nav),
        showSymbol: false,
        lineStyle: { width: 1.6, color: '#7c3aed' },
        areaStyle: { opacity: 0.08 },
        markLine: {
          symbol: 'none',
          silent: true,
          lineStyle: { color: '#cbd5e1', type: 'dashed' },
          data: [{ yAxis: 1 }],
          label: { show: false },
        },
      },
    ],
  };
  return (
    <div style={{ height }}>
      <EChartsChart option={option} />
    </div>
  );
};

/** 横向分布条（行业/市值分布） */
export const DistBars: React.FC<{ rows: DistRow[]; color?: string; max?: number; height?: number }> = ({
  rows,
  color = '#6366f1',
  max,
  height = 150,
}) => {
  const total = rows.reduce((a, b) => a + b.count, 0) || 1;
  const cap = max ?? Math.max(...rows.map((r) => r.count), 1);
  return (
    <div className="overflow-y-auto custom-scrollbar space-y-[3px]" style={{ height }}>
      {rows.map((r) => (
        <div key={r.name} className="flex items-center gap-2">
          <span className="w-20 shrink-0 truncate text-[10px] text-slate-500" title={r.name}>{r.name}</span>
          <div className="flex-1 h-[9px] rounded-full bg-slate-100 overflow-hidden">
            <div
              className="h-full rounded-full"
              style={{ width: `${(r.count / cap) * 100}%`, backgroundColor: color, opacity: 0.85 }}
            />
          </div>
          <span className="w-14 shrink-0 text-right text-[10px] font-mono text-slate-500">
            {r.count} · {((r.count / total) * 100).toFixed(0)}%
          </span>
        </div>
      ))}
      {rows.length === 0 && <div className="text-[10px] text-slate-300 py-4 text-center">—</div>}
    </div>
  );
};

/** 标签徽章（环境标签 + 时效标签，判据见排行榜说明） */
const TAG_STYLES: Record<string, string> = {
  牛市进攻型: 'bg-rose-50 text-rose-600 border-rose-100',
  熊市防御型: 'bg-emerald-50 text-emerald-600 border-emerald-100',
  震荡占优型: 'bg-amber-50 text-amber-600 border-amber-100',
  全天候型: 'bg-slate-50 text-slate-500 border-slate-200',
  长期稳定型: 'bg-blue-50 text-blue-600 border-blue-100',
  近期转强: 'bg-violet-50 text-violet-600 border-violet-100',
  近期失效: 'bg-emerald-50 text-emerald-500 border-emerald-100',
  持续低效: 'bg-slate-100 text-slate-400 border-slate-200',
};

/** 环境标签（因子报告口径，离线：沪深300 滚动 3 月 ±5%）。
 * 与平台 regime 时间线（20 日量价三态，qm_regime_daily / market_regime）是**两套口径**，
 * 设计 §6.1 词表收编：不并入、不可互比——悬停提示写明。 */
export const ENV_TAGS = ['牛市进攻型', '熊市防御型', '震荡占优型', '全天候型'];
const ENV_TAG_HINT =
  '因子报告口径（离线：沪深300 滚动 3 月累计 >+5% 牛 / <−5% 熊 / 其余震荡）——' +
  '非平台 regime 时间线（20 日量价三态），两套口径不并入、不可互比';

export const TagChip: React.FC<{ tag: string; small?: boolean }> = ({ tag, small }) => {
  if (!tag) return null;
  return (
    <span
      title={ENV_TAGS.includes(tag) ? ENV_TAG_HINT : undefined}
      className={`inline-flex items-center rounded-full border font-bold whitespace-nowrap ${
        small ? 'px-1.5 py-0 text-[9px]' : 'px-2 py-[1px] text-[10px]'
      } ${TAG_STYLES[tag] || 'bg-slate-50 text-slate-500 border-slate-200'}`}
    >
      {tag}
    </span>
  );
};

export const ALL_TAGS = [
  ...ENV_TAGS,
  '长期稳定型', '近期转强', '近期失效', '持续低效',
];

/** 卡片壳 */
export const Card: React.FC<{ title: string; extra?: React.ReactNode; children: React.ReactNode; className?: string }> = ({
  title,
  extra,
  children,
  className,
}) => (
  <div className={`bg-white rounded-2xl border border-slate-200/80 shadow-sm p-3 flex flex-col min-h-0 ${className || ''}`}>
    <div className="flex items-baseline justify-between mb-1.5">
      <h4 className="text-xs font-extrabold text-slate-800">{title}</h4>
      {extra}
    </div>
    <div className="flex-1 min-h-0">{children}</div>
  </div>
);

// ---------------------------------------------------------------------------
// 时间区间选择器（全部/近3年/近1年/年/自定义）
// ---------------------------------------------------------------------------
export interface RangeValue {
  start: string | null;
  end: string | null;
  preset: string;
}

const shiftMonth = (ym: string, delta: number): string => {
  const [y, m] = ym.split('-').map(Number);
  const total = y * 12 + (m - 1) + delta;
  return `${Math.floor(total / 12)}-${String((total % 12) + 1).padStart(2, '0')}`;
};

/** 依据快照窗口生成预置区间 */
export function rangePresets(windowStr?: string[]): Array<{ key: string; label: string; start: string | null; end: string | null }> {
  const out: Array<{ key: string; label: string; start: string | null; end: string | null }> = [
    { key: 'all', label: '全部', start: null, end: null },
  ];
  if (windowStr?.[1]) {
    const endYm = windowStr[1].slice(0, 7);
    out.push({ key: '3y', label: '近3年', start: `${shiftMonth(endYm, -35)}-01`, end: null });
    out.push({ key: '1y', label: '近1年', start: `${shiftMonth(endYm, -11)}-01`, end: null });
    const endYear = Number(endYm.slice(0, 4));
    for (let y = endYear; y >= Math.max(endYear - 4, 2015); y -= 1) {
      out.push({ key: `y${y}`, label: String(y), start: `${y}-01`, end: `${y}-12` });
    }
  }
  out.push({ key: 'custom', label: '自定义', start: null, end: null });
  return out;
}

/** 自定义区间的年月下拉（从快照窗口内列出 YYYY-MM） */
export function monthOptions(windowStr?: string[]): string[] {
  if (!windowStr?.[0] || !windowStr?.[1]) return [];
  const out: string[] = [];
  let cur = windowStr[0].slice(0, 7);
  const end = windowStr[1].slice(0, 7);
  while (cur <= end && out.length < 200) {
    out.push(cur);
    cur = shiftMonth(cur, 1);
  }
  return out.reverse();
}

export const RangePicker: React.FC<{
  windowStr?: string[];
  value: RangeValue;
  onChange: (v: RangeValue) => void;
}> = ({ windowStr, value, onChange }) => {
  const presets = rangePresets(windowStr);
  const months = monthOptions(windowStr);
  const active = value.preset;
  return (
    <div className="flex items-center gap-1.5 flex-wrap">
      <span className="text-[10px] font-bold text-slate-400">区间</span>
      <div className="flex items-center gap-1 rounded-full bg-slate-100 border border-slate-200 p-0.5">
        {presets.map((p) => (
          <button
            key={p.key}
            onClick={() => {
              if (p.key === 'custom') {
                onChange({
                  preset: 'custom',
                  start: value.start || months[months.length - 1] || null,
                  end: value.end || months[0] || null,
                });
              } else {
                onChange({ preset: p.key, start: p.start, end: p.end });
              }
            }}
            className={`rounded-full px-2.5 py-0.5 text-[10px] font-bold transition-colors ${
              active === p.key ? 'bg-white text-slate-800 shadow-sm' : 'text-slate-500 hover:text-slate-700'
            }`}
          >
            {p.label}
          </button>
        ))}
      </div>
      {active === 'custom' && (
        <span className="flex items-center gap-1">
          <select
            value={(value.start || '').slice(0, 7)}
            onChange={(e) => onChange({ ...value, start: e.target.value ? `${e.target.value}-01` : null })}
            className="rounded-lg border border-slate-200 bg-white px-1.5 py-0.5 text-[10px] font-mono"
          >
            {months.slice().reverse().map((m) => (
              <option key={m} value={m}>{m}</option>
            ))}
          </select>
          <span className="text-[10px] text-slate-400">~</span>
          <select
            value={(value.end || '').slice(0, 7)}
            onChange={(e) => onChange({ ...value, end: e.target.value ? `${e.target.value}-12` : null })}
            className="rounded-lg border border-slate-200 bg-white px-1.5 py-0.5 text-[10px] font-mono"
          >
            {months.map((m) => (
              <option key={m} value={m}>{m}</option>
            ))}
          </select>
        </span>
      )}
    </div>
  );
};
