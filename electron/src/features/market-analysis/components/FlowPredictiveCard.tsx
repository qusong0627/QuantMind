/**
 * 资金流预测力（v2 图表版）：净流入金额对照 + 资金流质量分分位 + 5×5 热力图 +
 * 高/低胜率形态 + 当日质量分分布/分项指纹/板块质量。
 *
 * 纪律：
 * - 统计数字全部来自离线产物（build_flow_predictive.py）；缺失显式提示，不回落 0；
 * - 单票胜率受左偏分布影响，主指标是**日期级篮子胜率**（与 50% 基准比）；
 * - 「净流入金额」只作对照：证明它本身没有方向性，避免把金额当信号；
 * - 所有收益均为 t+1 收盘建仓的可执行口径，历史 ≠ 未来。
 */
import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { AlertCircle, ChevronDown, ChevronUp, Sparkles } from 'lucide-react';
import {
  getFlowPredictive,
  type FlowPredictiveArtifact,
  type FlowPredictiveResponse,
  type FlowPredictiveStatRow,
  type FlowScoreRow,
  type FlowSectorScore,
} from '../services/moneyFlowService';

const HORIZONS = [3, 5, 10, 20] as const;
type Horizon = (typeof HORIZONS)[number];

const fmtPct = (v: number | null | undefined, digits = 2): string =>
  v === null || v === undefined || Number.isNaN(v) ? '—' : `${v > 0 ? '+' : ''}${v.toFixed(digits)}%`;
const fmtWin = (v: number | null | undefined): string =>
  v === null || v === undefined || Number.isNaN(v) ? '—' : `${Math.round(v * 100)}%`;
const clamp = (v: number) => Math.max(0, Math.min(100, v));

function table(artifact: FlowPredictiveArtifact | null, key: string): FlowPredictiveStatRow[] {
  return artifact?.tables?.[key] ?? [];
}

/** 5 档柱：均值（相对宇宙，%）+ 日期级胜率；红涨绿跌。 */
const DecileBars: React.FC<{ rows: FlowPredictiveStatRow[]; tone: 'net' | 'quality' }> = ({
  rows,
  tone,
}) => {
  if (!rows.length) return <div className="text-[10px] text-slate-300">—</div>;
  const maxAbs = Math.max(...rows.map((r) => Math.abs(r.mean ?? 0)), 0.01);
  return (
    <div className="flex items-end gap-1 h-[92px]">
      {rows.map((r) => {
        const v = r.mean ?? 0;
        const hPct = Math.max(6, (Math.abs(v) / maxAbs) * 52);
        return (
          <div key={r.q} className="flex-1 flex flex-col items-center justify-end gap-0.5 min-w-0">
            <span className={`text-[9px] font-mono font-extrabold ${v >= 0 ? 'text-red-600' : 'text-emerald-600'}`}>
              {fmtPct(v, 2)}
            </span>
            <div
              className={`w-full rounded-t ${v >= 0 ? (tone === 'quality' ? 'bg-red-500' : 'bg-red-300') : tone === 'quality' ? 'bg-emerald-500' : 'bg-emerald-300'}`}
              style={{ height: `${hPct}px` }}
              title={`Q${r.q}：平均 ${fmtPct(v)} · 日期胜率 ${fmtWin(r.date_win)} · n=${r.n}`}
            />
            <span className="text-[8px] text-slate-400 font-mono">Q{r.q}</span>
            <span className="text-[8px] text-slate-500 font-mono">{fmtWin(r.date_win)}</span>
          </div>
        );
      })}
    </div>
  );
};

