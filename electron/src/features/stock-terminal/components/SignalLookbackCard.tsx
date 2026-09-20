/**
 * 信号准确率回看卡（候选信号页 · 两栏下方通栏）。
 *
 * 回答一个问题：**模型 T-3 / T-5 / T-10 给高分（或负分）的票，到今天涨了还是跌了。**
 *
 * 上面汇总（每回看点一行，覆盖全市场打分集）判「模型有没有区分度」；
 * 下面明细（一行一只票）供逐只核对。
 *
 * 口径全部来自后端，本组件**只呈现不重算**。三条纪律：
 * 1. 缺失一律 `—`，绝不显示成 0（0 涨幅是「没涨没跌」这一事实主张）；
 * 2. 表头必须写明现价来自哪里（实时快照 / 最新收盘）与取数日；
 * 3. 一个回看点都算不出价差时，明说「算不出来」，不渲染一张全 `—` 的表
 *    —— 后者会被读成「模型没有区分度」。
 *
 * 交互（2026-09-20 用户反馈后改）：
 * - **默认折叠**。上一版默认展开、折叠入口只是个 12px 灰图标，用户「差点没发现」这个块；
 *   现在折叠条写明它回答什么问题 + 锚点日，右侧是实心「展开」按钮。
 * - 折叠也让上方两栏（候选列表）拿回高度 —— 展开时卡片高约 1150px，会把列表压到只剩几行。
 * - 展开后默认高度 = **20 行逐只明细**；**底边可拖动**改高度，高度记 localStorage。
 *   总高超过视口时整页滚动（由宿主页保证），不把上方两栏压没。
 */
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { AlertTriangle, ChevronDown, ChevronUp, History, RefreshCw } from 'lucide-react';
import { CardHeader } from '../../desk/components/cardKit';
import { PctText } from '../../market-analysis-shared/ui';
import { stockTerminalService as cnTerminalService } from '../services/stockTerminalService';
import type { LookbackDetailItem, LookbackSummaryRow, SignalLookbackData } from '../lookbackModel';
import {
  formatRank,
  formatScore,
  isVerdictUsable,
  lookbackLabel,
  orderPoints,
  priceSourceLabel,
  toPct,
} from '../lookbackModel';

interface SignalLookbackCardProps {
  /** 明细范围（锚点日信号方向）：BUY/SELL/HOLD；缺省=全部 */
  side?: string;
  /** 推理模型 model_id；缺省=各日各自最新 run */
  model?: string;
  /** 锚定信号日；缺省=最近覆盖充分日 */
  asof?: string;
  className?: string;
}

const DEFAULT_LOOKBACKS = [3, 5, 10];

/**
 * 展开后的默认高度（px）按「至少露出 20 行逐只明细」倒推。
 *
 * 实测（1680 宽，浏览器里量的，不是估的）：明细一行 45px；明细表**之前**占掉
 * 298px（汇总表 163 + 「逐只明细」标题行 + 表头 + 内边距），且 `comparable=false`
 * 时还会多出一条约 46px 的「另一套分数尺」警示条。
 *
 * 所以预算 = 20×45 + 298 + 46 ≈ 1244，取整到 1250 —— 带警示条时正好 20 行、
 * 不带时 21 行。**改表结构（行高/列数/警告条/汇总列数）后必须重新在浏览器里量**，
 * 否则「20 行」会悄悄缩水（第一版按估算的 253 算，实测只露 19 行）。
 */
const DETAIL_ROW_PX = 45;
const CARD_CHROME_PX = 350;
const DEFAULT_DETAIL_ROWS = 20;
export const DEFAULT_BODY_PX = DEFAULT_DETAIL_ROWS * DETAIL_ROW_PX + CARD_CHROME_PX;

const MIN_BODY_PX = 240;
const MAX_BODY_PX = 4000;
const HEIGHT_KEY = 'qm:stock-terminal:lookback:bodyH';

const clampBody = (v: number) => Math.min(MAX_BODY_PX, Math.max(MIN_BODY_PX, v));

/** 记住用户拖出来的高度；读不到（首访/隐私模式）就用默认值。 */
function readSavedBody(): number {
  try {
    const raw = typeof window === 'undefined' ? null : window.localStorage.getItem(HEIGHT_KEY);
    const n = raw ? Number(raw) : Number.NaN;
    return Number.isFinite(n) ? clampBody(n) : DEFAULT_BODY_PX;
  } catch {
    return DEFAULT_BODY_PX;
  }
}

