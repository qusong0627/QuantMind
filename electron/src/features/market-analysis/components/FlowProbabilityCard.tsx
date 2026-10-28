/**
 * 资金流概率模型卡：未来 h 日跑赢排序分（订单流结构 + 量价速度 + 板块状态）。
 *
 * 展示纪律：
 * - 模型分是**排序分**（isotonic 校准后仍不是真实胜率）；真正的概率看 OOS
 *   十分位胜率（top_hit vs base_hit），不用「85 分＝85%」误导；
 * - 板块没有独立模型（实测无稳定 alpha），板块分＝成分股概率成交额加权，只作构成/风险聚合；
 * - 当前流出但概率高＝「逆流候选」（吸筹），当前流入但概率低＝「流入但弱」（派发警惕）；
 * - 产物只覆盖 trade_date；历史回看不拿最新概率冒充当时。
 */
import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { AlertCircle, ChevronDown, ChevronUp, Gauge } from 'lucide-react';
import {
  getFlowProbability,
  type FlowProbabilityIntentStock,
  type FlowProbabilityResponse,
  type FlowProbabilityStock,
} from '../services/moneyFlowService';

const HORIZONS = [3, 5, 10, 20] as const;
type Horizon = (typeof HORIZONS)[number];

const scoreText = (v: number | null | undefined): string =>
  v === null || v === undefined || Number.isNaN(v) ? '—' : (v * 100).toFixed(1);
const pct = (v: number | null | undefined, digits = 1): string =>
  v === null || v === undefined || Number.isNaN(v) ? '—' : `${(v * 100).toFixed(digits)}%`;
/** 已经是百分数的值（OOS 均值、5 日涨跌） */
const pointPct = (v: number | null | undefined, digits = 2): string =>
  v === null || v === undefined || Number.isNaN(v) ? '—' : `${v > 0 ? '+' : ''}${v.toFixed(digits)}%`;
const money = (v: number | null | undefined): string =>
  v === null || v === undefined ? '—' : v >= 0 ? '流入' : '流出';

const StockRow: React.FC<{
  row: FlowProbabilityStock;
  h: Horizon;
  intent?: FlowProbabilityIntentStock;
  onSelect?: (s: string, n: string) => void;
}> = ({ row, h, intent, onSelect }) => {
  const p =
    (row[`raw${h}` as keyof FlowProbabilityStock] as number | null | undefined) ??
    (row[`p${h}` as keyof FlowProbabilityStock] as number | null | undefined);
  const inflow = (row.net_ratio ?? 0) >= 0;
  return (
    <button
      onClick={() => onSelect?.(row.symbol, row.name)}
      className="flex items-center gap-1.5 py-0.5 text-left hover:bg-slate-50 rounded px-1 -mx-1 transition-colors w-full"
      title={`${row.industry ?? '—'} · 5日 ${pointPct(row.r5)} · z20 ${row.z20 ?? '—'} · 净额比 ${pointPct((row.net_ratio ?? 0) * 100)}`}
    >
      <span className="text-[10px] font-mono text-slate-400 w-10 shrink-0">{row.symbol}</span>
      <span className="text-[10px] font-bold text-slate-700 truncate">{row.name || '—'}</span>
      {intent?.news_score != null && Math.abs(intent.news_score) >= 0.15 && (
        <span
          className={`text-[8px] font-bold shrink-0 ${intent.news_score > 0 ? 'text-purple-600' : 'text-slate-500'}`}
          title={`消息情绪 ${intent.news_score > 0 ? '偏多' : '偏空'} · ${intent.news_n ?? 0} 条`}
        >
          消息{intent.news_score > 0 ? '+' : '−'}
        </span>
      )}
      {intent?.lhb_net != null && (
        <span
          className={`text-[8px] font-bold shrink-0 ${intent.lhb_net >= 0 ? 'text-red-500' : 'text-emerald-600'}`}
          title={`龙虎榜净买 ${(intent.lhb_net / 1e8).toFixed(2)} 亿`}
        >
          龙虎
        </span>
      )}
      {intent?.block_premium != null && (
        <span
          className={`text-[8px] font-bold shrink-0 ${intent.block_premium < 0 ? 'text-emerald-600' : 'text-red-500'}`}
          title={`大宗${intent.block_premium < 0 ? '折价' : '溢价'} ${(intent.block_premium * 100).toFixed(1)}%`}
        >
          大宗{intent.block_premium < 0 ? '折' : '溢'}
        </span>
      )}
      <span className="flex-1" />
      <span className={`text-[9px] font-bold shrink-0 ${inflow ? 'text-red-500' : 'text-emerald-600'}`}>
        {money(row.net_ratio)}
      </span>
      <span className={`text-[10px] font-mono font-extrabold w-8 text-right ${(p ?? 0) >= 0.5 ? 'text-red-600' : 'text-emerald-600'}`}>
        {scoreText(p)}
      </span>
    </button>
  );
};

