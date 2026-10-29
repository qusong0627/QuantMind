/** 聪明钱 · 选股榜 v2（细案 §7：B1 空态分流 / B8 服务端分页 / B6 快照日期 / 行点击开档案抽屉）。
 *
 * 服务端筛选（event=最近事件类型 / industry=行业精确 / min_score=复合分下限）+
 * 服务端分页（50/页）；自持取数（seq 防串页），失败显式错误条不伪装空数据。
 * 展示口径：分与分位=研究评分（中性紫）；金额正负才用红绿；缺失「—」不补 0。
 */

import React, { useCallback, useEffect, useRef, useState } from 'react';
import { AlertCircle, Crosshair, ListFilter } from 'lucide-react';
import { Tooltip } from 'antd';
import { EmptyHint, FaceCard } from '../../../market-analysis-shared/ui';
import { StockPillarHeatmap, StockScoreBar, StockScoreScatter, TopStockStrip } from './SmartMoneyStockCharts';
import {
  getSmartMoneyStocks,
  type SmartPillarMeta,
  type SmartStockItem,
  type SmartStocksResponse,
} from '../../services/smartMoneyService';
import {
  MISSING,
  directionHint,
  eventTypeLabel,
  fmtScore,
  fmtSigned,
  fmtYiFromWan,
  fmtYiFromYuan,
  pillarShort,
  scoreCls,
  signCls,
} from '../../smartMoneyVocabulary';

const EVENT_FILTER_ORDER = ['lhb', 'inst', 'block_trade', 'survey'] as const;
const PAGE_SIZE = 50;
const MIN_SCORE_OPTIONS = [
  { id: 0, label: '全部分' },
  { id: 60, label: '≥60' },
  { id: 80, label: '≥80' },
];

const COLS =
  'grid-cols-[24px_minmax(150px,1.2fr)_84px_88px_minmax(150px,1fr)_84px_84px_78px_64px_60px]';

interface SmartMoneyStockBoardProps {
  /** 快照日期（null=最新）；变化即整表重取（B6） */
  date: string | null;
  /** 外部刷新信号（父级「刷新」按钮递增值触发重取） */
  refreshToken?: number;
  /** 行业筛选项（来源=overview.industry 行业名） */
  industryOptions?: string[];
  /** 行点击 → 个股档案抽屉 */
  onPick?: (symbol: string) => void;
  /** 响应元数据上抛（面板头部展示 as_of/partial/池规模） */
  onMeta?: (meta: { as_of: string | null; partial: boolean | null; total: number } | null) => void;
  /** true = 无卡壳（统一大框内的标签面） */
  bare?: boolean;
  className?: string;
}

interface BoardQuery {
  event: string | null;
  industry: string | null;
  minScore: number;
  offset: number;
}

