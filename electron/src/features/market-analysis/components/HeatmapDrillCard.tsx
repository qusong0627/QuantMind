/**
 * 市场分析 · 板块热力矩形图谱 + 个股下钻容器
 *
 * 两层结构（同一张图内切换，不是 ECharts 嵌套 treemap）：
 *   L1 板块层：通达信二级行业 / 一级行业 / 概念板块（面积=板块总市值，颜色=涨跌幅或主力净额）
 *   L2 个股层：点板块钻取成分股（面积=个股总市值，颜色同口径），面包屑返回 L1
 *
 * 为什么不用 ECharts 原生嵌套 treemap：板块层组件本就在与 ECharts 内部布局 API
 * 打交道（标签居中），再嵌一层会把 hack 复杂度翻倍；用 React 状态驱动分层 + 面包屑，
 * 返回行为、颜色切换、逐块取色都更可控（可维护性优先）。
 */

import React, { useEffect, useMemo, useState } from 'react';
import { Layers, ChevronRight, ArrowLeft, Loader2, Info } from 'lucide-react';
import {
  SectorHeatmapChart,
  type SectorHeatmapItem,
} from '../../market-analysis-shared/SectorHeatmapChart';
import {
  getHeatmapDrill,
  getHeatmapTop,
  type HeatmapDirectionCell,
  type HeatmapDirectionMarket,
  type HeatmapDrillCategory,
  type HeatmapDrillResponse,
} from '../services/moneyFlowService';
import {
  computeSentimentScores,
  heatmapCellColor,
  heatmapDirectionLabel,
  heatmapLabelValue,
  vpLabel,
  HEATMAP_COLOR_LABEL,
  type HeatmapColorMode,
} from './heatmapColor';

/** 资金流/涨幅统计窗口。 */
const PERIOD_TABS: { id: string; label: string }[] = [
  { id: '1d', label: '1日' },
  { id: '2d', label: '2日' },
  { id: '3d', label: '3日' },
  { id: '5d', label: '5日' },
  { id: '10d', label: '10日' },
  { id: '20d', label: '20日' },
];

/** 方向概率周期（情绪模型只有 T+5/10/20；T+3 不硬凑）。 */
const DIRECTION_HORIZONS = [5, 10, 20] as const;

/** 顶层板块分类切换项（键名与后端 /heatmap 的 category 一致）。 */
const CATEGORY_TABS: { id: HeatmapDrillCategory; label: string }[] = [
  { id: 'shenwan', label: '二级行业' },
  { id: 'industry1', label: '一级行业' },
  { id: 'concept', label: '概念板块' },
];

interface HeatmapDrillCardProps {
  /** 顶层板块热力图数据（由页面预取，用于 shenwan 分类的首屏即时渲染） */
  topItems: SectorHeatmapItem[];
  /** 快照日期 YYYY-MM-DD（透传给下钻接口，支持历史回溯） */
  date?: string;
  /** 点击个股 → 打开个股资金流详情 */
  onStockClick?: (symbol: string, name: string) => void;
}