export const FlowProbabilityCard: React.FC<{
  date?: string;
  onSelectStock?: (symbol: string, name: string) => void;
}> = ({ date, onSelectStock }) => {
  const [open, setOpen] = useState(true);
  const [h, setH] = useState<Horizon>(5);
  const [data, setData] = useState<FlowProbabilityResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setData(await getFlowProbability(date));
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : '概率模型加载失败');
    } finally {
      setLoading(false);
    }
  }, [date]);

  useEffect(() => {
    void load();
  }, [load]);

  const art = data?.available ? data.artifact : undefined;
  const metrics = art?.metrics?.[String(h)];
  const thresholds = art?.thresholds?.[String(h)];
  const stocks = art?.latest?.stocks;
  const intentMap = data?.intent?.stocks ?? {};
  const top = (stocks?.[`top_${h}` as keyof typeof stocks] as FlowProbabilityStock[] | undefined) ?? [];
  const bottom = (stocks?.[`bottom_${h}` as keyof typeof stocks] as FlowProbabilityStock[] | undefined) ?? [];
  const outflowStrong =
    (stocks?.[`outflow_strong_${h}` as keyof typeof stocks] as FlowProbabilityStock[] | undefined) ?? [];
  const inflowWeak =
    (stocks?.[`inflow_weak_${h}` as keyof typeof stocks] as FlowProbabilityStock[] | undefined) ?? [];
  const sectors = useMemo(() => {
    const list = art?.latest?.sectors ?? [];
    return [...list].sort((a, b) => (b[`p${h}` as keyof typeof b] as number ?? 0) - (a[`p${h}` as keyof typeof a] as number ?? 0));
  }, [art, h]);

  if (!loading && !error && data && !data.available) {
    return (
      <div className="bg-white/95 rounded-2xl border border-slate-200 px-3.5 py-2.5 text-[11px] text-slate-500 flex items-center gap-2">
        <AlertCircle className="w-3.5 h-3.5 text-amber-500" />
        概率模型产物未生成{data.hint ? <>：<code className="font-mono text-[10px]">{data.hint}</code></> : null}
      </div>
    );
  }
  if (!art) return null;

  return (
    <div className="bg-white/95 backdrop-blur-md rounded-2xl border border-purple-100/80 shadow-xs">
      <button onClick={() => setOpen((v) => !v)} className="w-full flex items-center justify-between gap-2 px-3.5 py-2.5 text-left">
        <div className="flex items-center gap-2 flex-wrap">
          <Gauge className="w-3.5 h-3.5 text-red-500" />
          <span className="text-xs font-extrabold text-slate-800">未来强度概率（模型）</span>
          <span className="text-[10px] text-slate-400">订单流结构 × 量价速度 × 板块状态 · 排序分</span>
          <span className="text-[10px] font-mono text-purple-700 bg-purple-50 border border-purple-200/70 rounded-full px-2 py-0.5">
            {data.trade_date}
          </span>
          {metrics?.auc !== undefined && (
            <span className="text-[9px] font-mono text-slate-500">OOS AUC {metrics.auc.toFixed(3)}</span>
          )}
        </div>
        {open ? <ChevronUp className="w-3.5 h-3.5 text-slate-400" /> : <ChevronDown className="w-3.5 h-3.5 text-slate-400" />}
      </button>
      {open && (
        <div className="px-3.5 pb-3.5 flex flex-col gap-2.5">
          <div className="flex items-center justify-between gap-2 flex-wrap">
            <div className="flex items-center gap-1">
              <span className="text-[10px] font-bold text-slate-400 mr-1">未来</span>
              {HORIZONS.map((x) => (
                <button
                  key={x}
                  onClick={() => setH(x)}
                  className={`px-2.5 py-0.5 rounded-full text-[10px] font-extrabold transition-colors ${
                    x === h ? 'bg-purple-600 text-white' : 'bg-slate-100 text-slate-600 hover:bg-slate-200'
                  }`}
                >
                  T+{x}
                </button>
              ))}
            </div>
            {metrics && metrics.n_days ? (
              <span className="text-[9px] text-slate-500 font-mono">
                OOS：top 十分位均值 {pointPct(metrics.top_mean)} · 胜率 {pct(metrics.top_hit)}
                （基准 {pct(metrics.base_hit)}）· bottom {pointPct(metrics.bot_mean)}
              </span>
            ) : (
              <span className="text-[9px] text-slate-400">该窗口无 2025+ 留出样本，仅有排序分</span>
            )}
          </div>

          {!data.as_of_latest && (
            <div className="rounded-lg border border-amber-200/70 bg-amber-50 px-2 py-1 text-[9px] text-amber-700">
              模型产物只覆盖 {data.trade_date}；当前是历史回看，不拿最新概率冒充当时。
            </div>
          )}

          <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
            <div className="rounded-xl border border-red-100 bg-red-50/30 px-2.5 py-1.5">
              <div className="flex items-center justify-between">
                <span className="text-[10px] font-extrabold text-red-700">强势概率 Top</span>
                <span className="text-[9px] text-slate-400">分数越高＝模型越看好</span>
              </div>
              {top.slice(0, 6).map((r) => <StockRow key={r.symbol} row={r} h={h} intent={intentMap[r.symbol]} onSelect={onSelectStock} />)}
              {!top.length && <div className="text-[10px] text-slate-300">—</div>}
            </div>
            <div className="rounded-xl border border-emerald-100 bg-emerald-50/30 px-2.5 py-1.5">
              <div className="flex items-center justify-between">
                <span className="text-[10px] font-extrabold text-emerald-700">弱势概率 Top</span>
                <span className="text-[9px] text-slate-400">分数越低＝模型越回避</span>
              </div>
              {bottom.slice(0, 6).map((r) => <StockRow key={r.symbol} row={r} h={h} intent={intentMap[r.symbol]} onSelect={onSelectStock} />)}
              {!bottom.length && <div className="text-[10px] text-slate-300">—</div>}
            </div>
          </div>

          <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
            <div className="rounded-xl border border-purple-100 bg-purple-50/30 px-2.5 py-1.5">
              <div className="flex items-center justify-between">
                <span className="text-[10px] font-extrabold text-purple-700">逆流候选：当前流出 · 概率高</span>
                <span className="text-[9px] text-slate-400">吸筹/清洗迹象</span>
              </div>
              {outflowStrong.slice(0, 5).map((r) => <StockRow key={r.symbol} row={r} h={h} intent={intentMap[r.symbol]} onSelect={onSelectStock} />)}
              {!outflowStrong.length && <div className="text-[10px] text-slate-300">今日无</div>}
            </div>
            <div className="rounded-xl border border-amber-100 bg-amber-50/30 px-2.5 py-1.5">
              <div className="flex items-center justify-between">
                <span className="text-[10px] font-extrabold text-amber-700">流入但弱：当前流入 · 概率低</span>
                <span className="text-[9px] text-slate-400">派发/追高警惕</span>
              </div>
              {inflowWeak.slice(0, 5).map((r) => <StockRow key={r.symbol} row={r} h={h} intent={intentMap[r.symbol]} onSelect={onSelectStock} />)}
              {!inflowWeak.length && <div className="text-[10px] text-slate-300">今日无</div>}
            </div>
          </div>

          {sectors.length > 0 && (
            <div className="flex flex-col gap-1 rounded-xl border border-slate-100 bg-slate-50/40 px-2.5 py-1.5">
              <div className="flex items-center justify-between">
                <span className="text-[10px] font-extrabold text-slate-600">板块聚合（成分股概率成交额加权）</span>
                <span className="text-[9px] text-slate-400">不是独立板块模型；仅作构成/风险聚合</span>
              </div>
              <div className="flex flex-wrap gap-1">
                {sectors.slice(0, 5).map((s) => (
                  <span key={s.name} className="px-1.5 py-0.5 rounded bg-red-50 text-red-600 text-[9px] font-bold" title={`${s.n} 只 · 净额比 ${pointPct((s.net_ratio ?? 0) * 100)}`}>
                    {s.name} {scoreText(s[`p${h}` as keyof typeof s] as number | null)}
                  </span>
                ))}
                {sectors.slice(-5).reverse().map((s) => (
                  <span key={s.name} className="px-1.5 py-0.5 rounded bg-emerald-50 text-emerald-600 text-[9px] font-bold" title={`${s.n} 只 · 净额比 ${pointPct((s.net_ratio ?? 0) * 100)}`}>
                    {s.name} {scoreText(s[`p${h}` as keyof typeof s] as number | null)}
                  </span>
                ))}
              </div>
            </div>
          )}

          <p className="text-[9px] text-slate-400 leading-snug">
            模型分＝排序分（isotonic 校准后仍不等于真实胜率）；单票超额左偏，主看 OOS 十分位与基准差。
            当前流出但概率高＝逆流候选（可能是吸筹/清洗），当前流入但概率低＝派发警惕。
            板块层资金流实测无稳定 alpha，故板块只做成分聚合，不做独立预测。
          </p>
          {thresholds && (
            <p className="text-[8px] text-slate-300 font-mono">
              着色阈值：hi={scoreText(thresholds.hi)} / lo={scoreText(thresholds.lo)}
              {loading ? ' · 刷新中' : ''}{error ? ` · ${error}` : ''}
            </p>
          )}
        </div>
      )}
    </div>
  );
};