export const SmartMoneyStockBoard: React.FC<SmartMoneyStockBoardProps> = ({
  date,
  refreshToken = 0,
  industryOptions,
  onPick,
  onMeta,
  bare = false,
  className = '',
}) => {
  const [q, setQ] = useState<BoardQuery>({ event: null, industry: null, minScore: 0, offset: 0 });
  const [view, setView] = useState<'chart' | 'table'>('chart');
  const [resp, setResp] = useState<SmartStocksResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const seq = useRef(0);

  // 筛选变化 → 回第 1 页（避免第 3 页下筛选后空窗误判）
  const setFilter = useCallback((patch: Partial<BoardQuery>) => {
    setQ((prev) => ({ ...prev, ...patch, offset: 0 }));
  }, []);

  const load = useCallback(async () => {
    const my = ++seq.current;
    setLoading(true);
    setError(null);
    try {
      const r = await getSmartMoneyStocks({
        limit: PAGE_SIZE,
        offset: q.offset,
        date: date ?? undefined,
        event: q.event ?? undefined,
        industry: q.industry ?? undefined,
        minScore: q.minScore > 0 ? q.minScore : undefined,
      });
      if (my !== seq.current) return;
      setResp(r);
      onMeta?.({ as_of: r.as_of ?? null, partial: r.partial ?? null, total: r.total });
    } catch (e) {
      if (my !== seq.current) return;
      setError(e instanceof Error ? e.message : '选股榜加载失败');
      onMeta?.(null);
    } finally {
      if (my === seq.current) setLoading(false);
    }
  }, [q, date, onMeta, refreshToken]);

  useEffect(() => {
    void load();
  }, [load]);

  const items: SmartStockItem[] = resp?.items ?? [];
  const pillars: SmartPillarMeta[] = resp?.pillars ?? [];
  const total = resp?.total ?? 0;
  const page = Math.floor(q.offset / PAGE_SIZE) + 1;
  const nPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const hasFilter = q.event !== null || q.industry !== null || q.minScore > 0;
  const snapMismatch =
    !!date && !!resp?.as_of && resp.as_of !== date && !loading && !error;

  return (
    <FaceCard
      bare={bare}
      className={className}
      title={
        <span className="flex items-center gap-1.5">
          <Crosshair className="w-3.5 h-3.5 text-purple-600" />
          聪明钱选股榜（池内复合分降序）
          <span className="text-[10px] font-normal text-slate-400">
            柱分位=池内截面 0–100（负向柱已按历史方向取反）· 缺失「{MISSING}」
          </span>
        </span>
      }
      extra={
        <div className="flex items-center gap-2 flex-wrap justify-end">
          <span className="text-[9px] font-mono text-slate-400 whitespace-nowrap">
            {resp?.as_of ? `${resp.as_of} 快照 · ` : ''}共 {total} 只
          </span>
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
        {/* 筛选行（服务端语义） */}
        <div className="flex items-center gap-1.5 flex-wrap">
          <span className="flex items-center gap-1 text-[9px] text-slate-400 font-bold">
            <ListFilter className="w-3 h-3" />
            最近事件
          </span>
          <button
            type="button"
            onClick={() => setFilter({ event: null })}
            className={`px-2 py-0.5 rounded-full text-[10px] font-extrabold border transition-colors ${
              q.event === null
                ? 'bg-purple-600 text-white border-purple-600'
                : 'bg-white text-slate-500 border-slate-200 hover:border-purple-300'
            }`}
          >
            全部
          </button>
          {EVENT_FILTER_ORDER.map((t) => (
            <button
              key={t}
              type="button"
              onClick={() => setFilter({ event: q.event === t ? null : t })}
              className={`px-2 py-0.5 rounded-full text-[10px] font-extrabold border transition-colors ${
                q.event === t
                  ? 'bg-purple-600 text-white border-purple-600'
                  : 'bg-white text-slate-500 border-slate-200 hover:border-purple-300'
              }`}
            >
              {eventTypeLabel(t)}
            </button>
          ))}
          <span className="w-px h-3 bg-slate-200 mx-0.5" />
          {MIN_SCORE_OPTIONS.map((opt) => (
            <button
              key={opt.id}
              type="button"
              onClick={() => setFilter({ minScore: opt.id })}
              className={`px-2 py-0.5 rounded-full text-[10px] font-extrabold border transition-colors ${
                q.minScore === opt.id
                  ? 'bg-purple-600 text-white border-purple-600'
                  : 'bg-white text-slate-500 border-slate-200 hover:border-purple-300'
              }`}
            >
              {opt.label}
            </button>
          ))}
          {!!industryOptions?.length && (
            <select
              value={q.industry ?? ''}
              onChange={(e) => setFilter({ industry: e.target.value || null })}
              className="px-1.5 py-0.5 rounded-full text-[10px] font-bold border border-slate-200 bg-white text-slate-600 max-w-[136px]"
            >
              <option value="">全部行业</option>
              {industryOptions.map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
            </select>
          )}
          {hasFilter && (
            <button
              type="button"
              onClick={() => setFilter({ event: null, industry: null, minScore: 0 })}
              className="text-[9px] font-bold text-slate-400 hover:text-purple-700 underline decoration-dotted"
            >
              清除筛选
            </button>
          )}
        </div>

        {snapMismatch && (
          <div className="mx-1 rounded-lg bg-sky-50 border border-sky-100 px-2 py-1 text-[9px] text-sky-800">
            所选日期 {date} 无计分快照，已回退至最近快照 {resp?.as_of}（条目为该日截面）。
          </div>
        )}

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

        {loading && !resp ? (
          <EmptyHint loading />
        ) : !items.length && !error ? (
          /* B1：筛选空态与基础空态分流，绝不落入误导性文案 */
          hasFilter ? (
            <EmptyHint text="当前筛选条件下榜内无匹配标的（可放宽分数下限或清除筛选）" />
          ) : (
            <EmptyHint text="当日事件池为空（无聪明钱事件触发）" />
          )
        ) : view === 'chart' ? (
          <div className="flex flex-col gap-3">
            <TopStockStrip items={items} onPick={onPick} />
            <div className="grid grid-cols-1 xl:grid-cols-2 gap-3 items-start">
              <div className="rounded-xl border border-slate-100 bg-white/60 p-1.5">
                <StockScoreBar items={items} onPick={onPick} />
              </div>
              <div className="rounded-xl border border-slate-100 bg-white/60 p-1.5">
                <StockPillarHeatmap items={items} pillars={pillars} onPick={onPick} />
              </div>
            </div>
            <div className="rounded-xl border border-slate-100 bg-white/60 p-1.5">
              <StockScoreScatter items={items} onPick={onPick} />
            </div>
            <div className="text-[9px] text-slate-400 px-1">
              图表取当前页前 20；完整 50 条与逐列数字切「明细」。分位=池内截面 0–100，灰格=缺失（不为 0）。
            </div>
          </div>
        ) : (
          <>
            {/* 限高滚动（表头吸顶）：50 行/页全展开会占近 2 屏，框内滚动阅读 */}
            <div className="max-h-[440px] overflow-auto custom-scrollbar rounded-lg border border-slate-100/80">
              <div className={`min-w-[860px] flex flex-col ${loading ? 'opacity-60' : ''} transition-opacity`}>
                <div
                  className={`grid ${COLS} gap-2 px-1 pb-1.5 text-[9px] font-extrabold text-slate-400 border-b border-slate-100 sticky top-0 bg-white/95 backdrop-blur z-[5]`}
                >
                  <span>#</span>
                  <span>标的 / 行业</span>
                  <span>最新事件</span>
                  <span className="text-right">机构净买·5日</span>
                  <span>柱分位</span>
                  <span className="text-right">龙虎榜·5日</span>
                  <span className="text-right">两融·5日</span>
                  <span className="text-right">北向Δ(季)</span>
                  <span className="text-right">覆盖</span>
                  <span className="text-right">分</span>
                </div>

                {items.map((it, i) => (
                  <div
                    key={it.symbol}
                    onClick={() => onPick?.(it.symbol)}
                    title={`打开 ${it.symbol} 个股档案`}
                    className={`grid ${COLS} gap-2 px-1 py-1.5 border-b border-slate-50 last:border-0 items-center cursor-pointer hover:bg-purple-50/60 transition-colors`}
                  >
                    <span
                      className={`w-5 h-5 rounded-md grid place-items-center text-[10px] font-extrabold ${
                        q.offset === 0 && i < 3 ? 'bg-purple-600 text-white' : 'bg-slate-100 text-slate-500'
                      }`}
                    >
                      {q.offset + i + 1}
                    </span>

                    <div className="min-w-0 flex items-center gap-1.5 flex-wrap">
                      <span className="text-[11px] font-bold text-slate-800 truncate">
                        {it.name || MISSING}
                      </span>
                      <span className="text-[9px] font-mono text-slate-400">{it.symbol}</span>
                      {it.industry && (
                        <span className="text-[9px] text-slate-400 bg-slate-50 border border-slate-100 rounded px-1">
                          {it.industry}
                        </span>
                      )}
                    </div>

                    <span className="text-[10px] text-slate-500 truncate">
                      {it.latest_event ? (
                        <>
                          {eventTypeLabel(it.latest_event.type)}
                          <span className="text-[9px] font-mono text-slate-400 ml-1">
                            {it.latest_event.date?.slice(5)}
                          </span>
                        </>
                      ) : (
                        MISSING
                      )}
                    </span>

                    <span className={`text-right text-[10px] font-mono font-bold ${signCls(it.inst_net_5d)}`}>
                      {fmtYiFromYuan(it.inst_net_5d)}
                    </span>

                    <span className="flex items-center gap-0.5 flex-wrap">
                      {pillars.map((p) => {
                        const v = it.pct?.[p.name];
                        const missing = v === null || v === undefined;
                        return (
                          <Tooltip
                            key={p.name}
                            title={`${p.label}：${
                              missing ? `${MISSING}（该柱当日无值）` : `分位 ${fmtScore(v)}`
                            } · 方向 ${directionHint(p.direction)}`}
                          >
                            <span
                              className={`px-1 rounded text-[9px] font-mono font-bold border whitespace-nowrap ${
                                missing
                                  ? 'bg-slate-50 text-slate-300 border-slate-100'
                                  : 'bg-purple-50 text-purple-700 border-purple-100'
                              }`}
                            >
                              {pillarShort(p.name)} {missing ? MISSING : Math.round(Number(v))}
                            </span>
                          </Tooltip>
                        );
                      })}
                    </span>

                    <span className={`text-right text-[10px] font-mono font-bold ${signCls(it.lhb_net_5d)}`}>
                      {fmtYiFromYuan(it.lhb_net_5d)}
                    </span>
                    <span className={`text-right text-[10px] font-mono font-bold ${signCls(it.margin_net_5d)}`}>
                      {fmtYiFromWan(it.margin_net_5d)}
                    </span>
                    <span className={`text-right text-[10px] font-mono ${signCls(it.north_q_delta_pp)}`}>
                      {it.north_q_delta_pp !== null && it.north_q_delta_pp !== undefined
                        ? `${fmtSigned(it.north_q_delta_pp, 1)}`
                        : MISSING}
                    </span>
                    <Tooltip
                      title={`有值柱数 ${it.covered ?? MISSING}/${it.n_pillars ?? MISSING}（缺失柱按 50 计入复合分，此处披露实际覆盖）`}
                    >
                      <span className="text-right text-[10px] font-mono text-slate-400">
                        {it.covered ?? MISSING}/{it.n_pillars ?? MISSING}
                      </span>
                    </Tooltip>
                    <span className={`text-right text-xs font-extrabold font-mono ${scoreCls(it.score)}`}>
                      {fmtScore(it.score)}
                    </span>
                  </div>
                ))}
              </div>
            </div>

            {/* 服务端分页（B8：绝不全量渲染） */}
            <div className="flex items-center gap-1.5 px-1">
              <span className="text-[9px] text-slate-400">
                第 {page}/{nPages} 页 · 每页 {PAGE_SIZE}
              </span>
              <div className="ml-auto flex items-center gap-1">
                <button
                  type="button"
                  disabled={q.offset <= 0}
                  onClick={() => setQ((prev) => ({ ...prev, offset: Math.max(0, prev.offset - PAGE_SIZE) }))}
                  className="px-2 py-0.5 rounded text-[10px] font-bold border border-slate-200 bg-white text-slate-600 disabled:opacity-40 disabled:cursor-not-allowed hover:border-purple-300"
                >
                  上一页
                </button>
                <button
                  type="button"
                  disabled={q.offset + PAGE_SIZE >= total}
                  onClick={() => setQ((prev) => ({ ...prev, offset: prev.offset + PAGE_SIZE }))}
                  className="px-2 py-0.5 rounded text-[10px] font-bold border border-slate-200 bg-white text-slate-600 disabled:opacity-40 disabled:cursor-not-allowed hover:border-purple-300"
                >
                  下一页
                </button>
              </div>
            </div>
          </>
        )}
      </div>
    </FaceCard>
  );
};