function saveBody(px: number): void {
  try {
    if (typeof window !== 'undefined') window.localStorage.setItem(HEIGHT_KEY, String(Math.round(px)));
  } catch {
    /* 记不住不影响使用，不该因为存储不可用而报错 */
  }
}

/**
 * 卡壳。刻意不复用 cardKit 的 `CARD`：它自带 `p-4`，与折叠态要的 `p-0` 同时出现时
 * 谁赢取决于 Tailwind 生成顺序（不是类名书写顺序），折叠条会被撑出一圈莫名留白。
 */
const SHELL =
  'bg-white rounded-2xl border border-slate-200/80 shadow-[0_1px_2px_rgba(15,23,42,0.04)]';

/** 命中率：高/低分档方向相反才叫有效，故用单调色阶而非红绿（避免与涨跌色混语义）。 */
const Hit: React.FC<{ v: number | null }> = ({ v }) => {
  if (v === null || v === undefined || Number.isNaN(v)) {
    return <span className="text-slate-300">—</span>;
  }
  const tone = v >= 0.6 ? 'text-slate-800' : v >= 0.45 ? 'text-slate-500' : 'text-slate-400';
  return <span className={`font-mono font-semibold ${tone}`}>{(v * 100).toFixed(0)}%</span>;
};

const SignalLookbackCard: React.FC<SignalLookbackCardProps> = ({ side, model, asof, className = '' }) => {
  const [data, setData] = useState<SignalLookbackData | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  /** 默认折叠：见文件头「交互」——折叠条本身就是发现入口，折叠也让上方列表拿回高度。 */
  const [collapsed, setCollapsed] = useState(true);
  const [bodyH, setBodyH] = useState<number>(readSavedBody);
  const [dragging, setDragging] = useState(false);
  const rootRef = useRef<HTMLElement | null>(null);
  const dragRef = useRef<{ y: number; h: number } | null>(null);
  const bodyHRef = useRef(bodyH);

  useEffect(() => {
    bodyHRef.current = bodyH;
  }, [bodyH]);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const resp = await cnTerminalService.getSignalLookback({
        lookbacks: DEFAULT_LOOKBACKS.join(','),
        ...(side ? { side } : {}),
        ...(model ? { model } : {}),
        ...(asof ? { asof } : {}),
        page: 1,
        page_size: 50,
      });
      setData(resp);
    } catch (e) {
      // 不静默：回看算不出来时用户必须知道是「取数失败」还是「真的没区分度」
      setError(e instanceof Error ? e.message : '回看数据加载失败');
    } finally {
      setLoading(false);
    }
  }, [side, model, asof]);

  useEffect(() => {
    void load();
  }, [load]);

  // 拖动底边改高度。listen 在 window 上，指针滑出把手也不会断。
  useEffect(() => {
    if (!dragging) return;
    const onMove = (e: MouseEvent) => {
      const d = dragRef.current;
      if (d) setBodyH(clampBody(d.h + (e.clientY - d.y)));
    };
    const onUp = () => {
      setDragging(false);
      saveBody(bodyHRef.current);
    };
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
    return () => {
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
    };
  }, [dragging]);

  const startDrag = (e: React.MouseEvent) => {
    e.preventDefault();
    dragRef.current = { y: e.clientY, h: bodyH };
    setDragging(true);
  };

  const toggle = () => {
    const next = !collapsed;
    setCollapsed(next);
    if (!next) {
      // 展开后卡片多半在折线以下：不滚过去，用户点了「展开」屏幕上没有任何变化
      requestAnimationFrame(() => rootRef.current?.scrollIntoView?.({ block: 'start', behavior: 'smooth' }));
    }
  };

  const summary = data?.summary ?? [];
  const items = data?.detail?.items ?? [];
  const lookbacks = data?.lookbacks ?? DEFAULT_LOOKBACKS;
  const usable = isVerdictUsable(summary);
  const priceLabel = priceSourceLabel(
    data?.price_source,
    data?.live_count ?? 0,
    (data?.live_count ?? 0) + (data?.close_count ?? 0),
  );

  if (collapsed) {
    return (
      <section ref={rootRef} className={`${SHELL} overflow-hidden ${className}`}>
        <button
          type="button"
          onClick={toggle}
          title="展开信号准确率回看"
          className="flex w-full items-center gap-3 px-3 py-2.5 text-left transition-colors hover:bg-slate-50"
        >
          <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-xl bg-gradient-to-br from-indigo-500 to-blue-500">
            <History className="h-4 w-4 text-white" />
          </span>
          <span className="min-w-0 flex-1">
            <span className="flex items-center gap-2">
              <span className="text-[13px] font-bold text-slate-800">信号准确率回看</span>
              {data?.as_of && (
                <span className="rounded-md bg-slate-100 px-1.5 py-0.5 font-mono text-[10px] text-slate-500">
                  锚点 {data.as_of}
                </span>
              )}
            </span>
            <span className="mt-0.5 block truncate text-[11px] text-slate-500">
              T-3 / T-5 / T-10 给高分的票后来涨了还是跌了 —— 逐只明细 · 分档命中率 · 负分档跌了多少
            </span>
          </span>
          <span className="flex shrink-0 items-center gap-1 rounded-xl bg-slate-800 px-3 py-1.5 text-[11px] font-bold text-white shadow-sm">
            展开 <ChevronUp className="h-3.5 w-3.5" />
          </span>
        </button>
      </section>
    );
  }

  return (
    <section ref={rootRef} className={`${SHELL} flex flex-col p-3 ${className}`}>
      <CardHeader
        icon={<History className="w-4 h-4" />}
        title="信号准确率回看"
        meta={
          <span className="flex items-center gap-2 text-[10px] text-slate-400">
            {data?.as_of && <span className="font-mono">锚点 {data.as_of}</span>}
            {priceLabel && (
              <span className="rounded-md bg-slate-100 px-1.5 py-0.5 font-semibold text-slate-500">
                {priceLabel}
                {data?.price_as_of ? ` · ${data.price_as_of}` : ''}
              </span>
            )}
          </span>
        }
        extra={
          <span className="flex items-center gap-1">
            <button
              type="button"
              onClick={() => void load()}
              disabled={loading}
              title="重新取数"
              className="rounded-lg p-1.5 text-slate-400 transition-colors hover:bg-slate-100 hover:text-slate-600 disabled:opacity-40"
            >
              <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} />
            </button>
            <button
              type="button"
              onClick={toggle}
              title="折叠"
              className="rounded-lg p-1.5 text-slate-400 transition-colors hover:bg-slate-100 hover:text-slate-600"
            >
              <ChevronDown className="w-3.5 h-3.5" />
            </button>
          </span>
        }
      />

      <div className="-mx-1 overflow-auto px-1" style={{ height: bodyH }}>
        {error ? (
          <div className="flex items-center gap-2 rounded-xl border border-amber-200 bg-amber-50/70 px-3 py-2 text-[11px] text-amber-700">
            <AlertTriangle className="w-3.5 h-3.5 shrink-0" />
            <span>{error}</span>
            <button type="button" onClick={() => void load()} className="ml-auto font-semibold underline">
              重试
            </button>
          </div>
        ) : loading && !data ? (
          <div className="py-6 text-center text-[11px] text-slate-300">回看数据加载中…</div>
        ) : data?.status === 'unavailable' ? (
          <div className="flex items-center gap-2 rounded-xl border border-slate-200 bg-slate-50 px-3 py-2 text-[11px] text-slate-500">
            <AlertTriangle className="w-3.5 h-3.5 shrink-0" />
            <span>{data.reason ?? '覆盖不足，暂无可回看的信号日'}</span>
          </div>
        ) : !usable ? (
          /* 零项参与不算通过：算不出价差时明说，别渲染一张全 `—` 的表 */
          <div className="flex items-center gap-2 rounded-xl border border-amber-200 bg-amber-50/70 px-3 py-2 text-[11px] text-amber-700">
            <AlertTriangle className="w-3.5 h-3.5 shrink-0" />
            <span>
              所有回看点都缺基准价或现价，<b>算不出涨跌</b>（不代表模型没有区分度）。
              可换锚点日重试。
            </span>
          </div>
        ) : (
          <>
            {/* 汇总：每回看点一行，覆盖全市场打分集 */}
            <table className="w-full text-xs table-fixed">
              <thead className="bg-gray-50 border-b border-gray-200 sticky top-0 z-10">
                <tr>
                  <th className="px-2 py-1.5 text-center font-semibold text-gray-600 w-[7%]">回看点</th>
                  <th className="px-2 py-1.5 text-center font-semibold text-gray-600 w-[10%]">信号日 / 基准日</th>
                  <th className="px-2 py-1.5 text-center font-semibold text-gray-600 w-[7%]">样本</th>
                  <th className="px-2 py-1.5 text-center font-semibold text-gray-600 w-[11%]">高分档均涨</th>
                  <th className="px-2 py-1.5 text-center font-semibold text-gray-600 w-[11%]">低分档均涨</th>
                  <th className="px-2 py-1.5 text-center font-semibold text-gray-600 w-[10%]">价差</th>
                  <th className="px-2 py-1.5 text-center font-semibold text-gray-600 w-[9%]">高分命中</th>
                  <th className="px-2 py-1.5 text-center font-semibold text-gray-600 w-[9%]">低分命中</th>
                  <th className="px-2 py-1.5 text-center font-semibold text-gray-600 w-[11%]">负分档均涨</th>
                  <th className="px-2 py-1.5 text-center font-semibold text-gray-600 w-[15%]">run</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {summary.map((row: LookbackSummaryRow) => (
                  <tr key={row.lookback} className="hover:bg-gray-50 transition-colors">
                    <td className="px-2 py-1.5 text-center font-mono font-bold text-slate-800">
                      {row.label || lookbackLabel(row.lookback)}
                      {!row.comparable && (
                        <AlertTriangle
                          className="ml-1 inline w-3 h-3 text-amber-500"
                          aria-label="该行 run 与锚点不同口径"
                        />
                      )}
                    </td>
                    <td className="px-2 py-1.5 text-center font-mono text-[10px] text-slate-500">
                      <div>{row.signal_date}</div>
                      <div className="text-slate-300">{row.base_price_date ?? '—'}</div>
                    </td>
                    <td className="px-2 py-1.5 text-center font-mono text-slate-500">
                      {row.sample}
                      {row.missing_price > 0 && (
                        <span className="ml-1 text-[10px] text-amber-500" title="缺基准价或现价的只数">
                          缺{row.missing_price}
                        </span>
                      )}
                    </td>
                    <td className="px-2 py-1.5 text-center font-mono">
                      <PctText value={toPct(row.hi_avg)} />
                    </td>
                    <td className="px-2 py-1.5 text-center font-mono">
                      <PctText value={toPct(row.lo_avg)} />
                    </td>
                    <td className="px-2 py-1.5 text-center font-mono">
                      {/* 价差 = 高分档 − 低分档，正数才叫有区分度 —— 全表的主读数列 */}
                      {row.spread === null || row.spread === undefined ? (
                        <span className="text-slate-300">—</span>
                      ) : (
                        <span
                          className={`text-sm font-extrabold tabular-nums ${
                            row.spread > 0 ? 'text-red-600' : row.spread < 0 ? 'text-green-600' : 'text-slate-500'
                          }`}
                        >
                          {row.spread > 0 ? '+' : ''}
                          {(row.spread * 100).toFixed(2)}pp
                        </span>
                      )}
                    </td>
                    <td className="px-2 py-1.5 text-center">
                      <Hit v={row.hi_hit} />
                    </td>
                    <td className="px-2 py-1.5 text-center">
                      <Hit v={row.lo_hit} />
                    </td>
                    <td className="px-2 py-1.5 text-center font-mono">
                      <PctText value={toPct(row.neg_avg)} />
                    </td>
                    <td className="px-2 py-1.5 text-center font-mono text-[9px] text-slate-300 truncate">
                      {row.run_id ?? '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>

            {summary.some((r) => !r.comparable) && (
              <p className="mt-1.5 flex items-start gap-1.5 rounded-lg bg-amber-50/70 px-2 py-1.5 text-[10px] leading-4 text-amber-700">
                <AlertTriangle className="mt-0.5 w-3 h-3 shrink-0" />
                <span>
                  带三角标的那天，模型下的是<b>另一套分数尺</b>（离散度与锚点差 3 倍以上），
                  所以那行的 <b>分数/名次不能和别的行直接比大小</b>；涨跌与命中率仍算得没错，
                  量的是那天那个 run 自己的成绩。各行的 <code className="font-mono">run</code> 已列出备查。
                </span>
              </p>
            )}

            {/* 明细：一行一只票，按锚点日名次升序 */}
            <div className="mt-2 flex items-center gap-2">
              <span className="text-[11px] font-bold text-slate-700">逐只明细</span>
              <span className="text-[10px] text-slate-400">
                锚点日 {data?.as_of ?? '—'} 候选 {data?.detail?.total ?? 0} 只
                {side ? ` · 信号=${side}` : ''} · 按名次升序，本页 {items.length} 只
              </span>
            </div>
            <table className="mt-1 w-full text-xs table-fixed">
              <thead className="bg-gray-50 border-b border-gray-200 sticky top-0 z-10">
                <tr>
                  <th className="px-2 py-1.5 text-left font-semibold text-gray-600 w-[16%]">股票</th>
                  <th className="px-2 py-1.5 text-center font-semibold text-gray-600 w-[12%]">当前分数</th>
                  {[...lookbacks]
                    .sort((a, b) => b - a)
                    .map((n) => (
                      <th key={n} className="px-2 py-1.5 text-center font-semibold text-gray-600">
                        {lookbackLabel(n)}
                        <span className="ml-1 font-normal text-gray-400">分数·名次 / 至今</span>
                      </th>
                    ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {items.length === 0 ? (
                  <tr>
                    <td colSpan={2 + lookbacks.length} className="px-3 py-8 text-center text-[11px] text-slate-300">
                      锚点日没有符合条件的候选
                    </td>
                  </tr>
                ) : (
                  items.map((it: LookbackDetailItem) => (
                    <tr key={it.symbol} className="hover:bg-gray-50 transition-colors">
                      <td className="px-2 py-1.5">
                        <div className="flex items-baseline gap-1.5 min-w-0">
                          <span className="truncate font-semibold text-slate-800">{it.name || it.symbol}</span>
                          <span className="shrink-0 font-mono text-[10px] text-slate-400">
                            {it.symbol.split('.')[0]}
                          </span>
                        </div>
                      </td>
                      <td className="px-2 py-1.5 text-center">
                        <div className="font-mono text-[11px] font-bold text-slate-700">
                          {formatScore(it.score_now)}
                        </div>
                        <div className="font-mono text-[10px] text-slate-400">{formatRank(it.rank_now)}</div>
                      </td>
                      {orderPoints(it.points, lookbacks).map((p) => (
                        <td key={p.lookback} className="px-2 py-1.5 text-center">
                          <div className="font-mono text-[10px] text-slate-400">
                            {formatScore(p.score)}
                            <span className="mx-1 text-slate-300">·</span>
                            {formatRank(p.rank, p.day_n)}
                          </div>
                          <PctText value={toPct(p.ret)} className="text-[11px]" />
                        </td>
                      ))}
                    </tr>
                  ))
                )}
              </tbody>
            </table>

            <p className="mt-1.5 text-[10px] leading-4 text-slate-400">
              高分档 = 当日分位前 {(100 * (data?.bucket_pct ?? 0.2)).toFixed(0)}%；低分档 = 后同比例，<b>含全部分数为负的票</b>；
              负分档 = 分数 &lt; 0（单独一列，回答「分数是负的跌了多少」）。基准价取<b>回看日当日收盘</b>
              （前复权，「看到信号按当日收盘买入」口径）。现价取{priceLabel || '最新收盘'}
              {data?.price_as_of ? `（${data.price_as_of}）` : ''}；盘中为实时价并按除权系数校正。
              命中率方向相反才对：高分档看上涨占比、低分档看下跌占比。缺价或缺分数一律显示 <b>—</b>，不补 0。
            </p>
          </>
        )}
      </div>

      {/* 底边拖高：展开后卡片约 20 行；嫌占地方就往上拖，嫌不够就往下拖 */}
      <div
        onMouseDown={startDrag}
        title="拖动调整高度"
        className={`-mb-1 mt-1 flex h-3 shrink-0 cursor-row-resize items-center justify-center ${
          dragging ? 'select-none' : ''
        }`}
      >
        <span
          className={`h-1 w-12 rounded-full transition-colors ${
            dragging ? 'bg-blue-400' : 'bg-slate-200 hover:bg-slate-400'
          }`}
        />
      </div>
    </section>
  );
};

export default SignalLookbackCard;