/** 5×5 热力图：行=净流入 z20 分位（低→高），列=过去 5 日涨跌分位（低→高）。 */
const Heatmap: React.FC<{ art: FlowPredictiveArtifact | null; h: Horizon }> = ({ art, h }) => {
  const grid = art?.[`heatmap_exec_h${h}`];
  if (!grid) return <div className="text-[10px] text-slate-300">—</div>;
  const flat = grid.mean.flat().filter((v): v is number => v !== null && Number.isFinite(v));
  const maxAbs = Math.max(...flat.map((v) => Math.abs(v)), 0.01);
  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-center gap-2">
        <span className="text-[9px] text-slate-400 w-16 shrink-0">净流入 z20</span>
        <div className="grid grid-cols-5 gap-0.5 flex-1">
          {grid.mean.map((row, zi) =>
            row.map((v, ri) => {
              const alpha = v === null ? 0.04 : Math.min(0.85, 0.12 + (Math.abs(v) / maxAbs) * 0.73);
              const bg =
                v === null
                  ? 'rgba(148,163,184,0.08)'
                  : v >= 0
                    ? `rgba(220,38,38,${alpha})`
                    : `rgba(5,150,105,${alpha})`;
              return (
                <div
                  key={`${zi}-${ri}`}
                  className="h-7 rounded-sm flex items-center justify-center text-[8px] font-mono text-slate-700"
                  style={{ background: bg }}
                  title={`净流入 z20 Q${zi + 1} × 5日涨跌 Q${ri + 1}：之后 ${h} 日平均 ${fmtPct(v)} · 胜率 ${fmtWin(grid.win[zi]?.[ri])} · n=${grid.n[zi]?.[ri] ?? 0}`}
                >
                  {v === null ? '—' : v.toFixed(1)}
                </div>
              );
            }),
          )}
        </div>
      </div>
      <div className="flex items-center gap-2">
        <span className="text-[9px] text-slate-400 w-16 shrink-0" />
        <div className="grid grid-cols-5 gap-0.5 flex-1 text-[8px] text-slate-400 text-center font-mono">
          {['跌最多', '跌', '平', '涨', '涨最多'].map((t) => (
            <span key={t}>{t}</span>
          ))}
        </div>
      </div>
      <p className="text-[9px] text-slate-400 leading-snug">
        颜色越红＝之后平均越强，越绿＝越弱；右上角（高净流入 + 已大涨）通常最绿 ——
        <span className="font-bold text-slate-600">资金流大不等于会涨</span>。
      </p>
    </div>
  );
};

const StatTile: React.FC<{ label: string; value: string; hint: string; tone: 'good' | 'bad' | 'neutral' }> = ({
  label,
  value,
  hint,
  tone,
}) => (
  <div
    className={`rounded-xl border px-2.5 py-1.5 flex flex-col gap-0.5 ${
      tone === 'good'
        ? 'border-red-100 bg-red-50/40'
        : tone === 'bad'
          ? 'border-emerald-100 bg-emerald-50/40'
          : 'border-slate-100 bg-slate-50/50'
    }`}
  >
    <span className="text-[9px] text-slate-500">{label}</span>
    <span
      className={`text-[15px] font-extrabold font-mono leading-none ${
        tone === 'good' ? 'text-red-600' : tone === 'bad' ? 'text-emerald-600' : 'text-slate-700'
      }`}
    >
      {value}
    </span>
    <span className="text-[8px] text-slate-400 leading-snug">{hint}</span>
  </div>
);

/** 当日质量分分布：10 分一档直方图 + 中位/分位；红=偏强、绿=偏弱。 */
const ScoreHistogram: React.FC<{
  dist: NonNullable<NonNullable<FlowPredictiveResponse['current']>['distribution']>;
}> = ({ dist }) => {
  const max = Math.max(...dist.counts, 1);
  const median = dist.median ?? 50;
  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-end gap-0.5 h-[86px]">
        {dist.counts.map((c, i) => {
          const center = (dist.bins[i] + (dist.bins[i + 1] ?? 100)) / 2;
          const strong = center >= median;
          return (
            <div key={dist.bins[i]} className="flex-1 flex flex-col items-center justify-end gap-0.5 min-w-0" title={`${dist.bins[i]}~${dist.bins[i + 1] ?? 100} 分：${c} 只`}>
              <span className="text-[7px] text-slate-400 font-mono">{c || ''}</span>
              <div
                className={`w-full rounded-t ${strong ? 'bg-red-400/80' : 'bg-emerald-400/80'}`}
                style={{ height: `${Math.max(2, (c / max) * 62)}px` }}
              />
              <span className="text-[7px] text-slate-400 font-mono">{dist.bins[i]}</span>
            </div>
          );
        })}
      </div>
      <div className="flex items-center justify-between text-[8px] text-slate-500 font-mono">
        <span>p10 {dist.p10 ?? '—'}</span>
        <span className="font-bold text-slate-700">中位 {dist.median ?? '—'}</span>
        <span>p90 {dist.p90 ?? '—'}</span>
        <span>n={dist.n}</span>
      </div>
    </div>
  );
};

