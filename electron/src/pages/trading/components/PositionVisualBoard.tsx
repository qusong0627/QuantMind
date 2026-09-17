/**
 * 持仓监控可视化面板（2026-09-17 重设计 · v2 加料）
 *
 * - PositionVisualBoard：单卡三层——
 *   ① KPI 行：总市值 / 持仓数 / 盈利·亏损家数 / 现金占比 / 浮动盈亏（涨红跌绿）
 *   ② 图表行：左「市值 Treemap」（块面积=市值、颜色=盈亏深浅）＋ 右「盈亏贡献 Top12 横向柱」
 *   ③ 持仓明细：市值占比条形列表（排序切换 市值/盈亏/比例 + 关键字过滤）
 * - ExecutionStrip：今日执行折叠条（默认一行摘要，点开列单）。
 */
import React, { useMemo, useState, useEffect } from 'react';
import ReactECharts from 'echarts-for-react';
import { Activity, ArrowDownUp, ChevronDown, ChevronUp, PieChart, Search } from 'lucide-react';
import type { NormalizedHolding, PositionSummary } from '../utils/positionMetrics';
import { getDeskToday } from '../../../features/desk/services/deskService';
import type { ExecutionItem } from '../../../features/desk/types';

const fmtMoney = (v: number | null | undefined): string =>
  v === null || v === undefined || Number.isNaN(Number(v))
    ? '—'
    : Number(v).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });

const fmtPct = (v: number | null | undefined, digits = 2): string =>
  v === null || v === undefined || Number.isNaN(Number(v)) ? '—' : `${(Number(v) * 100).toFixed(digits)}%`;

/** 个股盈亏比例：positionMetrics 已按百分数口径返回（9.8 = 9.8%），不要再 ×100 */
const fmtRowPct = (v: number | null | undefined): string =>
  v === null || v === undefined || Number.isNaN(Number(v)) ? '—' : `${Number(v).toFixed(2)}%`;

/** A 股口径：盈利红、亏损绿 */
const pnlTone = (v: number): string => (v > 0 ? 'text-red-600' : v < 0 ? 'text-emerald-600' : 'text-slate-500');
const barTone = (v: number): string => (v > 0 ? 'bg-red-400' : v < 0 ? 'bg-emerald-400' : 'bg-slate-300');

/** Treemap 颜色：涨红跌绿按 |pct| 分四档（0/2%/5%/10%），零涨跌灰 */
function tileColor(profit: number, pct: number): string {
  if (profit === 0) return '#cbd5e1';
  const a = Math.abs(pct);
  const pos = profit > 0;
  if (a >= 10) return pos ? '#b91c1c' : '#047857';
  if (a >= 5) return pos ? '#dc2626' : '#059669';
  if (a >= 2) return pos ? '#f87171' : '#34d399';
  return pos ? '#fecaca' : '#a7f3d0';
}

type SortKey = 'value' | 'profit' | 'pct';

