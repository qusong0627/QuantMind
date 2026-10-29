/**
 * 板块工作台 v5：拆为两个独立组件，供左列「一窗两签」窗口与右列明细卡复用。
 *
 * - `SectorBoardList`：板块榜 · 延续状态优先（板块榜签，填充式列表：主标题由外层
 *   窗口提供，本组件只留一行排序口径提示）。行 = 排位 + 名称 +「连N」徽章（hover
 *   证据卡）+ 双向净额条 + 净额比 + 净额 + 涨幅 + 成分数。
 * - `SectorDetailPanel`：板块明细（窗口汇总 + 20 日趋势 + 成分贡献榜）。v5 起挂
 *   工作台右列，成分表列宽放宽（名称不挤压、近 5 日逐日涨跌幅列加宽），窄屏退化为横向滚动。
 */

import React, { useEffect, useMemo, useRef, useState } from 'react';
import * as echarts from 'echarts';
import { Spin } from 'antd';
import { Layers, ChevronRight } from 'lucide-react';
import {
  fmtYi,
  fmtPct,
  fmtRatio,
  flowTextClass,
  type FlowSectorDetailResponse,
  type FlowSectorRow,
} from '../services/moneyFlowService';
import { EvidenceBadge } from './EvidenceBadge';

const INFLOW_COLOR = '#e11d48';
const OUTFLOW_COLOR = '#059669';

/** 板块榜滚动区高度（v5：与右列明细卡列高相当；超出滚动，卡高不随板块数膨胀）。 */
const BOARD_LIST_MAX_HEIGHT = 560;

/** 双向净额条：以 0 为轴心，红向右（净流入）、绿向左（净流出）。 */
const BipolarBar: React.FC<{ value: number; maxAbs: number; width?: string }> = ({ value, maxAbs, width = 'w-20' }) => {
  const pct = maxAbs > 0 ? Math.min(50, (Math.abs(value) / maxAbs) * 50) : 0;
  return (
    <div className={`${width} h-2 bg-slate-100 rounded-full relative overflow-hidden shrink-0`}>
      <div className="absolute inset-y-0 left-1/2 w-px bg-slate-300" />
      <div
        className={`absolute inset-y-0 rounded-full ${value >= 0 ? 'bg-rose-500/80' : 'bg-emerald-500/80'}`}
        style={
          value >= 0
            ? { left: '50%', width: `${pct}%` }
            : { right: '50%', width: `${pct}%` }
        }
      />
    </div>
  );
};