const LollipopRow: React.FC<{
  row: FlowScoreRow;
  tone: 'top' | 'bottom';
  onSelectStock?: (symbol: string, name: string) => void;
}> = ({ row, tone, onSelectStock }) => (
  <button
    onClick={() => onSelectStock?.(row.symbol, row.name)}
    className="flex items-center gap-1.5 py-0.5 text-left hover:bg-white/80 rounded px-1 -mx-1 transition-colors w-full"
    title={`${row.industry ?? '—'} · 净额比 ${fmtPct((row.net_ratio ?? 0) * 100)} · 5日 ${fmtPct(row.r5)} · z20 ${row.z20 ?? '—'}`}
  >
    <span className="text-[9px] font-mono text-slate-400 w-10 shrink-0">{row.symbol}</span>
    <span className="text-[10px] font-bold text-slate-700 truncate w-20 shrink-0">{row.name || '—'}</span>
    <span className="relative h-1.5 flex-1 rounded-full bg-slate-200/70">
      <span className="absolute top-0 bottom-0 left-1/2 w-px bg-slate-300" />
      <span
        className={`absolute top-1/2 -translate-y-1/2 h-2 w-2 rounded-full border border-white shadow-sm ${tone === 'top' ? 'bg-red-600' : 'bg-emerald-600'}`}
        style={{ left: `calc(${clamp(row.score)}% - 4px)` }}
      />
    </span>
    <span className={`text-[10px] font-mono font-extrabold w-8 text-right ${tone === 'top' ? 'text-red-600' : 'text-emerald-600'}`}>
      {row.score.toFixed(1)}
    </span>
  </button>
);

/** 强势组/弱势组的分项指纹：颜色=该分项对质量分的贡献（正向后 >0.6 红、负向后 >0.6 绿）。 */
const ComponentFingerprint: React.FC<{
  profile: NonNullable<NonNullable<FlowPredictiveResponse['current']>['component_profile']>;
  components: NonNullable<FlowPredictiveResponse['meta']['components']>;
}> = ({ profile, components }) => {
  const rows: Array<{ label: string; data: Record<string, number | null> }> = [
    { label: '强势组', data: profile.top },
    { label: '弱势组', data: profile.bottom },
  ];
  const colorOf = (key: string, v: number | null | undefined): string => {
    if (typeof v !== 'number' || !Number.isFinite(v)) return '#f1f5f9';
    const sign = components.find((c) => c.key === key)?.sign ?? 1;
    const q = sign > 0 ? v : 1 - v;
    if (q >= 0.8) return 'rgba(220,38,38,0.85)';
    if (q >= 0.6) return 'rgba(248,113,113,0.65)';
    if (q > 0.4) return 'rgba(203,213,225,0.55)';
    if (q > 0.2) return 'rgba(52,211,153,0.55)';
    return 'rgba(5,150,105,0.75)';
  };
  return (
    <div className="flex flex-col gap-1">
      {rows.map((r) => (
        <div key={r.label} className="grid grid-cols-[34px_repeat(7,minmax(0,1fr))] gap-0.5 items-center">
          <span className={`text-[9px] font-extrabold ${r.label === '强势组' ? 'text-red-600' : 'text-emerald-600'}`}>{r.label}</span>
          {components.map((c) => (
            <div
              key={`${r.label}-${c.key}`}
              className="h-5 rounded-sm flex items-center justify-center text-[8px] font-mono text-slate-700"
              style={{ background: colorOf(c.key, r.data[c.key]) }}
              title={`${c.label}：${r.data[c.key] == null ? '—' : (r.data[c.key]! * 100).toFixed(0)}（分项方向 ${c.sign > 0 ? '+' : '−'}）`}
            >
              {r.data[c.key] == null ? '—' : (r.data[c.key]! * 100).toFixed(0)}
            </div>
          ))}
        </div>
      ))}
      <div className="grid grid-cols-[34px_repeat(7,minmax(0,1fr))] gap-0.5">
        <span />
        {components.map((c) => (
          <span key={`lbl-${c.key}`} className="text-[7px] text-slate-400 text-center truncate" title={`${c.label}：${c.note}`}>
            {c.label.slice(0, 4)}
          </span>
        ))}
      </div>
      <p className="text-[8px] text-slate-400">颜色=该分项对质量分的贡献（已按分项正负向翻转）；红=更强、绿=更弱。</p>
    </div>
  );
};