export const PositionVisualBoard: React.FC<{
  holdings: NormalizedHolding[];
  summary: PositionSummary;
}> = ({ holdings, summary }) => {
  const [sortKey, setSortKey] = useState<SortKey>('value');
  const [q, setQ] = useState('');

  const totalPnl = useMemo(() => holdings.reduce((s, h) => s + (h.profit || 0), 0), [holdings]);
  const winCount = holdings.filter((h) => h.profit > 0).length;
  const loseCount = holdings.filter((h) => h.profit < 0).length;
  const base = summary.positionValue > 0 ? summary.positionValue : 1;

  const sorted = useMemo(() => {
    const arr = [...holdings].sort((a, b) => {
      if (sortKey === 'profit') return b.profit - a.profit;
      if (sortKey === 'pct') return b.profitPercent - a.profitPercent;
      return b.value - a.value;
    });
    const kw = q.trim().toLowerCase();
    return kw ? arr.filter((h) => (h.name || '').toLowerCase().includes(kw) || h.code.toLowerCase().includes(kw)) : arr;
  }, [holdings, sortKey, q]);

  // —— 图表①：市值 Treemap（面积=市值，颜色=盈亏） ——
  const treemapOption = useMemo(
    () => ({
      tooltip: {
        formatter: (info: { name: string; value: number }) => {
          const h = holdings.find((x) => (x.name || x.code) === info.name);
          if (!h) return info.name;
          return `${h.name}（${h.code}）<br/>市值 ${fmtMoney(h.value)} · 占比 ${((h.value / base) * 100).toFixed(1)}%<br/>盈亏 ${h.profit > 0 ? '+' : ''}${fmtMoney(h.profit)}（${fmtRowPct(h.profitPercent)}）`;
        },
      },
      series: [
        {
          type: 'treemap',
          roam: false,
          nodeClick: false,
          breadcrumb: { show: false },
          width: '100%',
          height: '100%',
          label: { show: true, fontSize: 10, color: '#fff', overflow: 'truncate' },
          upperLabel: { show: false },
          itemStyle: { borderColor: '#ffffff', borderWidth: 1.5, gapWidth: 1 },
          data: sorted
            .filter((h) => h.value > 0)
            .map((h) => ({
              name: h.name || h.code,
              value: Math.round(h.value),
              itemStyle: { color: tileColor(h.profit, h.profitPercent) },
            })),
        },
      ],
    }),
    [sorted, holdings, base],
  );

  // —— 图表②：盈亏贡献 Top12（横向柱，红正绿负） ——
  const contribOption = useMemo(() => {
    const top = [...holdings]
      .filter((h) => h.profit !== 0)
      .sort((a, b) => Math.abs(b.profit) - Math.abs(a.profit))
      .slice(0, 12)
      .sort((a, b) => a.profit - b.profit); // 从小到大 → 柱图自下而上
    return {
      grid: { left: 66, right: 18, top: 4, bottom: 4, containLabel: false },
      xAxis: {
        type: 'value',
        axisLabel: { fontSize: 9, color: '#94a3b8', formatter: (v: number) => (Math.abs(v) >= 10000 ? `${(v / 10000).toFixed(0)}万` : String(v)) },
        splitLine: { lineStyle: { color: '#f1f5f9' } },
      },
      yAxis: {
        type: 'category',
        data: top.map((h) => (h.name || h.code).slice(0, 6)),
        axisLabel: { fontSize: 9, color: '#64748b' },
        axisTick: { show: false },
        axisLine: { show: false },
      },
      tooltip: {
        formatter: (p: { dataIndex: number }) => {
          const h = top[p.dataIndex];
          return `${h.name}（${h.code}）<br/>盈亏 ${h.profit > 0 ? '+' : ''}${fmtMoney(h.profit)}（${fmtRowPct(h.profitPercent)}）`;
        },
      },
      series: [
        {
          type: 'bar',
          barWidth: 9,
          data: top.map((h) => ({ value: Math.round(h.profit), itemStyle: { color: h.profit > 0 ? '#dc2626' : '#059669', borderRadius: 3 } })),
        },
      ],
    };
  }, [holdings]);

  const SORTS: Array<{ key: SortKey; label: string }> = [
    { key: 'value', label: '市值' },
    { key: 'profit', label: '盈亏额' },
    { key: 'pct', label: '盈亏比例' },
  ];

  return (
    <div className="flex-1 min-h-0 rounded-2xl border border-gray-200 bg-white shadow-sm flex flex-col overflow-hidden">
      {/* ① KPI 行 */}
      <div className="shrink-0 flex flex-wrap items-center gap-2 px-4 py-2.5 border-b border-gray-100">
        <div className="flex items-center gap-2">
          <span className="w-7 h-7 rounded-lg bg-blue-50 text-blue-600 flex items-center justify-center">
            <PieChart className="w-4 h-4" />
          </span>
          <span className="text-sm font-bold text-slate-800">持仓监控</span>
        </div>
        <div className="ml-auto flex flex-wrap items-center gap-1.5 text-[11px] font-bold">
          <span className="rounded-full border border-slate-200 bg-slate-50 px-2.5 py-1 text-slate-600">
            总市值 <span className="font-mono text-slate-800">{fmtMoney(summary.positionValue)}</span>
          </span>
          <span className="rounded-full border border-slate-200 bg-slate-50 px-2.5 py-1 text-slate-600">
            持仓 <span className="font-mono text-slate-800">{holdings.length}</span> 只
          </span>
          <span className="rounded-full border border-red-100 bg-red-50/60 px-2.5 py-1 text-red-600">
            盈利 <span className="font-mono">{winCount}</span>
          </span>
          <span className="rounded-full border border-emerald-100 bg-emerald-50/60 px-2.5 py-1 text-emerald-600">
            亏损 <span className="font-mono">{loseCount}</span>
          </span>
          <span
            className="rounded-full border border-slate-200 bg-slate-50 px-2.5 py-1 text-slate-600"
            title={summary.cashRatio > 1.5 ? '现金/总资产 > 100%，账户口径数据异常，暂显 —' : undefined}
          >
            现金占比 <span className="font-mono text-slate-800">{summary.cashRatio > 1.5 ? '—' : fmtPct(summary.cashRatio)}</span>
          </span>
          <span
            className={`rounded-full border px-2.5 py-1 ${
              totalPnl > 0
                ? 'border-red-200 bg-red-50 text-red-600'
                : totalPnl < 0
                  ? 'border-emerald-200 bg-emerald-50 text-emerald-600'
                  : 'border-slate-200 bg-slate-50 text-slate-500'
            }`}
          >
            浮动盈亏 <span className="font-mono">{totalPnl > 0 ? '+' : ''}{fmtMoney(totalPnl)}</span>
          </span>
        </div>
      </div>

      {/* ② 图表行：市值 Treemap + 盈亏贡献 */}
      <div className="shrink-0 grid grid-cols-1 lg:grid-cols-2 gap-2 px-4 pt-2 pb-1 border-b border-slate-100">
        <div className="min-w-0">
          <div className="mb-0.5 text-[10px] font-bold text-slate-400" title="块面积=持仓市值，颜色=盈亏（红涨绿跌，越深幅度越大）">
            市值分布
          </div>
          <ReactECharts option={treemapOption} style={{ height: 150 }} notMerge />
        </div>
        <div className="min-w-0">
          <div className="mb-0.5 text-[10px] font-bold text-slate-400" title="盈亏金额绝对值 Top12（红=盈利、绿=亏损）">
            盈亏贡献 Top12
          </div>
          {holdings.some((h) => h.profit !== 0) ? (
            <ReactECharts option={contribOption} style={{ height: 150 }} notMerge />
          ) : (
            <div className="flex h-[150px] items-center justify-center text-[11px] text-slate-300">暂无盈亏数据</div>
          )}
        </div>
      </div>

      {/* ③ 明细工具行：排序 + 过滤 */}
      <div className="shrink-0 flex items-center gap-2 px-4 pt-2 pb-1">
        <div className="grid grid-cols-[minmax(0,1.3fr)_minmax(0,2.2fr)_minmax(0,1fr)_minmax(0,1.1fr)] items-center gap-3 flex-1 text-[10px] font-bold text-slate-400">
          <span>股票</span>
          <span title="条宽 = 个股市值 ÷ 持仓总市值（红涨绿跌）">市值占比</span>
          <span className="text-right">现价 / 成本</span>
          <span className="text-right">盈亏（金额 / 比例）</span>
        </div>
        <div className="flex shrink-0 items-center gap-1">
          <div className="relative">
            <Search className="absolute left-2 top-1/2 h-3 w-3 -translate-y-1/2 text-slate-300" />
            <input
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="代码/名称"
              className="w-28 rounded-lg border border-slate-200 py-0.5 pl-6 pr-1.5 text-[10px] outline-none focus:border-blue-400"
            />
          </div>
          <button
            type="button"
            onClick={() => setSortKey(SORTS[(SORTS.findIndex((s) => s.key === sortKey) + 1) % SORTS.length].key)}
            title={`当前按${SORTS.find((s) => s.key === sortKey)?.label}排序，点击切换`}
            className="flex items-center gap-1 rounded-lg border border-slate-200 px-1.5 py-0.5 text-[10px] font-bold text-slate-500 hover:border-blue-300 hover:text-blue-600 transition-colors"
          >
            <ArrowDownUp className="h-3 w-3" />
            {SORTS.find((s) => s.key === sortKey)?.label}
          </button>
        </div>
      </div>

      {/* 明细列表 */}
      <div className="flex-1 min-h-0 overflow-y-auto px-2 pb-2 pt-0.5 space-y-1">
        {sorted.map((h) => {
          const pct = Math.max(0, Math.min(1, (h.value || 0) / base));
          return (
            <div
              key={h.code}
              title={`${h.name}（${h.code}） 持仓 ${h.shares} 股 · 市值 ${fmtMoney(h.value)}`}
              className="grid grid-cols-[minmax(0,1.3fr)_minmax(0,2.2fr)_minmax(0,1fr)_minmax(0,1.1fr)] items-center gap-3 rounded-xl border border-transparent px-2 py-1.5 transition-colors hover:border-slate-200 hover:bg-slate-50/70"
            >
              <div className="min-w-0">
                <div className="truncate text-xs font-bold text-slate-800">{h.name || h.code}</div>
                <div className="truncate font-mono text-[10px] text-slate-400">{h.code}</div>
              </div>
              <div className="min-w-0 flex items-center gap-2">
                <div className="flex-1 h-2 rounded-full bg-slate-100 overflow-hidden">
                  <div className={`h-full rounded-full transition-all ${barTone(h.profit)}`} style={{ width: `${(pct * 100).toFixed(1)}%` }} />
                </div>
                <span className="shrink-0 w-12 text-right font-mono text-[11px] font-bold text-slate-600">{(pct * 100).toFixed(1)}%</span>
              </div>
              <div className="min-w-0 text-right">
                <div className="truncate font-mono text-xs font-bold text-slate-800">{fmtMoney(h.current)}</div>
                <div className="truncate font-mono text-[10px] text-slate-400">成本 {fmtMoney(h.cost)}</div>
              </div>
              <div className="min-w-0 text-right">
                <div className={`truncate font-mono text-xs font-bold ${pnlTone(h.profit)}`}>
                  {h.profit > 0 ? '+' : ''}
                  {fmtMoney(h.profit)}
                </div>
                <div className={`truncate font-mono text-[10px] ${pnlTone(h.profit)}`}>
                  {h.profitPercent > 0 ? '+' : ''}
                  {fmtRowPct(h.profitPercent)}
                </div>
              </div>
            </div>
          );
        })}
        {sorted.length === 0 && (
          <div className="flex items-center justify-center py-10 text-sm text-slate-400">
            {holdings.length === 0 ? '暂无持仓数据' : '无匹配持仓'}
          </div>
        )}
      </div>
    </div>
  );
};