const TrendChart: React.FC<{ trend: FlowSectorDetailResponse['trend']; height?: number }> = ({ trend, height = 150 }) => {
  const ref = useRef<HTMLDivElement>(null);
  const inst = useRef<echarts.ECharts | null>(null);

  useEffect(() => {
    if (!ref.current) return;
    if (!inst.current) inst.current = echarts.init(ref.current);
    const chart = inst.current;
    chart.setOption(
      {
        grid: { left: 40, right: 40, top: 16, bottom: 18 },
        tooltip: {
          trigger: 'axis',
          formatter: (ps: any[]) => {
            const idx = ps?.[0]?.dataIndex ?? 0;
            const p = trend[idx];
            if (!p) return '';
            return (
              `<div style="font-size:11px"><b>${p.date}</b><br/>主力净额：` +
              `<b style="color:${p.net_main >= 0 ? INFLOW_COLOR : OUTFLOW_COLOR}">${p.net_main >= 0 ? '+' : ''}${p.net_main.toFixed(2)} 亿</b><br/>` +
              `净额比：${p.net_ratio === null ? '—' : `${(p.net_ratio * 100).toFixed(2)}%`}</div>`
            );
          },
        },
        xAxis: {
          type: 'category',
          data: trend.map((t) => t.date.slice(5)),
          axisLabel: { fontSize: 9, color: '#94a3b8' },
          axisTick: { show: false },
          axisLine: { lineStyle: { color: '#e2e8f0' } },
        },
        yAxis: [
          {
            type: 'value',
            name: '亿',
            nameTextStyle: { fontSize: 9, color: '#94a3b8' },
            axisLabel: { fontSize: 9, color: '#94a3b8', formatter: (v: number) => v.toFixed(0) },
            splitLine: { lineStyle: { color: '#f1f5f9' } },
          },
          {
            type: 'value',
            axisLabel: { show: false },
            splitLine: { show: false },
          },
        ],
        series: [
          {
            name: '主力净额',
            type: 'bar',
            data: trend.map((t) => ({
              value: t.net_main,
              itemStyle: { color: t.net_main >= 0 ? INFLOW_COLOR : OUTFLOW_COLOR, borderRadius: 2 },
            })),
            barMaxWidth: 12,
          },
          {
            name: '净额比',
            type: 'line',
            yAxisIndex: 1,
            data: trend.map((t) => t.net_ratio),
            symbolSize: 3,
            connectNulls: true,
            lineStyle: { color: '#6366f1', width: 1.2, opacity: 0.8 },
            itemStyle: { color: '#6366f1' },
          },
        ],
      },
      { notMerge: true },
    );
    const ro = new ResizeObserver(() => chart.resize());
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, [trend]);

  useEffect(
    () => () => {
      inst.current?.dispose();
      inst.current = null;
    },
    [],
  );

  return <div ref={ref} className="w-full min-w-0" style={{ height }} />;
};

// ── 板块榜（板块榜签；无外框，由左列窗口提供卡壳与切换器） ─────────────

/** 板块榜行（两行制：窄栏下保住板块名；首行 = 名 + 徽章 + 净额，次行 = 净额条 + 比/成分/涨幅）。 */
const MasterRow: React.FC<{
  row: FlowSectorRow;
  rank: number;
  maxAbs: number;
  active: boolean;
  onClick: () => void;
}> = ({ row, rank, maxAbs, active, onClick }) => (
  <button
    onClick={onClick}
    title={`板块榜 · ${row.name} · 点击联动桑基与右侧明细`}
    className={`w-full flex flex-col gap-1 px-2.5 py-1.5 rounded-xl text-left transition-colors cursor-pointer ${
      active ? 'bg-purple-50 border border-purple-200/80 shadow-2xs' : 'hover:bg-slate-50 border border-transparent'
    }`}
  >
    <span className="w-full flex items-center gap-2 min-w-0">
      <span className={`w-6 text-[10px] font-mono font-bold shrink-0 ${rank <= 3 ? 'text-purple-600' : 'text-slate-400'}`}>
        {rank}
      </span>
      <span className="flex-1 min-w-0 text-[12px] font-extrabold text-slate-700 truncate">{row.name}</span>
      <EvidenceBadge streak={row.streak} badge={row.evidence} />
      <span className={`w-[82px] text-right text-[12px] font-mono font-extrabold shrink-0 ${flowTextClass(row.net_main)}`}>
        {fmtYi(row.net_main)}
      </span>
    </span>
    <span className="w-full flex items-center gap-2 pl-8 min-w-0">
      <span className="flex-1 min-w-0 flex items-center">
        <BipolarBar value={row.net_main} maxAbs={maxAbs} width="w-full" />
      </span>
      <span className="text-[10px] font-mono text-slate-400 shrink-0">比 {fmtRatio(row.net_ratio)}</span>
      <span className="text-[10px] font-mono text-slate-400 shrink-0">{row.members} 只</span>
      <span className={`w-[58px] text-right text-[11px] font-mono shrink-0 ${flowTextClass(row.pct_change)}`}>
        {fmtPct(row.pct_change)}
      </span>
      <ChevronRight className={`w-3 h-3 shrink-0 ${active ? 'text-purple-500' : 'text-slate-300'}`} />
    </span>
  </button>
);

export interface SectorBoardListProps {
  rows: FlowSectorRow[];
  selectedSector: string | null;
  onSelectSector: (name: string) => void;
}

export const SectorBoardList: React.FC<SectorBoardListProps> = ({
  rows,
  selectedSector,
  onSelectSector,
}) => {
  const maxAbs = useMemo(() => Math.max(1, ...rows.map((r) => Math.abs(r.net_main))), [rows]);
  // 「连N」证据只覆盖行业二级：其他体系（行业一级/概念）行内无 streak → 如实改口径
  const hasStreakEvidence = useMemo(
    () => rows.some((r) => r.streak !== null && r.streak !== undefined),
    [rows],
  );
  return (
    <div className="flex flex-col gap-2 min-w-0">
      {/* ── 口径一行（窗口主标题「板块榜」与切换器由父级提供） ── */}
      <div className="flex flex-wrap items-center justify-between gap-2 min-h-[18px]">
        <span className="text-[10px] font-bold text-slate-500 flex items-center gap-1.5">
          <Layers className="w-3 h-3 text-purple-500" />
          {hasStreakEvidence ? '延续状态优先 · 连N 命中在前 → 净额' : '窗口净额优先'}
        </span>
        <span
          className="text-[10px] font-mono text-slate-400"
          title={hasStreakEvidence ? undefined : '连续净流入证据仅按「行业二级」测量——该体系按窗口净额排序'}
        >
          {hasStreakEvidence ? '连N 徽章悬停看条件超额' : rows.length > 0 ? '无「连N」证据（仅行业二级测量）' : ''}
        </span>
      </div>
      <div className="flex flex-col gap-1 overflow-y-auto custom-scrollbar pr-0.5" style={{ maxHeight: BOARD_LIST_MAX_HEIGHT }}>
        {rows.length === 0 && <div className="px-3 py-6 text-center text-xs text-slate-400">暂无板块数据</div>}
        {rows.map((r, i) => (
          <MasterRow
            key={r.name}
            row={r}
            rank={i + 1}
            maxAbs={maxAbs}
            active={selectedSector === r.name}
            onClick={() => onSelectSector(r.name)}
          />
        ))}
      </div>
    </div>
  );
};

// ── 板块明细（全宽卡；成分贡献榜放宽列宽） ─────────────────────────────

export interface SectorDetailPanelProps {
  selectedSector: string | null;
  detail: FlowSectorDetailResponse | null;
  detailLoading: boolean;
  detailError: string | null;
  /** 每次点击板块入口（含同名再点=重取）递增——触发一次短暂高亮，给出可见反馈。 */
  refreshTick?: number;
  onSelectStock: (symbol: string, name: string) => void;
  /** 选中行个股（=当前个股卡个股）：行常驻高亮 + `aria-current`，出现时自动滚入可视区。 */
  highlightSymbol?: string | null;
}

export const SectorDetailPanel: React.FC<SectorDetailPanelProps> = ({
  selectedSector,
  detail,
  detailLoading,
  detailError,
  refreshTick = 0,
  onSelectStock,
  highlightSymbol = null,
}) => {
  const detailMaxAbs = useMemo(
    () => (detail ? Math.max(1, ...detail.members.map((m) => Math.abs(m.net_main))) : 1),
    [detail],
  );
  // 点击反馈闪光：refreshTick 每变一次亮 600ms（首帧不亮），同名再点也有可见响应
  const [flash, setFlash] = useState(false);
  const flashFirst = useRef(true);
  useEffect(() => {
    if (flashFirst.current) {
      flashFirst.current = false;
      return;
    }
    setFlash(true);
    const t = setTimeout(() => setFlash(false), 600);
    return () => clearTimeout(t);
  }, [refreshTick]);
  // 近 N 日涨跌幅列（升序统一日期组，取自任一成分行的 recent）
  const recentDates = useMemo(() => {
    const m = detail?.members.find((x) => x.recent && x.recent.length > 0);
    return (m?.recent ?? []).map((p) => p.date);
  }, [detail]);
  // v7 选中行：检索个股定位到本板块、或板块跳转自动下钻首位成分时，高亮行并滚入可视区
  const activeRowRef = useRef<HTMLButtonElement | null>(null);
  useEffect(() => {
    if (highlightSymbol) activeRowRef.current?.scrollIntoView({ block: 'nearest' });
  }, [highlightSymbol, detail]);

  return (
    <div
      className={`bg-white/95 backdrop-blur-md rounded-2xl border border-purple-100/80 shadow-md shadow-purple-500/5 flex flex-col min-w-0 transition-shadow duration-300 ${
        flash ? 'ring-2 ring-purple-300/80' : 'ring-0'
      }`}
    >
      {!selectedSector && (
        <div className="flex-1 flex items-center justify-center py-16 text-xs text-slate-400">
          点击「板块榜」或「延续命中榜」查看明细与成分贡献；点成分股下钻个股资金流
        </div>
      )}
      {selectedSector && detailLoading && (
        <div className="flex-1 flex items-center justify-center py-16">
          <Spin size="small" />
        </div>
      )}
      {selectedSector && !detailLoading && detailError && (
        <div className="flex-1 flex items-center justify-center py-16 text-xs text-slate-400">{detailError}</div>
      )}
      {selectedSector && !detailLoading && !detailError && detail && (
        <div className="flex flex-col gap-3 p-3.5 min-w-0">
          <div className="flex items-center justify-between gap-2">
            <div className="flex items-center gap-2 min-w-0">
              <span className="text-sm font-extrabold text-slate-900 truncate">{detail.name}</span>
              <span className="text-[10px] font-mono text-slate-400 shrink-0">
                {detail.sector_set_label} · {detail.trade_date} · {detail.summary.members} 只（有资金流 {detail.summary.data_symbols}）
              </span>
            </div>
            <EvidenceBadge streak={detail.summary.streak} badge={detail.summary.evidence} size="md" />
          </div>

          <div className="grid grid-cols-2 lg:grid-cols-5 gap-2">
            <div className="bg-slate-50/80 rounded-xl px-3 py-2 border border-slate-100">
              <div className="text-[10px] font-bold text-slate-400">窗口主力净额</div>
              <div className={`text-sm font-extrabold font-mono ${flowTextClass(detail.summary.net_main)}`}>
                {fmtYi(detail.summary.net_main)}
              </div>
            </div>
            <div className="bg-slate-50/80 rounded-xl px-3 py-2 border border-slate-100">
              <div className="text-[10px] font-bold text-slate-400">净额比</div>
              <div className="text-sm font-extrabold font-mono text-purple-700">{fmtRatio(detail.summary.net_ratio)}</div>
            </div>
            <div className="bg-slate-50/80 rounded-xl px-3 py-2 border border-slate-100">
              <div className="text-[10px] font-bold text-slate-400">成分均涨幅</div>
              <div className={`text-sm font-extrabold font-mono ${flowTextClass(detail.summary.pct_change)}`}>
                {fmtPct(detail.summary.pct_change)}
              </div>
            </div>
            <div className="bg-slate-50/80 rounded-xl px-3 py-2 border border-slate-100">
              <div className="text-[10px] font-bold text-slate-400">主力广度</div>
              <div className="text-sm font-extrabold font-mono text-slate-700">
                {detail.summary.breadth === null ? '—' : `${(detail.summary.breadth * 100).toFixed(0)}%`}
              </div>
            </div>
            <div className="bg-amber-50/60 rounded-xl px-3 py-2 border border-amber-100">
              <div className="text-[10px] font-bold text-amber-700">资金流速率（日均）</div>
              <div className={`text-sm font-extrabold font-mono ${(detail.summary.accel ?? 0) >= 0 ? 'text-red-600' : 'text-emerald-600'}`}>
                {detail.summary.accel === null || detail.summary.accel === undefined
                  ? '—'
                  : `${detail.summary.accel >= 0 ? '+' : ''}${(detail.summary.accel / 1e8).toFixed(2)} 亿/日`}
              </div>
              <div className="text-[9px] text-amber-700/80 font-mono">
                当前 {fmtYi(detail.summary.net_daily)}/日 · 前 {fmtYi(detail.summary.prev_net_daily)}/日
              </div>
            </div>
          </div>

          <div>
            <div className="flex items-center justify-between mb-1">
              <span className="text-[10px] font-bold text-slate-400">20 日主力净额与净额比</span>
              <span className="text-[10px] font-mono text-slate-400">红=净流入 绿=净流出（亿）</span>
            </div>
            <TrendChart trend={detail.trend} />
          </div>

          <div className="flex flex-col gap-1 min-w-0">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="text-[10px] font-bold text-slate-400">成分贡献榜（窗口净额 · 点击下钻个股）</span>
              <span className="text-[10px] font-mono text-slate-400">
                {recentDates.length > 0
                  ? `前排按净额绝对值 · 右列近 ${recentDates.length} 日逐日与 20 日累计涨幅（%）`
                  : '前排为净额绝对值最大'}
              </span>
            </div>
            <div className="overflow-x-auto custom-scrollbar">
              <div className="min-w-[860px] flex flex-col gap-1">
                {recentDates.length > 0 && (
                  <div className="flex items-center gap-2 px-2 pb-1 border-b border-slate-100">
                    <span className="w-7 shrink-0" />
                    <span className="w-[118px] shrink-0 text-[10px] font-bold text-slate-400">成分</span>
                    <span className="w-[78px] shrink-0" />
                    <span className="w-20 shrink-0" />
                    <span className="w-[88px] text-right shrink-0 text-[10px] font-bold text-slate-400">窗口净额</span>
                    <span className="w-[54px] text-right shrink-0 text-[10px] font-bold text-slate-400">占比</span>
                    {recentDates.map((d) => (
                      <span key={d} className="w-[48px] text-right shrink-0 text-[10px] font-mono text-slate-400">
                        {d.slice(5)}
                      </span>
                    ))}
                    <span className="w-[68px] text-right shrink-0 text-[10px] font-bold text-slate-400">20日涨幅</span>
                  </div>
                )}
                <div className="overflow-y-auto custom-scrollbar" style={{ maxHeight: 340 }}>
                  {detail.members.map((m) => {
                    const active = m.symbol === highlightSymbol;
                    return (
                    <button
                      key={m.symbol}
                      ref={active ? activeRowRef : undefined}
                      aria-current={active ? 'true' : undefined}
                      onClick={() => onSelectStock(m.symbol, m.name)}
                      className={`w-full flex items-center gap-2 px-2 py-1.5 rounded-lg text-left transition-colors cursor-pointer ${
                        active ? 'bg-purple-50/90 ring-1 ring-inset ring-purple-300/70' : 'hover:bg-purple-50/70'
                      }`}
                    >
                      <span className="w-7 text-[10px] font-mono text-slate-400 shrink-0">#{m.inner_rank}</span>
                      <span className="w-[118px] min-w-0 text-[11px] font-bold text-slate-700 truncate shrink-0">{m.name || '—'}</span>
                      <span className="w-[78px] text-[10px] font-mono text-slate-400 shrink-0">{m.symbol}</span>
                      <BipolarBar value={m.net_main} maxAbs={detailMaxAbs} />
                      <span className={`w-[88px] text-right text-[12px] font-mono font-extrabold shrink-0 ${flowTextClass(m.net_main)}`}>
                        {fmtYi(m.net_main)}
                      </span>
                      <span className="w-[54px] text-right text-[10px] font-mono text-slate-400 shrink-0">
                        {m.share === null ? '—' : `${(m.share * 100).toFixed(1)}%`}
                      </span>
                      {recentDates.map((d) => {
                        const p = m.recent?.find((x) => x.date === d);
                        return (
                          <span
                            key={d}
                            title={`${d} 涨跌幅 ${fmtPct(p?.pct)}`}
                            className={`w-[48px] text-right text-[10px] font-mono shrink-0 ${
                              p?.pct == null ? 'text-slate-300' : flowTextClass(p.pct)
                            }`}
                          >
                            {p?.pct == null ? '—' : `${p.pct > 0 ? '+' : ''}${p.pct.toFixed(1)}%`}
                          </span>
                        );
                      })}
                      <span
                        title="近 20 交易日累计涨幅（官方逐日涨跌幅复利；有效日不足 10 日显 —）"
                        className={`w-[68px] text-right text-[11px] font-mono font-bold shrink-0 ${
                          m.pct_20d == null ? 'text-slate-300' : flowTextClass(m.pct_20d)
                        }`}
                      >
                        {m.pct_20d == null ? '—' : `${m.pct_20d > 0 ? '+' : ''}${m.pct_20d.toFixed(2)}%`}
                      </span>
                    </button>
                    );
                  })}
                </div>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};