const SectorQualityBars: React.FC<{
  rows: FlowSectorScore[];
  tone: 'top' | 'bottom';
}> = ({ rows, tone }) => (
  <div className="flex flex-col gap-0.5">
    {rows.map((r) => (
      <div key={r.name} className="flex items-center gap-1.5 py-0.5" title={`${r.n} 只成分`}>
        <span className="text-[9px] font-bold text-slate-600 w-24 truncate shrink-0">{r.name}</span>
        <span className="relative h-1.5 flex-1 rounded-full bg-slate-200/70">
          <span className="absolute top-0 bottom-0 left-1/2 w-px bg-slate-300" />
          <span
            className={`absolute top-1/2 -translate-y-1/2 h-2 w-2 rounded-full border border-white shadow-sm ${tone === 'top' ? 'bg-red-600' : 'bg-emerald-600'}`}
            style={{ left: `calc(${clamp(r.score ?? 0)}% - 4px)` }}
          />
        </span>
        <span className={`text-[9px] font-mono font-extrabold w-8 text-right ${tone === 'top' ? 'text-red-600' : 'text-emerald-600'}`}>
          {r.score == null ? '—' : r.score.toFixed(1)}
        </span>
      </div>
    ))}
  </div>
);

/** 形态胜率点图：0~100% 轴、50% 基准线；红=长边、绿=应回避、黄=战术。 */
const PatternDotPlot: React.FC<{
  rows: Array<{
    key: string;
    tone: 'long' | 'avoid' | 'tactical';
    stat?: { date_win: number | null; date_mean: number | null; n: number };
    today?: number;
  }>;
}> = ({ rows }) => (
  <div className="flex flex-col gap-0.5">
    <div className="flex items-center gap-1.5 text-[8px] text-slate-400 font-mono px-0.5">
      <span className="w-40 shrink-0" />
      <span className="flex-1 flex justify-between">
        <span>0%</span>
        <span className="text-slate-500">50% 基准</span>
        <span>100%</span>
      </span>
      <span className="w-20 shrink-0 text-right">超额 / n / 今日</span>
    </div>
    {rows.map((p) => {
      const win = p.stat?.date_win;
      const pos = win == null ? null : clamp(win * 100);
      const color = p.tone === 'long' ? 'bg-red-600' : p.tone === 'tactical' ? 'bg-amber-500' : 'bg-emerald-600';
      const text = p.tone === 'long' ? 'text-red-600' : p.tone === 'tactical' ? 'text-amber-600' : 'text-emerald-600';
      return (
        <div key={p.key} className="flex items-center gap-1.5 py-0.5">
          <span className={`text-[9px] font-bold w-40 truncate shrink-0 ${text}`} title={p.key}>
            {p.key}
          </span>
          <span className="relative h-1.5 flex-1 rounded-full bg-slate-200/70">
            <span className="absolute top-0 bottom-0 left-1/2 w-px bg-slate-400/80" />
            {pos !== null && (
              <span
                className={`absolute top-1/2 -translate-y-1/2 h-2 w-2 rounded-full border border-white shadow-sm ${color}`}
                style={{ left: `calc(${pos}% - 4px)` }}
              />
            )}
          </span>
          <span className="w-20 shrink-0 text-right text-[8px] font-mono text-slate-500">
            {fmtWin(win)} · {fmtPct(p.stat?.date_mean ?? null)} · n={p.stat?.n ?? '—'} · 今{p.today ?? 0}
          </span>
        </div>
      );
    })}
  </div>
);

