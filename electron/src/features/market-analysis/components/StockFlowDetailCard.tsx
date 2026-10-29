/**
 * 个股资金流详情卡（细案 v2 §5，R3 核心）。
 *
 * - 20 日逐日五档序列（C 级：红=净流入、绿=净流出）；
 * - 所属板块归因：板块 chips + 该股在各板块的贡献排名，点 chip 回板块视角
 *   （=「个股资金流返回板块」）；
 * - 因子板共振态：合成分 × 模型分位象限（个体预测锚；板缺失不渲染、陈旧显形
 *   不冒充当日）；行业 chip 一键回板块工作台；
 * - 个股层不出现资金流预测徽章——实测否决（IC≤0.01），页内明示原因（meta.disclosure）。
 */

import React, { useEffect, useRef, useState } from 'react';
import * as echarts from 'echarts';
import { Spin, Tooltip } from 'antd';
import { X, TrendingUp, ChevronRight } from 'lucide-react';
import {
  getFlowStock,
  fmtSeriesYi,
  fmtYi,
  fmtPct,
  fmtRatio,
  flowTextClass,
  MoneyFlowError,
  type FlowStockDetailResponse,
  type FlowStockFactor,
} from '../services/moneyFlowService';
import { quadChipCls, quadrantMeta } from '../factorBoardVocabulary';
import { NumText } from '../../market-analysis-shared/ui';
import { EvidenceBadge } from './EvidenceBadge';

const INFLOW_COLOR = '#e11d48';
const OUTFLOW_COLOR = '#059669';

export interface StockFlowDetailCardProps {
  symbol: string;
  date?: string;
  onClose: () => void;
  /** 点归因 chip → 回板块视角（sectorSet 为 null 时保持当前体系）。 */
  onNavigateSector: (name: string, sectorSet: string | null) => void;
  /** 取数到位回调（v7：面板据此把成分贡献榜定位到该股所属板块并选中其行）。 */
  onDetailLoaded?: (detail: FlowStockDetailResponse) => void;
}