export const HeatmapDrillCard: React.FC<HeatmapDrillCardProps> = ({
  topItems,
  date,
  onStockClick,
}) => {
  const [category, setCategory] = useState<HeatmapDrillCategory>('shenwan');
  const [period, setPeriod] = useState<string>('1d');
  const [colorMode, setColorMode] = useState<HeatmapColorMode>('pct');
  const [directionH, setDirectionH] = useState<number>(10);
  const [directionMeta, setDirectionMeta] = useState<HeatmapDirectionMarket | null>(null);
  const directionReady = !!directionMeta?.available;
  const [drillName, setDrillName] = useState<string | null>(null);
  const [drill, setDrill] = useState<HeatmapDrillResponse | null>(null);
  const [drillLoading, setDrillLoading] = useState(false);
  // 非 shenwan 分类的顶层数据需自行拉取（页面只预取了 shenwan）
  const [ownTop, setOwnTop] = useState<SectorHeatmapItem[]>([]);
  const [topLoading, setTopLoading] = useState(false);

  // 切分类时退出下钻层，避免「二级行业 › 概念板块的板块名」这种跨体系错配
  useEffect(() => {
    setDrillName(null);
    setDrill(null);
  }, [category]);

  // 周期切换退出下钻层（下钻层是当日个股口径，避免与板块周期错配）
  useEffect(() => {
    setDrillName(null);
    setDrill(null);
  }, [period]);

  // 历史快照不含周期/加速度（不拿最新冒充历史）：选定历史日期时回到 1 日
  useEffect(() => {
    if (date) setPeriod('1d');
  }, [date]);

  // 下钻层是个股当日口径，没有板块方向/情绪热度字段——进入下钻自动回涨跌幅着色
  useEffect(() => {
    if (drillName && (colorMode === 'direction' || colorMode === 'sentiment')) {
      setColorMode('pct');
    }
  }, [drillName, colorMode]);

  // 方向概率只覆盖「行业二级 + 最新快照」；切换分类/历史日期或产物缺失时自动退出
  useEffect(() => {
    if (colorMode !== 'direction' || topLoading) return;
    if (category !== 'shenwan' || !!date || !directionReady) setColorMode('pct');
  }, [category, date, colorMode, directionReady, topLoading]);

  // 顶层数据：一律走实时全量 —— 快照库（SQLite sector_mv）既不含主力净额，
  // 又只存概念板块前 50，无法支撑「主力净额」着色与概念全量；页面预取的
  // shenwan 快照仅作首屏即时铺底，实时数据到达后替换（无估值/无 L2 时不闪空态）。
  useEffect(() => {
    let cancelled = false;
    // 不清空旧数据：日期/周期切换时保留上一帧，等新数据到达后由 ECharts 补间色块，
    // 这样能看到哪些板块颜色变了，而不是整图闪空再重画。
    setTopLoading(true);
    getHeatmapTop({ category, date, realtime: true, period })
      .then((res) => {
        if (cancelled) return;
        setOwnTop(res.items);
        setDirectionMeta(res.direction);
      })
      .catch(() => {
        if (cancelled) return;
        setOwnTop([]);
        setDirectionMeta(null);
      })
      .finally(() => {
        if (!cancelled) setTopLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [category, date, period]);

  // 拉取下钻数据
  useEffect(() => {
    if (!drillName) return;
    let cancelled = false;
    setDrillLoading(true);
    getHeatmapDrill({ category, name: drillName, date })
      .then((d) => {
        if (!cancelled) setDrill(d);
      })
      .catch(() => {
        if (!cancelled) setDrill(null);
      })
      .finally(() => {
        if (!cancelled) setDrillLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [drillName, category, date]);

  // 实时全量优先；尚未到达时用页面预取的 shenwan 快照铺底（避免首屏空态）
  const topData = ownTop.length > 0 ? ownTop : topItems;
  const sentimentReady = topData.some(
    (it) =>
      (typeof it.pct_above_ma20 === 'number' && Number.isFinite(it.pct_above_ma20)) ||
      (typeof it.breadth_flow === 'number' && Number.isFinite(it.breadth_flow)),
  );

  // 情绪热度依赖实时资金广度；历史快照/实时字段缺失时自动回涨跌幅
  useEffect(() => {
    if (colorMode === 'sentiment' && !topLoading && (!sentimentReady || !!date)) {
      setColorMode('pct');
    }
  }, [colorMode, sentimentReady, topLoading, date]);

  const periodDays = Number(period.replace('d', '')) || 1;
  const topChartData = useMemo(() => {
    const base = topData.map((it) => ({
      ...it,
      // >1 日时面积/颜色/标签都用周期涨幅；1 日两者相同
      pct_change: periodDays > 1 ? (it.pct_period ?? it.pct_change) : it.pct_change,
    }));
    if (colorMode === 'sentiment') return computeSentimentScores(base);
    if (colorMode !== 'direction') return base;
    // 方向模式：同一周期内按全板块截面排名着色；概率本身是历史频率，不按排名改写
    const h = String(directionH);
    const vals = base
      .map((it) => it.direction?.[h]?.rel_prob)
      .filter((v): v is number => typeof v === 'number' && Number.isFinite(v))
      .sort((a, b) => a - b);
    const rankOf = (v: number) => {
      if (vals.length <= 1) return 0.5;
      const below = vals.filter((x) => x < v).length;
      return below / (vals.length - 1);
    };
    return base.map((it) => {
      const cell = it.direction?.[h];
      if (!cell) return it;
      return {
        ...it,
        direction: {
          ...it.direction,
          [h]: { ...cell, rank: rankOf(cell.rel_prob) },
        },
      };
    });
  }, [topData, periodDays, colorMode, directionH]);
  const speedUp = useMemo(
    () =>
      topData
        .filter((it) => typeof it.flow_accel === 'number' && Number.isFinite(it.flow_accel))
        .sort((a, b) => (b.flow_accel ?? 0) - (a.flow_accel ?? 0))
        .slice(0, 6),
    [topData],
  );
  const speedDown = useMemo(
    () =>
      topData
        .filter((it) => typeof it.flow_accel === 'number' && Number.isFinite(it.flow_accel))
        .sort((a, b) => (a.flow_accel ?? 0) - (b.flow_accel ?? 0))
        .slice(0, 6),
    [topData],
  );

  // 相对方向榜：同周期内历史跑赢概率靠前/靠后（仅方向模式渲染）
  const directionRows = useMemo(() => {
    if (colorMode !== 'direction' || !directionMeta?.available) return { up: [], down: [] };
    const rows = topData
      .map((it) => ({ it, cell: it.direction?.[String(directionH)] as HeatmapDirectionCell | undefined }))
      .filter((r): r is { it: (typeof topData)[number]; cell: HeatmapDirectionCell } => !!r.cell)
      .sort((a, b) => b.cell.rel_prob - a.cell.rel_prob);
    return { up: rows.slice(0, 6), down: rows.slice(-6).reverse() };
  }, [topData, colorMode, directionH, directionMeta]);

  // 情绪热度榜：同一周期内截面分位合成后的靠前/靠后板块
  const sentimentRows = useMemo(() => {
    if (colorMode !== 'sentiment') return { up: [], down: [] };
    const rows = topChartData
      .map((it) => ({ it, score: it.sentiment_score }))
      .filter((r): r is { it: (typeof topChartData)[number]; score: number } =>
        typeof r.score === 'number' && Number.isFinite(r.score),
      )
      .sort((a, b) => b.score - a.score);
    return { up: rows.slice(0, 6), down: rows.slice(-6).reverse() };
  }, [topChartData, colorMode]);

  // 个股层：名称 → symbol 映射（treemap 点击回传的是显示名）
  const symbolByName = useMemo(() => {
    const m = new Map<string, string>();
    (drill?.items || []).forEach((it) => m.set(it.name, it.symbol));
    return m;
  }, [drill]);

  const drillChartData: SectorHeatmapItem[] = useMemo(
    () =>
      (drill?.items || []).map((it) => ({
        name: it.name,
        value: it.value,
        pct_change: it.pct_change,
        flow_net: it.flow_net,
        leader: undefined,
      })) as SectorHeatmapItem[],
    [drill],
  );

  const handleTopClick = (name: string) => {
    setDrillName(name);
  };

  const handleDrillClick = (name: string) => {
    const symbol = symbolByName.get(name);
    if (symbol && onStockClick) onStockClick(symbol, name);
  };

  const renderKey = `${category}-${drillName || 'top'}-${colorMode}-${period}-${directionH}`;
  const chartData = drillName ? drillChartData : topChartData;
  const isEmpty = chartData.length === 0;

  return (
    <div className="h-full bg-white/95 backdrop-blur-md rounded-2xl p-3.5 border border-purple-100/80 shadow-md shadow-purple-500/5 flex flex-col min-h-0">
      {/* 头部：标题 + 面包屑 + 分类/着色切换 */}
      <div className="flex items-center justify-between border-b border-purple-100/60 pb-2 mb-1 shrink-0 gap-2">
        <h3 className="text-xs font-extrabold text-slate-900 flex items-center gap-1.5 min-w-0">
          <Layers className="w-3.5 h-3.5 text-purple-600 shrink-0" />
          {drillName ? (
            <span className="flex items-center gap-1 min-w-0">
              <button
                type="button"
                onClick={() => setDrillName(null)}
                className="flex items-center gap-0.5 text-purple-600 hover:text-purple-800 hover:underline shrink-0"
                title="返回板块层"
              >
                <ArrowLeft className="w-3 h-3" />
                {CATEGORY_TABS.find((t) => t.id === category)?.label}
              </button>
              <ChevronRight className="w-3 h-3 text-slate-300 shrink-0" />
              <span className="truncate">{drillName}</span>
            </span>
          ) : (
            <span className="truncate">板块热力矩形图谱</span>
          )}
        </h3>
        <div className="flex items-center gap-1.5 shrink-0 flex-wrap justify-end">
          {/* 方向模式：T+5/10/20 概率周期；其余模式：资金/涨幅统计周期 */}
          {colorMode === 'direction' ? (
            <div className="flex rounded-full bg-slate-100 p-0.5" title="方向概率来自情绪模型 T+5/10/20">
              {DIRECTION_HORIZONS.map((h) => (
                <button
                  key={h}
                  type="button"
                  onClick={() => setDirectionH(h)}
                  className={`px-2 py-0.5 rounded-full text-[10px] font-bold transition-colors ${
                    directionH === h ? 'bg-white text-purple-700 shadow-2xs' : 'text-slate-500 hover:text-slate-800'
                  }`}
                >
                  T+{h}
                </button>
              ))}
            </div>
          ) : (
            <div className="flex rounded-full bg-slate-100 p-0.5">
              {PERIOD_TABS.map((p) => (
                <button
                  key={p.id}
                  type="button"
                  onClick={() => setPeriod(p.id)}
                  className={`px-1.5 py-0.5 rounded-full text-[10px] font-bold transition-colors ${
                    period === p.id ? 'bg-white text-purple-700 shadow-2xs' : 'text-slate-500 hover:text-slate-800'
                  }`}
                >
                  {p.label}
                </button>
              ))}
            </div>
          )}
          {/* 着色维度切换（两层共用）；方向概率仅行业二级最新快照 */}
          <div className="flex rounded-full bg-slate-100 p-0.5">
            {(['pct', 'flow', 'vp', 'sentiment', 'direction'] as HeatmapColorMode[]).map((mode) => {
              const directionDisabled =
                mode === 'direction' &&
                (category !== 'shenwan' || !!date || (!directionReady && !topLoading));
              const sentimentDisabled =
                mode === 'sentiment' && (!!date || (!sentimentReady && !topLoading));
              const disabled = directionDisabled || sentimentDisabled;
              return (
                <button
                  key={mode}
                  type="button"
                  disabled={disabled}
                  onClick={() => setColorMode(mode)}
                  title={
                    directionDisabled
                      ? '方向概率只覆盖行业二级最新快照；状态表未生成/历史快照/其它分类不可用'
                      : sentimentDisabled
                        ? '情绪热度需要实时资金广度字段（历史快照不可用）'
                        : mode === 'direction'
                          ? '颜色=板块间历史跑赢概率排名；数字=历史同状态跑赢概率（50%=无优势）'
                          : mode === 'sentiment'
                            ? '颜色=情绪热度：资金广度/广度变化/周期涨幅/资金加速的截面分位合成（0~100，不是概率）'
                            : undefined
                  }
                  className={`px-2 py-0.5 rounded-full text-[10px] font-bold transition-colors ${
                    colorMode === mode
                      ? 'bg-white text-purple-700 shadow-2xs'
                      : disabled
                        ? 'text-slate-300 cursor-not-allowed'
                        : 'text-slate-500 hover:text-slate-800'
                  }`}
                >
                  {HEATMAP_COLOR_LABEL[mode]}
                </button>
              );
            })}
          </div>
          {/* 顶层分类切换（仅板块层显示） */}
          {!drillName && (
            <div className="flex rounded-full bg-slate-100 p-0.5">
              {CATEGORY_TABS.map((t) => (
                <button
                  key={t.id}
                  type="button"
                  onClick={() => setCategory(t.id)}
                  className={`px-2 py-0.5 rounded-full text-[10px] font-bold transition-colors ${
                    category === t.id ? 'bg-white text-purple-700 shadow-2xs' : 'text-slate-500 hover:text-slate-800'
                  }`}
                >
                  {t.label}
                </button>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* 副标题条：口径与规模如实披露 */}
      <div className="flex items-center justify-between px-0.5 pb-1 shrink-0">
        <span className="text-[10px] text-slate-400 font-mono">
          面积=总市值 · 周期={periodDays}日 · 颜色={HEATMAP_COLOR_LABEL[colorMode]}
          {colorMode === 'flow' && '（周期主力净额）'}
          {colorMode === 'accel' && '（当前日均 − 前一窗口日均）'}
          {colorMode === 'vp' && '（资金加速方向 × 价格加速方向；同向=共振，反向=背离）'}
          {colorMode === 'direction' &&
            `（T+${directionH} 历史跑赢概率；颜色/箭头=板块间排名，数字=概率，50%=无优势）`}
          {colorMode === 'sentiment' &&
            '（情绪热度=价格广度(MA20)/资金广度/两者变化/周期涨幅/资金加速的截面分位合成，0~100）'}
          {drillName && '（下钻层为当日个股口径）'}
        </span>
        {drillName && drill && (
          <span className="text-[10px] text-slate-400 font-mono">
            {drill.trade_date} · {drill.summary.shown}/{drill.summary.total} 只
            {drill.summary.truncated ? '（按市值截断）' : ''} · 均涨 {drill.summary.avg_pct >= 0 ? '+' : ''}
            {drill.summary.avg_pct}%
          </span>
        )}
        {topLoading && !isEmpty && (
          <span className="text-[10px] text-purple-500 font-mono animate-pulse">更新中…</span>
        )}
      </div>

      {/* 方向模式 · 大盘情绪基准：压力状态 + 同状态分布 + 全A模型 T+5/10/20 */}
      {(colorMode === 'direction' || colorMode === 'sentiment') && directionMeta && (
        <div className="rounded-xl border border-purple-100/80 bg-gradient-to-r from-purple-50/70 to-slate-50/60 px-2.5 py-1.5 shrink-0 text-[10px] leading-relaxed">
          <div className="flex items-center gap-1.5 flex-wrap">
            <Info className="w-3 h-3 text-purple-500 shrink-0" />
            <span className="font-extrabold text-purple-700">
              {colorMode === 'sentiment' ? '大盘情绪基准' : '大盘方向基准'}
            </span>
            <span className="font-mono text-slate-700">
              {directionMeta.stress_label ?? '—'} {directionMeta.stress_score ?? '—'}
              {directionMeta.phase ? ` · ${directionMeta.phase}` : ''}
            </span>
            <span className="text-slate-400">·</span>
            <span className="text-slate-600">
              同状态 20 日：胜率{' '}
              <b className="font-mono text-slate-800">
                {directionMeta.win20 == null ? '—' : `${(directionMeta.win20 * 100).toFixed(1)}%`}
              </b>{' '}
              中位{' '}
              <b className="font-mono text-slate-800">
                {directionMeta.median20 == null ? '—' : `${directionMeta.median20 > 0 ? '+' : ''}${directionMeta.median20.toFixed(2)}%`}
              </b>{' '}
              八成 [
              <span className="font-mono">
                {directionMeta.p10_20 == null ? '—' : `${directionMeta.p10_20.toFixed(1)}%`},{' '}
                {directionMeta.p90_20 == null ? '—' : `${directionMeta.p90_20.toFixed(1)}%`}
              </span>
              ]（n={directionMeta.n_days ?? '—'}）
            </span>
            <span className="text-slate-400">·</span>
            <span className="text-slate-600">
              全A 模型{' '}
              {DIRECTION_HORIZONS.map((h) => {
                const p = directionMeta.market_prob?.[String(h)];
                return (
                  <span key={h} className="font-mono mr-1.5">
                    T+{h} <b className="text-slate-800">{p == null ? '—' : `${(p * 100).toFixed(1)}%`}</b>
                  </span>
                );
              })}
            </span>
          </div>
          {directionMeta.indices && directionMeta.indices.length > 0 && (
            <div className="flex items-center gap-2 overflow-x-auto custom-scrollbar mt-0.5 text-[9px] text-slate-500">
              <span className="shrink-0 text-slate-400">指数</span>
              {directionMeta.indices.map((idx) => (
                <span key={idx.key} className="shrink-0 font-mono">
                  {idx.name}{' '}
                  <b className={idx.change_pct == null ? 'text-slate-400' : idx.change_pct >= 0 ? 'text-red-500' : 'text-emerald-600'}>
                    {idx.change_pct == null ? '—' : `${idx.change_pct > 0 ? '+' : ''}${idx.change_pct.toFixed(2)}%`}
                  </b>
                </span>
              ))}
            </div>
          )}
          {colorMode === 'direction' && directionMeta.disclosure && (
            <div className="text-[9px] text-slate-400 mt-0.5">
              {directionMeta.disclosure} 颜色/箭头=板块间相对排名（前 50% ↑ / 后 50% ↓），
              数字是历史跑赢概率（50%=无相对优势），不是未来保证。
            </div>
          )}
          {colorMode === 'sentiment' && (
            <div className="text-[9px] text-slate-400 mt-0.5">
              情绪热度=价格广度(MA20) / 资金广度 / 广度变化 / 周期涨幅 / 资金加速的截面分位合成（0~100，
              不是概率）；大盘基准来自情绪快照，热度只做板块间相对比较。
            </div>
          )}
        </div>
      )}

      {/* 加速榜：直接回答「哪些板块在加速流入/流出、涨幅怎么样」 */}

      {!drillName && colorMode !== 'direction' && colorMode !== 'sentiment' && (speedUp.length > 0 || speedDown.length > 0) && (
        <div className="flex items-center gap-1.5 overflow-x-auto custom-scrollbar pb-1 shrink-0 text-[10px]">
          <span className="font-extrabold text-red-600 shrink-0">加速流入</span>
          {speedUp.slice(0, 4).map((it) => (
            <button
              key={`up-${it.name}`}
              onClick={() => handleTopClick(it.name)}
              className="shrink-0 inline-flex items-center gap-1 px-1.5 py-0.5 rounded-full bg-red-50 border border-red-100 text-red-700 hover:bg-red-100"
              title={`${periodDays}日净额 ${it.flow_net?.toFixed(2) ?? '—'}亿 · 日均 ${it.flow_daily?.toFixed(2) ?? '—'}亿/日 · 加速度 ${it.flow_accel?.toFixed(2) ?? '—'}亿/日`}
            >
              {it.name}
              <span className="font-mono">{it.flow_accel !== null && it.flow_accel !== undefined ? `+${it.flow_accel.toFixed(2)}` : '—'}</span>
              <span className="text-red-400 font-mono">{it.pct_period?.toFixed(1) ?? '—'}%</span>
            </button>
          ))}
          <span className="font-extrabold text-emerald-600 shrink-0 ml-2">加速流出</span>
          {speedDown.slice(0, 4).map((it) => (
            <button
              key={`down-${it.name}`}
              onClick={() => handleTopClick(it.name)}
              className="shrink-0 inline-flex items-center gap-1 px-1.5 py-0.5 rounded-full bg-emerald-50 border border-emerald-100 text-emerald-700 hover:bg-emerald-100"
              title={`${periodDays}日净额 ${it.flow_net?.toFixed(2) ?? '—'}亿 · 日均 ${it.flow_daily?.toFixed(2) ?? '—'}亿/日 · 加速度 ${it.flow_accel?.toFixed(2) ?? '—'}亿/日`}
            >
              {it.name}
              <span className="font-mono">{it.flow_accel?.toFixed(2) ?? '—'}</span>
              <span className="text-emerald-400 font-mono">{it.pct_period?.toFixed(1) ?? '—'}%</span>
            </button>
          ))}
        </div>
      )}

      {/* 方向榜：同周期历史跑赢概率靠前/靠后（榜单不替代概率，只做入口） */}
      {colorMode === 'direction' && (directionRows.up.length > 0 || directionRows.down.length > 0) && (
        <div className="flex items-center gap-1.5 overflow-x-auto custom-scrollbar pb-1 shrink-0 text-[10px]">
          <span className="font-extrabold text-red-600 shrink-0">相对方向靠前</span>
          {directionRows.up.slice(0, 4).map(({ it, cell }) => (
            <button
              key={`dir-up-${it.name}`}
              onClick={() => handleTopClick(it.name)}
              className="shrink-0 inline-flex items-center gap-1 px-1.5 py-0.5 rounded-full bg-red-50 border border-red-100 text-red-700 hover:bg-red-100"
              title={`T+${directionH} 跑赢大盘概率 ${(cell.rel_prob * 100).toFixed(1)}% · 绝对上涨 ${(cell.prob * 100).toFixed(1)}% · n=${cell.n ?? '—'} · 日期胜率 ${cell.date_win == null ? '—' : `${(cell.date_win * 100).toFixed(0)}%`}`}
            >
              {it.name}
              <span className="font-mono">{heatmapDirectionLabel(cell)}</span>
              <span className="text-red-400 font-mono">{it.pct_period?.toFixed(1) ?? '—'}%</span>
            </button>
          ))}
          <span className="font-extrabold text-emerald-600 shrink-0 ml-2">相对方向靠后</span>
          {directionRows.down.slice(0, 4).map(({ it, cell }) => (
            <button
              key={`dir-down-${it.name}`}
              onClick={() => handleTopClick(it.name)}
              className="shrink-0 inline-flex items-center gap-1 px-1.5 py-0.5 rounded-full bg-emerald-50 border border-emerald-100 text-emerald-700 hover:bg-emerald-100"
              title={`T+${directionH} 跑赢大盘概率 ${(cell.rel_prob * 100).toFixed(1)}% · 绝对上涨 ${(cell.prob * 100).toFixed(1)}% · n=${cell.n ?? '—'} · 日期胜率 ${cell.date_win == null ? '—' : `${(cell.date_win * 100).toFixed(0)}%`}`}
            >
              {it.name}
              <span className="font-mono">{heatmapDirectionLabel(cell)}</span>
              <span className="text-emerald-400 font-mono">{it.pct_period?.toFixed(1) ?? '—'}%</span>
            </button>
          ))}
        </div>
      )}

      {/* 情绪热度榜：截面合成后的靠前/靠后板块（榜单不替代颜色，只做入口） */}
      {colorMode === 'sentiment' && (sentimentRows.up.length > 0 || sentimentRows.down.length > 0) && (
        <div className="flex items-center gap-1.5 overflow-x-auto custom-scrollbar pb-1 shrink-0 text-[10px]">
          <span className="font-extrabold text-red-600 shrink-0">情绪热度靠前</span>
          {sentimentRows.up.slice(0, 4).map(({ it, score }) => (
            <button
              key={`sent-up-${it.name}`}
              onClick={() => handleTopClick(it.name)}
              className="shrink-0 inline-flex items-center gap-1 px-1.5 py-0.5 rounded-full bg-red-50 border border-red-100 text-red-700 hover:bg-red-100"
              title={`情绪热度 ${(score * 100).toFixed(0)} · 价格广度 ${it.pct_above_ma20 == null ? '—' : `${(it.pct_above_ma20 * 100).toFixed(0)}%`} · 资金广度 ${it.breadth_flow == null ? '—' : `${(it.breadth_flow * 100).toFixed(0)}%`} · 资金广度变化 ${it.breadth_flow_chg == null ? '—' : `${it.breadth_flow_chg > 0 ? '+' : ''}${(it.breadth_flow_chg * 100).toFixed(1)}pp`}`}
            >
              {it.name}
              <span className="font-mono">{heatmapLabelValue(it, 'sentiment')}</span>
              <span className="text-red-400 font-mono">{it.pct_period?.toFixed(1) ?? '—'}%</span>
            </button>
          ))}
          <span className="font-extrabold text-emerald-600 shrink-0 ml-2">情绪热度靠后</span>
          {sentimentRows.down.slice(0, 4).map(({ it, score }) => (
            <button
              key={`sent-down-${it.name}`}
              onClick={() => handleTopClick(it.name)}
              className="shrink-0 inline-flex items-center gap-1 px-1.5 py-0.5 rounded-full bg-emerald-50 border border-emerald-100 text-emerald-700 hover:bg-emerald-100"
              title={`情绪热度 ${(score * 100).toFixed(0)} · 价格广度 ${it.pct_above_ma20 == null ? '—' : `${(it.pct_above_ma20 * 100).toFixed(0)}%`} · 资金广度 ${it.breadth_flow == null ? '—' : `${(it.breadth_flow * 100).toFixed(0)}%`} · 资金广度变化 ${it.breadth_flow_chg == null ? '—' : `${it.breadth_flow_chg > 0 ? '+' : ''}${(it.breadth_flow_chg * 100).toFixed(1)}pp`}`}
            >
              {it.name}
              <span className="font-mono">{heatmapLabelValue(it, 'sentiment')}</span>
              <span className="text-emerald-400 font-mono">{it.pct_period?.toFixed(1) ?? '—'}%</span>
            </button>
          ))}
        </div>
      )}

      {/* 图区 */}
      <div className="flex-1 min-h-0 relative">
        {(drillLoading || topLoading) && isEmpty && (
          <div className="absolute inset-0 z-10 flex items-center justify-center bg-white/60 backdrop-blur-xs">
            <Loader2 className="w-5 h-5 text-purple-500 animate-spin" />
          </div>
        )}
        {isEmpty && !drillLoading && !topLoading ? (
          <div className="w-full h-full flex items-center justify-center text-xs text-slate-400 bg-slate-50/60 rounded-2xl border border-dashed border-slate-200">
            {drillName ? '该板块暂无成分数据' : '暂无热力图数据'}
          </div>
        ) : (
          <SectorHeatmapChart
            data={chartData}
            height="100%"
            valueLabel={drillName ? '总市值' : '市值权重规模'}
            renderKey={renderKey}
            onItemClick={drillName ? handleDrillClick : handleTopClick}
            colorOf={(item) => heatmapCellColor(item, colorMode, directionH)}
            labelValueOf={(item) => heatmapLabelValue(item, colorMode, directionH)}
            tooltipExtraOf={(item) => {
              const it = item as {
                pct_period?: number | null;
                flow_net?: number | null;
                flow_daily?: number | null;
                flow_accel?: number | null;
                flow_ratio_accel?: number | null;
                price_accel?: number | null;
                breadth_flow?: number | null;
                breadth_flow_chg?: number | null;
                pct_above_ma20?: number | null;
                pct_above_ma20_chg?: number | null;
                sentiment_score?: number | null;
                direction?: Record<string, HeatmapDirectionCell> | null;
              };
              const parts: string[] = [];
              if (periodDays > 1 && typeof it.pct_period === 'number') {
                parts.push(`<span style="color:#cbd5e1;">${periodDays}日涨幅: <b>${it.pct_period >= 0 ? '+' : ''}${it.pct_period.toFixed(2)}%</b></span>`);
              }
              if (typeof it.flow_net === 'number') {
                const c = it.flow_net >= 0 ? '#f87171' : '#34d399';
                parts.push(`<span style="color:#cbd5e1;">${periodDays}日净额: <b style="color:${c};">${it.flow_net >= 0 ? '+' : ''}${it.flow_net.toFixed(2)}亿</b></span>`);
              }
              if (typeof it.flow_daily === 'number') {
                parts.push(`<span style="color:#cbd5e1;">日均: ${it.flow_daily.toFixed(2)}亿/日</span>`);
              }
              if (typeof it.flow_accel === 'number') {
                const c = it.flow_accel >= 0 ? '#f87171' : '#34d399';
                parts.push(`<span style="color:#cbd5e1;">加速度: <b style="color:${c};">${it.flow_accel >= 0 ? '+' : ''}${it.flow_accel.toFixed(2)}亿/日</b></span>`);
              }
              if (colorMode === 'vp') {
                const f = it.flow_accel;
                const p = it.price_accel;
                parts.push(
                  `<span style="color:#fbbf24;">量价共振：<b>${vpLabel(f, p)}</b></span>`,
                );
                parts.push(
                  `<span style="color:#94a3b8;">资金加速 ${f == null ? '—' : `${f > 0 ? '+' : ''}${f.toFixed(2)}亿/日`}` +
                    ` · 价格加速 ${p == null ? '—' : `${p > 0 ? '+' : ''}${p.toFixed(2)}pp/日`}</span>`,
                );
                parts.push('<span style="color:#64748b;">描述性状态；历史统计不支持把它当方向预测</span>');
              }
              if (colorMode === 'sentiment') {
                parts.push(
                  `<span style="color:#fbbf24;">情绪热度 T+${periodDays}：<b>${it.sentiment_score == null ? '—' : `${(it.sentiment_score * 100).toFixed(0)} / 100`}</b></span>`,
                );
                parts.push(
                  `<span style="color:#94a3b8;">价格广度(MA20) ${it.pct_above_ma20 == null ? '—' : `${(it.pct_above_ma20 * 100).toFixed(0)}%`}` +
                    ` · 价格广度变化 ${it.pct_above_ma20_chg == null ? '—' : `${it.pct_above_ma20_chg > 0 ? '+' : ''}${(it.pct_above_ma20_chg * 100).toFixed(1)}pp`}</span>`,
                );
                parts.push(
                  `<span style="color:#94a3b8;">资金广度 ${it.breadth_flow == null ? '—' : `${(it.breadth_flow * 100).toFixed(0)}%`}` +
                    ` · 资金广度变化 ${it.breadth_flow_chg == null ? '—' : `${it.breadth_flow_chg > 0 ? '+' : ''}${(it.breadth_flow_chg * 100).toFixed(1)}pp`}` +
                    ` · 净额比加速 ${it.flow_ratio_accel == null ? '—' : `${it.flow_ratio_accel > 0 ? '+' : ''}${(it.flow_ratio_accel * 100).toFixed(2)}pp`}</span>`,
                );
                parts.push('<span style="color:#64748b;">热度=资金广度/广度变化/周期涨幅/资金加速的截面分位合成，不是概率</span>');
              }
              if (colorMode === 'direction') {
                const cell = it.direction?.[String(directionH)];
                if (!cell) {
                  parts.push('<span style="color:#94a3b8;">方向概率：该板块状态格样本不足（—）</span>');
                } else {
                  const stateLabel: Record<string, string> = {
                    extreme: '极端压力',
                    pressure: '承压',
                    neutral: '中性',
                    strong: '强势',
                    overheat: '过热',
                  };
                  const mean =
                    cell.mean == null ? '—' : `${cell.mean > 0 ? '+' : ''}${(cell.mean * 100).toFixed(2)}%`;
                  parts.push(
                    `<span style="color:#c4b5fd;">方向 T+${directionH}：跑赢大盘 <b>${(cell.rel_prob * 100).toFixed(1)}%</b>` +
                      ` · 绝对上涨 <b>${(cell.prob * 100).toFixed(1)}%</b> · 平均超额 <b>${mean}</b></span>`,
                  );
                  parts.push(
                    `<span style="color:#94a3b8;">大盘先验 ${(cell.market_prob * 100).toFixed(1)}% · 历史样本 n=${cell.n ?? '—'}` +
                      ` · 日期级胜率 ${cell.date_win == null ? '—' : `${(cell.date_win * 100).toFixed(0)}%`}` +
                      `（${cell.n_dates ?? '—'} 个日期）</span>`,
                  );
                  parts.push(
                    `<span style="color:#94a3b8;">状态：${stateLabel[cell.state.stress] ?? cell.state.stress}` +
                      ` / 资金加速 ${cell.state.flow} / 5日涨幅 ${cell.state.price}</span>`,
                  );
                }
              }
              return parts.join('<br/>');
            }}
          />
        )}
      </div>
    </div>
  );
};
