/**
 * 资金流预测力（研究证据卡）：净流入金额对照 + 资金流质量分分位 + 5×5 热力图 +
 * 高/低胜率形态 + 当日靠前/靠后个股。
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
} from '../services/moneyFlowService';

const HORIZONS = [3, 5, 10, 20] as const;
type Horizon = (typeof HORIZONS)[number];

const fmtPct = (v: number | null | undefined, digits = 2): string =>
  v === null || v === undefined || Number.isNaN(v) ? '—' : `${v > 0 ? '+' : ''}${v.toFixed(digits)}%`;

const fmtWin = (v: number | null | undefined): string =>
  v === null || v === undefined || Number.isNaN(v) ? '—' : `${Math.round(v * 100)}%`;

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

const ScoreRowList: React.FC<{
  title: string;
  rows: FlowScoreRow[];
  tone: 'top' | 'bottom';
  onSelectStock?: (symbol: string, name: string) => void;
}> = ({ title, rows, tone, onSelectStock }) => (
  <div className="flex flex-col gap-1 min-w-0">
    <span className={`text-[10px] font-extrabold ${tone === 'top' ? 'text-red-600' : 'text-emerald-600'}`}>
      {title}
    </span>
    <div className="flex flex-col divide-y divide-slate-50">
      {rows.slice(0, 6).map((r) => (
        <button
          key={r.symbol}
          onClick={() => onSelectStock?.(r.symbol, r.name)}
          className="flex items-center gap-1.5 py-0.5 text-left hover:bg-slate-50 rounded px-1 -mx-1 transition-colors"
          title={`${r.industry ?? '—'} · 净额比 ${fmtPct((r.net_ratio ?? 0) * 100)} · 5日 ${fmtPct(r.r5)} · z20 ${r.z20 ?? '—'}`}
        >
          <span className="text-[10px] font-mono text-slate-400 w-10 shrink-0">{r.symbol}</span>
          <span className="text-[10px] font-bold text-slate-700 truncate flex-1">{r.name || '—'}</span>
          <span className={`text-[10px] font-mono font-extrabold ${r.score >= 50 ? 'text-red-600' : 'text-emerald-600'}`}>
            {r.score.toFixed(1)}
          </span>
        </button>
      ))}
      {!rows.length && <span className="text-[10px] text-slate-300">当日无命中</span>}
    </div>
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
    { key: '强一致+未大涨（一致性前20% & 5日涨幅≤2%）', tone: 'long', match: 'consistency_not_extended' },
    { key: '高净流入+大涨（z20≥1.5 & 5日涨幅≥5%）', tone: 'avoid', match: 'chase_inflow' },
    { key: '净流出+大涨（z20≤-1.5 & 5日涨幅≥5%）', tone: 'tactical', match: 'outflow_strong' },
    { key: '卖压前20%', tone: 'avoid', match: 'sell_pressure_high' },
    { key: '撤单深度前20%', tone: 'avoid', match: 'cancel_depth_high' },
    { key: '资金流质量分前20%', tone: 'long', match: 'score_top20' },
    { key: '资金流质量分后20%', tone: 'avoid', match: 'score_bottom20' },
  ];

  return (
    <div className="bg-white/95 backdrop-blur-md rounded-2xl border border-purple-100/80 shadow-xs">
      <button
        onClick={() => setOpen((v) => !v)}
        className="w-full flex items-center justify-between gap-2 px-3.5 py-2.5 text-left"
      >
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
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
              <div className="rounded-xl border border-slate-100 bg-white px-3 py-2 flex flex-col gap-1.5">
                <span className="text-[10px] font-extrabold text-slate-600">当日质量分（{cur.coverage?.scored ?? 0} 只可评分）</span>
                <div className="grid grid-cols-2 gap-3">
                  <ScoreRowList title="靠前" rows={cur.top ?? []} tone="top" onSelectStock={onSelectStock} />
                  <ScoreRowList title="靠后" rows={cur.bottom ?? []} tone="bottom" onSelectStock={onSelectStock} />
                </div>
                {(cur.sectors_top?.length || cur.sectors_bottom?.length) && (
                  <div className="flex flex-col gap-0.5 pt-1 border-t border-slate-100">
                    <span className="text-[9px] text-slate-400">板块质量分（成分加权）</span>
                    <div className="flex flex-wrap gap-1">
                      {(cur.sectors_top ?? []).slice(0, 4).map((s) => (
                        <span key={s.name} className="px-1.5 py-0.5 rounded bg-red-50 text-red-600 text-[9px] font-bold">
                          {s.name} {s.score?.toFixed(0)}
                        </span>
                      ))}
                      {(cur.sectors_bottom ?? []).slice(-4).map((s) => (
                        <span key={s.name} className="px-1.5 py-0.5 rounded bg-emerald-50 text-emerald-600 text-[9px] font-bold">
                          {s.name} {s.score?.toFixed(0)}
                        </span>
                      ))}
                    </div>
                  </div>
                )}
              </div>

              <div className="rounded-xl border border-slate-100 bg-white px-3 py-2 flex flex-col gap-1">
                <span className="text-[10px] font-extrabold text-slate-600">形态 · 历史日期胜率（T+{horizon}）</span>
                <div className="flex flex-col divide-y divide-slate-50">
                  {patternOrder.map((p) => {
                    const stat = patterns[`${p.key}|h${horizon}`];
                    if (!stat) return null;
                    const hit = cur.patterns?.[p.match];
                    const matchCount = hit?.count ?? 0;
                    const tone =
                      p.tone === 'long'
                        ? 'text-red-600'
                        : p.tone === 'tactical'
                          ? 'text-amber-600'
                          : 'text-emerald-600';
                    return (
                      <div key={p.key} className="flex items-center gap-1.5 py-1">
                        <span className={`text-[10px] font-bold flex-1 truncate ${tone}`} title={p.key}>
                          {p.key}
                        </span>
                        <span className="text-[10px] font-mono text-slate-600 w-9 text-right">
                          {fmtWin(stat.date_win)}
                        </span>
                        <span className={`text-[10px] font-mono w-12 text-right ${stat.date_mean >= 0 ? 'text-red-600' : 'text-emerald-600'}`}>
                          {fmtPct(stat.date_mean)}
                        </span>
                        <span className="text-[9px] font-mono text-slate-400 w-10 text-right">n={stat.n}</span>
                        <span className="text-[9px] font-mono text-purple-600 w-8 text-right">今{matchCount}</span>
                      </div>
                    );
                  })}
                </div>
                <p className="text-[9px] text-slate-400">日期胜率＝信号日触发篮子跑赢过滤后宇宙的比例，50%＝无方向；n 为股票-日样本。</p>
              </div>
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