const SeriesChart: React.FC<{ detail: FlowStockDetailResponse; height?: number }> = ({ detail, height = 168 }) => {
  const ref = useRef<HTMLDivElement>(null);
  const inst = useRef<echarts.ECharts | null>(null);

  useEffect(() => {
    if (!ref.current) return;
    if (!inst.current) inst.current = echarts.init(ref.current);
    const chart = inst.current;
    const dates = detail.series.map((p) => p.date.slice(5));
    const main = detail.series.map((p) => p.main ?? null);
    const close = detail.series.map((p) => p.close ?? null);
    const fwd5 = detail.series.map((p) => p.fwd_5d ?? null);
    const hasClose = close.some((c) => c !== null);
    const hasFwd = fwd5.some((v) => v !== null);
    chart.setOption(
      {
        grid: { left: 46, right: hasFwd ? 84 : hasClose ? 46 : 12, top: hasFwd ? 28 : 18, bottom: 20 },
        legend: hasFwd
          ? {
              top: 0,
              right: 58,
              itemWidth: 10,
              itemHeight: 6,
              textStyle: { fontSize: 9, color: '#64748b' },
              data: ['主力净额', '收盘价', '之后5日'],
            }
          : undefined,
        tooltip: {
          trigger: 'axis',
          formatter: (ps: any[]) => {
            const idx = ps?.[0]?.dataIndex ?? 0;
            const p = detail.series[idx];
            if (!p) return '';
            return (
              `<div style="font-size:11px;line-height:1.6">${p.date}<br/>` +
              `主力净额：<b style="color:${(p.main ?? 0) >= 0 ? INFLOW_COLOR : OUTFLOW_COLOR}">${fmtSeriesYi(p.main)}</b><br/>` +
              `超大 ${fmtSeriesYi(p.super)} · 大 ${fmtSeriesYi(p.large)}<br/>` +
              `中 ${fmtSeriesYi(p.medium)} · 小 ${fmtSeriesYi(p.small)}<br/>` +
              `净额比：${fmtRatio(p.net_ratio)} · 收盘 ${p.close ?? '—'}（${fmtPct(p.pct_change)}）<br/>` +
              `<span style="color:#7c3aed">之后：5日 ${fmtPct(p.fwd_5d)} · 10日 ${fmtPct(p.fwd_10d)} · 20日 ${fmtPct(p.fwd_20d)}</span></div>`
            );
          },
        },
        xAxis: {
          type: 'category',
          data: dates,
          axisLabel: { fontSize: 9, color: '#94a3b8' },
          axisTick: { show: false },
          axisLine: { lineStyle: { color: '#e2e8f0' } },
        },
        yAxis: [
          {
            type: 'value',
            name: '亿',
            nameTextStyle: { fontSize: 9, color: '#94a3b8' },
            axisLabel: { fontSize: 9, color: '#94a3b8', formatter: (v: number) => v.toFixed(1) },
            splitLine: { lineStyle: { color: '#f1f5f9' } },
          },
          ...(hasClose
            ? [
                {
                  type: 'value',
                  scale: true,
                  position: 'right',
                  axisLabel: { fontSize: 9, color: '#94a3b8' },
                  splitLine: { show: false },
                },
              ]
            : []),
          ...(hasFwd
            ? [
                {
                  type: 'value',
                  name: '%',
                  nameTextStyle: { fontSize: 9, color: '#a855f7' },
                  position: 'right',
                  offset: 42,
                  axisLabel: { fontSize: 9, color: '#a855f7', formatter: (v: number) => `${v.toFixed(0)}%` },
                  splitLine: { show: false },
                },
              ]
            : []),
        ],
        series: [
          {
            name: '主力净额',
            type: 'bar',
            data: main.map((v) => ({
              value: v,
              itemStyle: { color: (v ?? 0) >= 0 ? INFLOW_COLOR : OUTFLOW_COLOR, borderRadius: 2 },
            })),
            barMaxWidth: 14,
          },
          ...(hasClose
            ? [
                {
                  name: '收盘价',
                  type: 'line',
                  yAxisIndex: 1,
                  data: close,
                  symbolSize: 4,
                  connectNulls: true,
                  lineStyle: { color: '#6366f1', width: 1.5 },
                  itemStyle: { color: '#6366f1' },
                },
              ]
            : []),
          ...(hasFwd
            ? [
                {
                  name: '之后5日',
                  type: 'line',
                  yAxisIndex: 2,
                  data: fwd5,
                  symbolSize: 3,
                  connectNulls: false,
                  lineStyle: { color: '#a855f7', width: 1.2, type: 'dashed' },
                  itemStyle: { color: '#a855f7' },
                },
              ]
            : []),
        ],
      },
      { notMerge: true },
    );
    const ro = new ResizeObserver(() => chart.resize());
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, [detail]);

  useEffect(
    () => () => {
      inst.current?.dispose();
      inst.current = null;
    },
    [],
  );

  return <div ref={ref} className="w-full min-w-0" style={{ height }} />;
};

const WindowCard: React.FC<{ label: string; w: FlowStockDetailResponse['windows']['1d'] }> = ({ label, w }) => (
  <div className="flex-1 min-w-0 bg-slate-50/80 rounded-xl px-2.5 py-2 border border-slate-100">
    <div className="text-[10px] font-bold text-slate-400">{label}</div>
    {w ? (
      <>
        <div className={`text-[13px] font-extrabold font-mono ${flowTextClass(w.net_main)}`}>{fmtYi(w.net_main)}</div>
        <div className="text-[10px] font-mono text-slate-400">净额比 {fmtRatio(w.net_ratio)}</div>
      </>
    ) : (
      <div className="text-[13px] font-extrabold font-mono text-slate-300">—</div>
    )}
  </div>
);

/** 因子板共振态（个体预测锚）：行业 chip 一键回板块工作台 + 合成分/模型分位/象限。
 *
 * 后端约定见 moneyFlowService.FlowStockFactor：available=false = 板在但未收录；
 * stale = 板决策日落后资金流数据日超 3 天（只显形不冒充当日）。缺失一律「—」。
 */
const FactorResonanceSection: React.FC<{
  factor: FlowStockFactor;
  onNavigateSector: (name: string, sectorSet: string | null) => void;
}> = ({ factor, onNavigateSector }) => {
  const q = quadrantMeta(factor.quadrant);
  const sectorL2 = factor.sector_l2;
  const staleTitle =
    factor.stale_days === null
      ? '板决策日无法与资金流数据日比较（按陈旧显形，不冒充当日）'
      : `板决策日距资金流数据日 ${factor.stale_days} 天（超过 3 天视为陈旧）：只显形，不冒充当日`;
  return (
    <div className="flex flex-col gap-1.5 pt-1.5 border-t border-slate-100">
      <div className="flex items-center gap-1.5">
        <span
          className="text-[10px] font-bold text-slate-400 cursor-help"
          title="个股层资金流实测不具预测力；个体方向锚 = 因子板合成分 × 模型分位象限（个股层不设资金流预测徽章）"
        >
          因子板共振态
        </span>
        {factor.decision_date && (
          <span className="text-[10px] font-mono text-slate-400">决策日 {factor.decision_date}</span>
        )}
        {factor.stale && (
          <Tooltip title={staleTitle}>
            <span className="text-[9px] font-bold text-amber-700 bg-amber-50 border border-amber-200/70 rounded px-1 py-px cursor-help">
              陈旧
            </span>
          </Tooltip>
        )}
      </div>
      {factor.available ? (
        <div className="flex flex-wrap items-center gap-1.5">
          {sectorL2 && (
            <button
              onClick={() => sectorL2 && onNavigateSector(sectorL2, 'industry2')}
              className="inline-flex items-center gap-0.5 px-2 py-0.5 rounded-full text-[10px] font-bold bg-purple-50 text-purple-700 border border-purple-200/80 hover:bg-purple-100 transition-colors cursor-pointer"
              title="所属行业（二级，因子板口径）：点击回板块工作台"
            >
              {sectorL2}
              <ChevronRight className="w-2.5 h-2.5 opacity-60" />
            </button>
          )}
          <span
            className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] bg-slate-50 text-slate-600 border border-slate-200/80"
            title="合成分（因子概率板口径，缺失显示 —）"
          >
            合成分 <NumText value={factor.composite} digits={3} />
          </span>
          <span
            className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] bg-slate-50 text-slate-600 border border-slate-200/80"
            title="模型分位 = 同批推理截面 rank_pct（分母为该批标的，非全市场，跨模型不可比）"
          >
            模型分位 <span className="font-mono">{factor.model_rank === null ? '—' : factor.model_rank.toFixed(2)}</span>
          </span>
          <Tooltip title={q.hint}>
            <span className={`min-w-7 text-center text-[10px] font-extrabold font-mono rounded border px-0.5 py-px cursor-help ${q.cls}`}>
              {q.label}
            </span>
          </Tooltip>
        </div>
      ) : (
        <span className="text-[11px] text-slate-400">
          该股未收录于最新因子板截面{factor.decision_date ? `（决策日 ${factor.decision_date}）` : ''}
        </span>
      )}
    </div>
  );
};