export const FlowPredictiveCard: React.FC<{
  date?: string;
  onSelectStock?: (symbol: string, name: string) => void;
}> = ({ date, onSelectStock }) => {
  const [open, setOpen] = useState(true);
  const [horizon, setHorizon] = useState<Horizon>(5);
  const [data, setData] = useState<FlowPredictiveResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setData(await getFlowPredictive(date));
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : '资金流预测力加载失败');
    } finally {
      setLoading(false);
    }
  }, [date]);

  useEffect(() => {
    void load();
  }, [load]);

  const art = data?.artifact ?? null;
  const cur = data?.current;
  const netRows = useMemo(() => table(art, `net_exec_h${horizon}`), [art, horizon]);
  const qualityRows = useMemo(() => table(art, `composite_exec_h${horizon}`), [art, horizon]);
  const isRows = useMemo(() => table(art, `composite_exec_h${horizon}_is`), [art, horizon]);
  const oosRows = useMemo(() => table(art, `composite_exec_h${horizon}_oos`), [art, horizon]);

  const spread = (rows: FlowPredictiveStatRow[]): number | null => {
    if (rows.length < 2) return null;
    const q1 = rows[0].date_mean ?? null;
    const q5 = rows[rows.length - 1].date_mean ?? null;
    return q1 === null || q5 === null ? null : q5 - q1;
  };
  const netSpread = spread(netRows);
  const qualitySpread = spread(qualityRows);

  const patterns = art?.patterns ?? {};
  const patternOrder = [
    { key: '强一致+未大涨（一致性前20% & 5日涨幅≤2%）', tone: 'long' as const, match: 'consistency_not_extended' },
    { key: '高净流入+大涨（z20≥1.5 & 5日涨幅≥5%）', tone: 'avoid' as const, match: 'chase_inflow' },
    { key: '净流出+大涨（z20≤-1.5 & 5日涨幅≥5%）', tone: 'tactical' as const, match: 'outflow_strong' },
    { key: '卖压前20%', tone: 'avoid' as const, match: 'sell_pressure_high' },
    { key: '撤单深度前20%', tone: 'avoid' as const, match: 'cancel_depth_high' },
    { key: '资金流质量分前20%', tone: 'long' as const, match: 'score_top20' },
    { key: '资金流质量分后20%', tone: 'avoid' as const, match: 'score_bottom20' },
  ];
  const matchCounts = cur?.patterns ?? {};
  const statOf = (p: (typeof patternOrder)[number]) => patterns[`${p.key}|h${horizon}`];
  const strongStat = statOf(patternOrder[0]);
  const chaseStat = statOf(patternOrder[1]);
  const outflowStat = statOf(patternOrder[2]);

  return (
    <div className="bg-white/95 backdrop-blur-md rounded-2xl border border-purple-100/80 shadow-xs">
      <button onClick={() => setOpen((v) => !v)} className="w-full flex items-center justify-between gap-2 px-3.5 py-2.5 text-left">
        <div className="flex items-center gap-2 flex-wrap">
          <Sparkles className="w-3.5 h-3.5 text-purple-600" />
          <span className="text-xs font-extrabold text-slate-800">资金流预测力</span>
          <span className="text-[10px] text-slate-400">研究证据 · 可执行口径（t+1 收盘建仓）</span>
          {cur?.trade_date && (
            <span className="text-[10px] font-mono text-purple-700 bg-purple-50 border border-purple-200/70 rounded-full px-2 py-0.5">
              {cur.trade_date}
            </span>
          )}
          {art?.built_at && (
            <span className="text-[9px] text-slate-400 font-mono">产物 {art.built_at.slice(0, 10)}</span>
          )}
          {qualitySpread !== null && (
            <span className="text-[9px] font-mono text-slate-500">质量分 Q5−Q1 {fmtPct(qualitySpread)}</span>
          )}
        </div>
        {open ? <ChevronUp className="w-3.5 h-3.5 text-slate-400" /> : <ChevronDown className="w-3.5 h-3.5 text-slate-400" />}
      </button>

      {open && (
        <div className="px-3.5 pb-3.5 flex flex-col gap-3">
          <div className="flex items-center justify-between gap-2 flex-wrap">
            <div className="flex items-center gap-1">
              <span className="text-[10px] font-bold text-slate-400 mr-1">之后</span>
              {HORIZONS.map((h) => (
                <button
                  key={h}
                  onClick={() => setHorizon(h)}
                  className={`px-2.5 py-0.5 rounded-full text-[10px] font-extrabold transition-colors ${
                    h === horizon ? 'bg-purple-600 text-white' : 'bg-slate-100 text-slate-600 hover:bg-slate-200'
                  }`}
                >
                  T+{h}
                </button>
              ))}
            </div>
            {loading && <span className="text-[10px] text-slate-400">加载中…</span>}
            {error && <span className="text-[10px] text-rose-500">{error}</span>}
          </div>

          {!art && !loading && (
            <div className="flex items-start gap-1.5 rounded-xl border border-amber-200/70 bg-amber-50 px-3 py-2 text-[11px] text-amber-700">
              <AlertCircle className="w-3.5 h-3.5 shrink-0 mt-px" />
              <span>研究产物尚未生成：运行 <code className="font-mono">python -m backend.scripts.market_analysis.build_flow_predictive</code>。当前只显示当日质量分。</span>
            </div>
          )}

          {art && (
            <div className="grid grid-cols-2 lg:grid-cols-4 gap-2">
              <StatTile
                label="质量分 Q5−Q1"
                value={qualitySpread === null ? '—' : `${qualitySpread > 0 ? '+' : ''}${qualitySpread.toFixed(2)}%`}
                hint={`T+${horizon} 日期级平均超额差`}
                tone="good"
              />
              <StatTile
                label="强一致 + 未大涨"
                value={fmtWin(strongStat?.date_win)}
                hint="日期胜率，50%=无方向"
                tone="good"
              />
              <StatTile
                label="高净流入 + 大涨"
                value={fmtWin(chaseStat?.date_win)}
                hint="日期胜率；<50% 即跑输"
                tone="bad"
              />
              <StatTile
                label="净流出 + 大涨"
                value={fmtWin(outflowStat?.date_win)}
                hint="日期胜率；50%=无方向"
                tone="bad"
              />
            </div>
          )}

          {cur?.available && (
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
              <div className="rounded-xl border border-slate-100 bg-slate-50/50 px-3 py-2">
                <div className="flex items-center justify-between">
                  <span className="text-[10px] font-extrabold text-slate-600">当日质量分分布</span>
                  <span className="text-[9px] text-slate-400">{cur.coverage?.scored ?? 0} 只可评分</span>
                </div>
                {cur.distribution ? <ScoreHistogram dist={cur.distribution} /> : <div className="text-[10px] text-slate-300">—</div>}
                <p className="text-[9px] text-slate-400 mt-0.5">0~100 分；红=偏强、绿=偏弱；中位线用于看整体拥挤方向。</p>
              </div>
              <div className="rounded-xl border border-slate-100 bg-white px-3 py-2 flex flex-col gap-1">
                <div className="flex items-center justify-between">
                  <span className="text-[10px] font-extrabold text-slate-600">当日质量分 · 靠前 / 靠后</span>
                  <span className="text-[9px] text-slate-400">点名字看个股</span>
                </div>
                <div className="grid grid-cols-2 gap-3">
                  <div className="flex flex-col gap-0.5">
                    <span className="text-[9px] font-extrabold text-red-600">靠前</span>
                    {(cur.top ?? []).slice(0, 6).map((r) => (
                      <LollipopRow key={r.symbol} row={r} tone="top" onSelectStock={onSelectStock} />
                    ))}
                  </div>
                  <div className="flex flex-col gap-0.5">
                    <span className="text-[9px] font-extrabold text-emerald-600">靠后</span>
                    {(cur.bottom ?? []).slice(0, 6).map((r) => (
                      <LollipopRow key={r.symbol} row={r} tone="bottom" onSelectStock={onSelectStock} />
                    ))}
                  </div>
                </div>
              </div>
            </div>
          )}

          {cur?.available && cur.component_profile && data?.meta.components?.length ? (
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
              <div className="rounded-xl border border-purple-100 bg-purple-50/25 px-3 py-2">
                <div className="flex items-center justify-between">
                  <span className="text-[10px] font-extrabold text-slate-700">分项指纹 · 强势组 vs 弱势组</span>
                  <span className="text-[9px] text-slate-400">各分项当日截面分位均值</span>
                </div>
                <ComponentFingerprint profile={cur.component_profile} components={data.meta.components} />
              </div>
              <div className="rounded-xl border border-slate-100 bg-white px-3 py-2">
                <div className="flex items-center justify-between">
                  <span className="text-[10px] font-extrabold text-slate-600">板块质量分（成分加权）</span>
                  <span className="text-[9px] text-slate-400">红=靠前 / 绿=靠后</span>
                </div>
                <div className="grid grid-cols-2 gap-3">
                  <SectorQualityBars rows={cur.sectors_top ?? []} tone="top" />
                  <SectorQualityBars rows={cur.sectors_bottom ?? []} tone="bottom" />
                </div>
              </div>
            </div>
          ) : null}

          {art && (
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
              <div className="rounded-xl border border-slate-100 bg-slate-50/50 px-3 py-2">
                <div className="flex items-center justify-between">
                  <span className="text-[10px] font-extrabold text-slate-600">主力净流入分位 → 之后 {horizon} 日</span>
                  <span className="text-[9px] text-slate-400">Q5−Q1 {netSpread === null ? '—' : `${netSpread.toFixed(2)}%`}</span>
                </div>
                <DecileBars rows={netRows} tone="net" />
                <p className="text-[9px] text-slate-400">金额分位几乎平坦；金额本身不能当方向信号。</p>
              </div>
              <div className="rounded-xl border border-purple-100 bg-purple-50/30 px-3 py-2">
                <div className="flex items-center justify-between">
                  <span className="text-[10px] font-extrabold text-slate-700">资金流质量分分位 → 之后 {horizon} 日</span>
                  <span className="text-[9px] text-slate-500">Q5−Q1 {qualitySpread === null ? '—' : `${qualitySpread.toFixed(2)}%`}</span>
                </div>
                <DecileBars rows={qualityRows} tone="quality" />
                <p className="text-[9px] text-slate-400">
                  订单流结构分位；Q5 日期胜率 {fmtWin(qualityRows[qualityRows.length - 1]?.date_win)}（50%＝无方向）。
                  {isRows.length > 0 && oosRows.length > 0 && (
                    <> · IS {fmtWin(isRows[isRows.length - 1]?.date_win)} / OOS {fmtWin(oosRows[oosRows.length - 1]?.date_win)}</>
                  )}
                </p>
                <p className="text-[9px] text-slate-400">
                  剔除规模/动量/波动/换手后，Q5 长边约 +0.06%/5日、+0.30%/20日——更像「避雷分」而非必涨信号。
                </p>
              </div>
            </div>
          )}

          {art && (
            <div className="rounded-xl border border-slate-100 bg-white px-3 py-2">
              <div className="flex items-center justify-between mb-1">
                <span className="text-[10px] font-extrabold text-slate-600">资金流 × 价格位置（之后 {horizon} 日，行业中性超额）</span>
                <span className="text-[9px] text-slate-400">格内数字＝平均超额（%）</span>
              </div>
              <Heatmap art={art} h={horizon} />
            </div>
          )}

          {cur?.available && (
            <div className="rounded-xl border border-slate-100 bg-white px-3 py-2 flex flex-col gap-1">
              <div className="flex items-center justify-between">
                <span className="text-[10px] font-extrabold text-slate-600">形态 · 历史日期胜率（T+{horizon}）</span>
                <span className="text-[9px] text-slate-400">0~100% 轴 · 50%=无方向 · 右侧为平均超额/n/今日命中</span>
              </div>
              <PatternDotPlot
                rows={patternOrder.map((p) => ({
                  key: p.key,
                  tone: p.tone,
                  stat: statOf(p),
                  today: matchCounts[p.match]?.count ?? 0,
                }))}
              />
              <p className="text-[9px] text-slate-400">日期胜率＝信号日触发篮子跑赢过滤后宇宙的比例，50%＝无方向；n 为股票-日样本。</p>
            </div>
          )}

          <p className="text-[9px] text-slate-400 leading-snug">
            {art?.disclosure ?? data?.meta.disclosure}
            {data?.meta.universe && <> · 宇宙：{data.meta.universe}</>}
          </p>
        </div>
      )}
    </div>
  );
};
