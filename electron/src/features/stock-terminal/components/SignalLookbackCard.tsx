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
 */
import React, { useCallback, useEffect, useState } from 'react';
import { AlertTriangle, ChevronDown, ChevronUp, History, RefreshCw } from 'lucide-react';
import { CARD, CardHeader } from '../../desk/components/cardKit';
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
  const [collapsed, setCollapsed] = useState(false);

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

  const summary = data?.summary ?? [];
  const items = data?.detail?.items ?? [];
  const lookbacks = data?.lookbacks ?? DEFAULT_LOOKBACKS;
  const usable = isVerdictUsable(summary);
  const priceLabel = priceSourceLabel(
    data?.price_source,
    data?.live_count ?? 0,
    (data?.live_count ?? 0) + (data?.close_count ?? 0),
  );

  return (
    <section className={`${CARD} p-3 gap-0 ${className}`}>
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
              onClick={() => setCollapsed(!collapsed)}
              title={collapsed ? '展开' : '折叠'}
              className="rounded-lg p-1.5 text-slate-400 transition-colors hover:bg-slate-100 hover:text-slate-600"
            >
              {collapsed ? <ChevronUp className="w-3.5 h-3.5" /> : <ChevronDown className="w-3.5 h-3.5" />}
            </button>
          </span>
        }
      />

      {collapsed ? null : (
        <div className="max-h-[38vh] overflow-auto -mx-1 px-1">
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
      )}
    </section>
  );
};

export default SignalLookbackCard;