/** 今日执行折叠条：默认一行摘要，点开展开订单列表（并入持仓监控同页） */
export const ExecutionStrip: React.FC = () => {
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState<ExecutionItem[]>([]);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let cancelled = false;
    getDeskToday({ health: false, plan: false })
      .then((resp) => {
        if (!cancelled) {
          setItems(resp?.data?.execution?.items || []);
          setLoaded(true);
        }
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, []);

  const filled = items.filter((it) => String(it.status).toUpperCase() === 'FILLED').length;
  const rejected = items.filter((it) => String(it.status).toUpperCase() === 'REJECTED').length;

  return (
    <div className="shrink-0 rounded-2xl border border-gray-200 bg-white shadow-sm overflow-hidden">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="w-full flex items-center gap-2 px-4 py-2 text-left hover:bg-slate-50/70 transition-colors"
      >
        <span className="w-6 h-6 rounded-lg bg-amber-50 text-amber-600 flex items-center justify-center">
          <Activity className="w-3.5 h-3.5" />
        </span>
        <span className="text-xs font-bold text-slate-700">今日执行</span>
        <span className="text-[11px] text-slate-400">
          {loaded ? `共 ${items.length} 单` : '加载中…'}
          {loaded && items.length > 0 && ` · 成交 ${filled} · 拒单 ${rejected}`}
        </span>
        <span className="ml-auto text-slate-400">{open ? <ChevronUp className="w-4 h-4" /> : <ChevronDown className="w-4 h-4" />}</span>
      </button>
      {open && (
        <div className="max-h-52 overflow-y-auto px-3 pb-2 space-y-0.5 border-t border-slate-100 pt-1.5">
          {items.length === 0 && <div className="py-3 text-center text-[11px] text-slate-400">今日暂无委托/成交记录</div>}
          {items.map((it, idx) => {
            const s = String(it.status || '').toUpperCase();
            const isBuy = String(it.side).toUpperCase() === 'BUY';
            const tone =
              s === 'FILLED'
                ? 'bg-red-50 text-red-700 border-red-200'
                : s === 'REJECTED'
                  ? 'bg-rose-50 text-rose-700 border-rose-200'
                  : 'bg-amber-50 text-amber-700 border-amber-200';
            return (
              <div key={`${it.client_order_id || it.symbol}-${idx}`} className="flex items-center gap-2 rounded-lg px-2 py-1 text-[11px] hover:bg-slate-50">
                <span className={`shrink-0 rounded border px-1 py-px text-[9px] font-bold ${it.mode === 'REAL' ? 'bg-purple-50 text-purple-700 border-purple-200' : 'bg-slate-100 text-slate-500 border-slate-200'}`}>
                  {it.mode}
                </span>
                <span className={`shrink-0 font-bold ${isBuy ? 'text-red-600' : 'text-emerald-600'}`}>{isBuy ? '买' : '卖'}</span>
                <span className="min-w-0 truncate font-mono text-slate-700">{it.symbol}</span>
                <span className="shrink-0 font-mono text-slate-400">{it.quantity} 股</span>
                <span className={`ml-auto shrink-0 rounded border px-1.5 py-px text-[10px] font-bold ${tone}`}>
                  {s === 'FILLED' ? '成交' : s === 'REJECTED' ? '拒单' : s === 'SUBMITTED' || s === 'PENDING' || s === 'NEW' ? '已报' : it.status}
                </span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
};