export const StockFlowDetailCard: React.FC<StockFlowDetailCardProps> = ({
  symbol,
  date,
  onClose,
  onNavigateSector,
  onDetailLoaded,
}) => {
  const [detail, setDetail] = useState<FlowStockDetailResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const reqSeq = useRef(0);
  const factor = detail?.factor ?? null;
  // 回调 ref 镜像：取数 effect 不因回调身份变化而重取（父组件每次渲染都会传新闭包）
  const onDetailLoadedRef = useRef(onDetailLoaded);
  useEffect(() => {
    onDetailLoadedRef.current = onDetailLoaded;
  });

  useEffect(() => {
    const seq = ++reqSeq.current;
    if (!symbol) return;
    setLoading(true);
    setError(null);
    getFlowStock(symbol, { days: 20, date })
      .then((d) => {
        if (seq !== reqSeq.current) return;
        setDetail(d);
        onDetailLoadedRef.current?.(d);
      })
      .catch((e: unknown) => {
        if (seq !== reqSeq.current) return;
        setDetail(null);
        if (e instanceof MoneyFlowError && e.status === 404) {
          setError('该代码在 L2 资金流分区中查无数据（可能为北交所/新代码或数据未覆盖）');
        } else {
          setError('个股资金流取数失败，请稍后重试');
        }
      })
      .finally(() => {
        if (seq === reqSeq.current) setLoading(false);
      });
  }, [symbol, date]);

  return (
    <div className="bg-white/95 backdrop-blur-md rounded-2xl border border-purple-100/80 shadow-md shadow-purple-500/5 flex flex-col min-w-0">
      <div className="flex items-center justify-between px-4 pt-3.5 pb-2 border-b border-slate-100">
        <div className="flex items-center gap-2 min-w-0">
          <TrendingUp className="w-3.5 h-3.5 text-purple-600 shrink-0" />
          <span className="text-xs font-extrabold text-slate-800 truncate">
            {detail?.name || '个股资金流详情'}
          </span>
          <span className="font-mono text-[11px] text-purple-700 bg-purple-50 px-1.5 py-0.5 rounded border border-purple-200 shrink-0">
            {symbol}
          </span>
          {detail && (
            <span className={`text-[12px] font-extrabold font-mono shrink-0 ${flowTextClass(detail.pct_change)}`}>
              {detail.close ?? '—'}（{fmtPct(detail.pct_change)}）
            </span>
          )}
        </div>
        <button
          onClick={onClose}
          className="text-slate-400 hover:text-slate-700 p-1 rounded-lg hover:bg-slate-100 shrink-0"
          aria-label="关闭个股详情"
        >
          <X className="w-3.5 h-3.5" />
        </button>
      </div>

      {loading && (
        <div className="flex items-center justify-center py-10">
          <Spin size="small" />
        </div>
      )}

      {!loading && error && <div className="px-4 py-6 text-xs text-slate-400 text-center">{error}</div>}

      {!loading && !error && detail && (
        <div className="flex flex-col gap-2.5 p-3.5">
          <div className="flex items-center gap-1.5">
            <span className="text-[10px] font-mono text-slate-400">{detail.trade_date} · 单位：亿元</span>
          </div>
          <SeriesChart detail={detail} />
          {detail.series.some((p) => p.fwd_5d !== null && p.fwd_5d !== undefined) && (
            <p className="text-[9px] text-slate-400 -mt-1">
              紫色虚线＝之后 5 日涨跌（close(t)→close(t+5)，仅历史回看；最新几天没有未来数据）
            </p>
          )}

          <div className="grid grid-cols-3 sm:grid-cols-6 gap-2">
            <WindowCard label="当日" w={detail.windows['1d']} />
            <WindowCard label="2 日" w={detail.windows['2d']} />
            <WindowCard label="3 日" w={detail.windows['3d']} />
            <WindowCard label="5 日" w={detail.windows['5d']} />
            <WindowCard label="10 日" w={detail.windows['10d']} />
            <WindowCard label="20 日" w={detail.windows['20d']} />
          </div>
          {detail.speed && (
            <div className="rounded-xl bg-amber-50/60 border border-amber-100 px-2.5 py-1.5 text-[10px] text-amber-800 flex items-center gap-2 flex-wrap">
              <span className="font-extrabold">资金流速率</span>
              <span>当前日均 {fmtYi(detail.speed.net_daily)}/日</span>
              <span>· 前一窗口日均 {fmtYi(detail.speed.prev_net_daily)}/日</span>
              <span className="font-mono font-extrabold">
                加速度 {detail.speed.accel === null ? '—' : `${detail.speed.accel >= 0 ? '+' : ''}${(detail.speed.accel / 1e8).toFixed(2)} 亿/日`}
              </span>
            </div>
          )}

          {factor && <FactorResonanceSection factor={factor} onNavigateSector={onNavigateSector} />}

          <div className="flex flex-col gap-1.5 pt-1.5 border-t border-slate-100">
            <span className="text-[10px] font-bold text-slate-400">所属板块归因（点击回板块视角）</span>
            <div className="flex flex-wrap gap-1.5 max-h-[168px] overflow-y-auto custom-scrollbar pr-1">
              {detail.attribution.length === 0 && (
                <span className="text-[11px] text-slate-400">无板块归属数据</span>
              )}
              {detail.attribution.map((a) => (
                <Tooltip
                  key={`${a.type}-${a.name}`}
                  title={`板块窗口净额 ${fmtYi(a.net_main)} · 该股贡献占比 ${a.share === null ? '—' : `${(a.share * 100).toFixed(1)}%`}`}
                >
                  <button
                    onClick={() => onNavigateSector(a.name, a.sector_set)}
                    className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-bold transition-colors cursor-pointer ${
                      a.streak !== null && a.streak !== undefined && a.streak >= 2
                        ? 'bg-amber-50 text-amber-700 border border-amber-200/80 hover:bg-amber-100'
                        : 'bg-slate-50 text-slate-600 border border-slate-200/80 hover:bg-slate-100'
                    }`}
                  >
                    <span className="text-slate-400">{a.type.replace('板块', '').replace('(', '').replace(')', '')}</span>
                    <span>{a.name}</span>
                    <span className="font-mono text-slate-400">
                      {a.inner_rank}/{a.inner_total}
                    </span>
                    <EvidenceBadge streak={a.streak} badge={a.evidence} />
                    <ChevronRight className="w-2.5 h-2.5 opacity-60" />
                  </button>
                </Tooltip>
              ))}
            </div>
          </div>

          <p className="text-[10px] text-slate-400 leading-snug border-t border-slate-100 pt-1.5">
            {detail.meta.disclosure?.[0] ??
              '个股层资金流实测不具预测力，个体预测以因子概率板与模型为准。'}
          </p>
        </div>
      )}
    </div>
  );
};
