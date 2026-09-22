/**
 * 持仓监控可视化面板（2026-09-17 重设计 · v2 加料）
 *
 * - PositionVisualBoard：单卡三层——
 *   ① KPI 行：总市值 / 持仓数 / 盈利·亏损家数 / 现金占比 / 浮动盈亏（涨红跌绿）
 *   ② 图表行：左「市值 Treemap」（块面积=市值、颜色=盈亏深浅）＋ 右「盈亏贡献 Top12 横向柱」
 *   ③ 持仓明细：市值占比条形列表（排序切换 市值/盈亏/比例 + 关键字过滤）
 *      + 行内「卖出」/ 批量「清仓选中」→ 预检确认面板（只预填与转发，不自己下单）
 * - ExecutionStrip：今日执行折叠条（默认一行摘要，点开列单）。
 */
import React, { useMemo, useState, useEffect } from 'react';
import ReactECharts from 'echarts-for-react';
import { Activity, ArrowDownUp, ChevronDown, ChevronUp, PieChart, Search } from 'lucide-react';
import { Checkbox, message } from 'antd';
import type { NormalizedHolding, PositionSummary } from '../utils/positionMetrics';
import { getDeskToday } from '../../../features/desk/services/deskService';
import { executionUnavailableReason } from '../../../features/desk/deskModel';
import type { ExecutionItem } from '../../../features/desk/types';
import { toSuffixCode } from '../../../utils/portfolioUtils';
import { PushConfirmPanel } from '../../../features/stock-terminal/components/PushConfirmPanel';
import type { PushChannel, PushExecute } from '../../../features/stock-terminal-shared/types';

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
  /** 卖出预检的默认通道（跟随页面顶栏 模拟/实盘） */
  defaultChannels?: PushChannel[];
}> = ({ holdings, summary, defaultChannels = ['sim'] }) => {
  const [sortKey, setSortKey] = useState<SortKey>('value');
  const [q, setQ] = useState('');

  // 卖出入口：勾选集合 + 当前要卖的标的（null = 面板关闭）
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [sellSymbols, setSellSymbols] = useState<string[] | null>(null);
  const [channels, setChannels] = useState<PushChannel[]>(defaultChannels);

  // 缺价行（`priceMissing`）不参与任何加减：它的盈亏是「不知道」，不是 0。
  // 2026-09-22 事故就出在这行合计上 —— 十行缺价被算成「市值 0 − 成本」，
  // 浮动盈亏显示 −84,778.90（= 全部持仓成本），而实际是小幅盈利。
  const priced = useMemo(() => holdings.filter((h) => !h.priceMissing), [holdings]);
  const missingCount = holdings.length - priced.length;
  const totalPnl = useMemo(() => priced.reduce((s, h) => s + (h.profit || 0), 0), [priced]);
  const winCount = priced.filter((h) => h.profit > 0).length;
  const loseCount = priced.filter((h) => h.profit < 0).length;
  // 一行都算不出来时宁可不报数：0 在这里会被读成「不赚不亏」，与「拿不到价」是两回事。
  const pnlUnknown = holdings.length > 0 && priced.length === 0;
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

  // —— 卖出入口 ——
  // 持仓行的 code 是**前缀式**（SH600036），预检接口要**后缀式**（600036.SH）：
  // 直接透传会被当成非法代码逐笔拦下。
  // 本仓库 tsc 下函数式 setter（`setX(prev => …)`）一律 TS2345，只能传值更新。
  const togglePick = (code: string) => {
    const next = new Set(picked);
    if (next.has(code)) next.delete(code);
    else next.add(code);
    setPicked(next);
  };
  const togglePickAll = () =>
    setPicked(
      sorted.length > 0 && sorted.every((h) => picked.has(h.code))
        ? new Set<string>()
        : new Set(sorted.map((h) => h.code)),
    );
  // 勾选集以**当前持仓**为准重算：卖出后市集刷新，勾选里会留下已不在持仓的代码，
  // 直接拿 `[...picked]` 去预检会为一只已经卖掉的票再发一张卖单。
  const pickedRows = sorted.filter((h) => picked.has(h.code));
  const openSell = (codes: string[]) => {
    const symbols = codes.map(toSuffixCode).filter(Boolean);
    if (symbols.length === 0) {
      message.warning('没有可提交的标的');
      return;
    }
    // 通道默认值每次**打开时**重取：`useState(defaultChannels)` 只在挂载那一次生效，
    // 而账户视图（模拟/实盘）可以在页面生命周期内随时切换 —— 切到实盘后再点卖出，
    // 面板若还停在「仅模拟盘」，实盘持仓会被逐笔拦成「未勾选实盘通道」。
    // 面板里手动改过的通道只在本次打开内有效，重开时重新跟随账户视图。
    setChannels(defaultChannels);
    setSellSymbols(symbols);
  };
  const closeSell = () => setSellSymbols(null);
  const sellDone = (outcome: PushExecute) => {
    // 只有真的成交/部分成交才清勾选。被拦下时保留勾选，用户改完条件可以重推——
    // 无条件清空会让「被风控拦下」看起来像「已经卖掉了」。
    const succeeded = Number(outcome?.summary?.succeeded ?? 0);
    if (!outcome?.dry_run && succeeded > 0) setPicked(new Set());
    else message.warning('本次未提交成功，勾选保留——请查看预检面板里的逐笔原因');
  };

  // —— 图表①：市值分布货架马赛克（自绘：两行货架，块宽=占比，大块在上，小块归并） ——
  const shelves = useMemo(() => {
    const items = [...holdings].sort((a, b) => b.value - a.value);
    const total = items.reduce((sum, h) => sum + Math.max(h.value, 0), 0) || 1;
    // 第一行：市值最大的块，累计占比 ≥ 55% 截止（至少 2 块、至多 5 块）
    const row1: NormalizedHolding[] = [];
    let cum = 0;
    for (const h of items) {
      if (row1.length >= 5 || (row1.length >= 2 && cum / total >= 0.55)) break;
      row1.push(h);
      cum += Math.max(h.value, 0);
    }
    const rest = items.slice(row1.length);
    // 第二行：占比 ≥ 1.2% 的逐块展示；更小的归并「其余 N 只」
    const row2 = rest.filter((h) => Math.max(h.value, 0) / total >= 0.012);
    const tail = rest.slice(row2.length);
    const tailValue = tail.reduce((sum, h) => sum + Math.max(h.value, 0), 0);
    return { row1, row2, tail, tailValue, total };
  }, [holdings]);

  const mosaicTile = (h: NormalizedHolding, key: string) => {
    const share = Math.max(h.value, 0) / shelves.total;
    return (
      <div
        key={key}
        title={
          h.priceMissing
            ? `${h.name}（${h.code}）\n持仓 ${h.shares} 股 · 成本 ${fmtMoney(h.cost)}\n现价缺失，市值与盈亏暂不可知`
            : `${h.name}（${h.code}）\n市值 ${fmtMoney(h.value)} · 占比 ${(share * 100).toFixed(1)}%\n盈亏 ${h.profit > 0 ? '+' : ''}${fmtMoney(h.profit)}（${fmtRowPct(h.profitPercent)}）`
        }
        style={{ flexGrow: Math.max(share, 0.004), flexBasis: 0, backgroundColor: tileColor(h.profit, h.profitPercent) }}
        className="min-w-[30px] h-full rounded-md px-1.5 py-1 overflow-hidden cursor-default transition-transform hover:scale-[1.015]"
      >
        <div className="truncate text-[10px] font-bold text-white/95 leading-3.5">{(h.name || h.code).slice(0, 5)}</div>
        <div className="truncate font-mono text-[9px] text-white/80 leading-3">{h.priceMissing ? '—' : `${(share * 100).toFixed(1)}%`}</div>
      </div>
    );
  };

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
          {/* 缺价单列一格：这些行的盈亏没被算进「盈利/亏损」，但也没消失——
              不单独报数的话，用户会以为持仓数对不上。 */}
          {missingCount > 0 && (
            <span
              className="rounded-full border border-amber-200 bg-amber-50 px-2.5 py-1 text-amber-700"
              title="这些持仓拿不到现价（补价失败 / 停牌 / 退市），市值与盈亏暂不可知，未计入盈亏合计"
            >
              缺价 <span className="font-mono">{missingCount}</span>
            </span>
          )}
          <span
            className="rounded-full border border-slate-200 bg-slate-50 px-2.5 py-1 text-slate-600"
            title={summary.cashRatio > 1.5 ? '现金/总资产 > 100%，账户口径数据异常，暂显 —' : undefined}
          >
            现金占比 <span className="font-mono text-slate-800">{summary.cashRatio > 1.5 ? '—' : fmtPct(summary.cashRatio)}</span>
          </span>
          <span
            className={`rounded-full border px-2.5 py-1 ${
              pnlUnknown
                ? 'border-slate-200 bg-slate-50 text-slate-500'
                : totalPnl > 0
                  ? 'border-red-200 bg-red-50 text-red-600'
                  : totalPnl < 0
                    ? 'border-emerald-200 bg-emerald-50 text-emerald-600'
                    : 'border-slate-200 bg-slate-50 text-slate-500'
            }`}
            title={
              pnlUnknown
                ? '全部持仓都拿不到现价，盈亏不可知'
                : missingCount > 0
                  ? `${missingCount} 只缺价未计入`
                  : undefined
            }
          >
            浮动盈亏{' '}
            <span className="font-mono">
              {pnlUnknown ? '—' : `${totalPnl > 0 ? '+' : ''}${fmtMoney(totalPnl)}`}
            </span>
          </span>
        </div>
      </div>

      {/* ② 主体：明细列表占满整行宽度，图表（市值分布 + 盈亏贡献）沉到底部一行。
          2026-09-20 用户要求：图表原本挤在右侧 430px 的窄列里（中间被挤着），
          改为底部整行两列 —— 明细列因此拿回宽度，图表也从「窄高」变成「宽扁」。 */}
      <div className="flex-1 min-h-0 flex flex-col gap-2 px-3 pt-1.5 pb-2">
        {/* 上部：工具行 + 明细列表（整行宽） */}
        <div className="flex-1 min-w-0 min-h-0 flex flex-col">
        <div className="shrink-0 flex items-center gap-2 pb-1">
        <div className="grid grid-cols-[1.1rem_minmax(0,1.3fr)_minmax(0,2.2fr)_minmax(0,1fr)_minmax(0,1.1fr)_3.5rem] items-center gap-3 flex-1 text-[10px] font-bold text-slate-400">
          <span />
          <span>股票</span>
          <span title="条宽 = 个股市值 ÷ 持仓总市值（红涨绿跌）">市值占比</span>
          <span className="text-right">现价 / 成本</span>
          <span className="text-right">盈亏（金额 / 比例）</span>
          <span className="text-right">操作</span>
        </div>
        <div className="flex shrink-0 items-center gap-1.5">
          {sorted.length > 0 && (
            <Checkbox
              checked={pickedRows.length === sorted.length}
              indeterminate={pickedRows.length > 0 && pickedRows.length < sorted.length}
              onChange={togglePickAll}
              className="!text-[10px] !font-bold !text-slate-500"
            >
              全选
            </Checkbox>
          )}
          {pickedRows.length > 0 && (
            <button
              type="button"
              onClick={() => openSell(pickedRows.map((h) => h.code))}
              title="对勾选的持仓发起整仓卖出预检（数量由服务端按可用持仓给，仍需在确认面板里手动确认）"
              className="flex items-center gap-1 rounded-lg bg-emerald-600 px-2 py-0.5 text-[10px] font-bold text-white transition-colors hover:bg-emerald-700"
            >
              清仓选中 {pickedRows.length} 只
            </button>
          )}
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
              title={`${h.name}（${h.code}） 持仓 ${h.shares} 股 · 市值 ${h.priceMissing ? '—（缺现价）' : fmtMoney(h.value)}`}
              className="grid grid-cols-[1.1rem_minmax(0,1.3fr)_minmax(0,2.2fr)_minmax(0,1fr)_minmax(0,1.1fr)_3.5rem] items-center gap-3 rounded-xl border border-transparent px-2 py-1.5 transition-colors hover:border-slate-200 hover:bg-slate-50/70"
            >
              <Checkbox checked={picked.has(h.code)} onChange={() => togglePick(h.code)} />
              <div className="min-w-0">
                <div className="truncate text-xs font-bold text-slate-800">{h.name || h.code}</div>
                <div className="truncate font-mono text-[10px] text-slate-400">{h.code}</div>
              </div>
              <div className="min-w-0 flex items-center gap-2">
                <div className="flex-1 h-2 rounded-full bg-slate-100 overflow-hidden">
                  <div className={`h-full rounded-full transition-all ${barTone(h.profit)}`} style={{ width: `${(pct * 100).toFixed(1)}%` }} />
                </div>
                <span className="shrink-0 w-12 text-right font-mono text-[11px] font-bold text-slate-600">
                  {h.priceMissing ? '—' : `${(pct * 100).toFixed(1)}%`}
                </span>
              </div>
              {/* 缺价行：现价与盈亏一律出 `—`。市值占比同理——0% 会读成「这只票不值钱」，
                  它其实只是「不知道」。成本价照常显示，缺的是价不是成本。 */}
              <div className="min-w-0 text-right">
                <div className="truncate font-mono text-xs font-bold text-slate-800">{h.priceMissing ? '—' : fmtMoney(h.current)}</div>
                <div className="truncate font-mono text-[10px] text-slate-400">成本 {fmtMoney(h.cost)}</div>
              </div>
              <div className="min-w-0 text-right">
                {h.priceMissing ? (
                  <>
                    <div className="truncate font-mono text-xs font-bold text-slate-400">—</div>
                    <div className="truncate font-mono text-[10px] text-slate-400">缺现价</div>
                  </>
                ) : (
                  <>
                    <div className={`truncate font-mono text-xs font-bold ${pnlTone(h.profit)}`}>
                      {h.profit > 0 ? '+' : ''}
                      {fmtMoney(h.profit)}
                    </div>
                    <div className={`truncate font-mono text-[10px] ${pnlTone(h.profit)}`}>
                      {h.profitPercent > 0 ? '+' : ''}
                      {fmtRowPct(h.profitPercent)}
                    </div>
                  </>
                )}
              </div>
              {/* 只有一个「卖出」而不是「卖出 / 清仓」两个：预检面板本来就把数量预填成
                  全部可用持仓，想卖一半才需要手动改数。批量清仓走上方「全选 → 清仓选中」。
                  绿色 = 卖（A 股买红卖绿）。 */}
              <button
                type="button"
                onClick={() => openSell([h.code])}
                title={`卖出 ${h.name || h.code}（打开预检确认面板，不会自动下单）`}
                className="justify-self-end rounded-lg bg-emerald-600 px-2 py-0.5 text-[10px] font-bold text-white transition-colors hover:bg-emerald-700"
              >
                卖出
              </button>
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

        {/* 底部：图表一行两列（窄屏堆叠）。min-w-0 必须给：grid 子项默认 min-width:auto，
            ECharts 内部的 canvas 会把列撑破（memory: echarts-resize-deadlock-minw0）。 */}
        <div className="shrink-0 grid grid-cols-1 lg:grid-cols-2 gap-2">
          <div className="min-w-0 h-[190px] lg:h-[200px] rounded-xl border border-slate-100 bg-slate-50/20 p-2 flex flex-col">
            <div className="mb-1 text-[10px] font-bold text-slate-400" title="货架马赛克：块宽=持仓市值占比（两行按市值降序），颜色=盈亏（红涨绿跌，越深幅度越大）；小块归并「其余」">
              市值分布
            </div>
            {holdings.length > 0 ? (
              <div className="flex flex-1 min-h-0 flex-col gap-1">
                <div className="flex flex-1 gap-1 min-h-0">{shelves.row1.map((h) => mosaicTile(h, h.code))}</div>
                {(shelves.row2.length > 0 || shelves.tail.length > 0) && (
                  <div className="flex flex-1 gap-1 min-h-0">
                    {shelves.row2.map((h) => mosaicTile(h, h.code))}
                    {shelves.tail.length > 0 && (
                      <div
                        title={`其余 ${shelves.tail.length} 只（合计占比 ${((shelves.tailValue / shelves.total) * 100).toFixed(1)}%）\n${shelves.tail.map((h) => h.name || h.code).join('、')}`}
                        style={{ flexGrow: Math.max(shelves.tailValue / shelves.total, 0.06), flexBasis: 0 }}
                        className="min-w-[30px] h-full rounded-md bg-slate-200 px-1.5 py-1 overflow-hidden cursor-default"
                      >
                        <div className="truncate text-[10px] font-bold text-slate-600 leading-3.5">其余 {shelves.tail.length} 只</div>
                        <div className="truncate font-mono text-[9px] text-slate-500 leading-3">
                          {((shelves.tailValue / shelves.total) * 100).toFixed(1)}%
                        </div>
                      </div>
                    )}
                  </div>
                )}
              </div>
            ) : (
              <div className="flex flex-1 items-center justify-center text-[11px] text-slate-300">暂无持仓</div>
            )}
          </div>
          <div className="min-w-0 h-[190px] lg:h-[200px] rounded-xl border border-slate-100 bg-slate-50/20 p-2 flex flex-col">
            <div className="mb-1 text-[10px] font-bold text-slate-400" title="盈亏金额绝对值 Top12（红=盈利、绿=亏损）">
              盈亏贡献 Top12
            </div>
            {holdings.some((h) => h.profit !== 0) ? (
              <div className="flex-1 min-h-0">
                <ReactECharts option={contribOption} style={{ height: '100%' }} notMerge />
              </div>
            ) : (
              <div className="flex flex-1 items-center justify-center text-[11px] text-slate-300">
                {missingCount > 0 ? '持仓缺现价，盈亏不可知' : '暂无盈亏数据'}
              </div>
            )}
          </div>
        </div>
      </div>

      {/* 卖出预检确认面板：本板只负责「预填 + 打开」，数量/风控/新闻全部由服务端
          `_build_legs` 给，与真正下单同源。`sellSymbols` 非空即打开，关闭时置 null。 */}
      <PushConfirmPanel
        open={sellSymbols !== null}
        symbols={sellSymbols ?? []}
        side="sell"
        channels={channels}
        onChannelsChange={setChannels}
        onClose={closeSell}
        onDone={sellDone}
      />
    </div>
  );
};

/** 今日执行折叠条：默认一行摘要，点开展开订单列表（并入持仓监控同页） */
export const ExecutionStrip: React.FC = () => {
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState<ExecutionItem[]>([]);
  const [unavailableReason, setUnavailableReason] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let cancelled = false;
    getDeskToday({ health: false, plan: false })
      .then((resp) => {
        if (!cancelled) {
          const execution = resp?.data?.execution;
          // 不可归集 ≠ 没交易：留着原因，别让下面渲染成"共 0 单"
          setUnavailableReason(executionUnavailableReason(execution));
          setItems(execution?.items || []);
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
          {unavailableReason
            ? '暂不可按市场归集'
            : loaded
              ? `共 ${items.length} 单`
              : '加载中…'}
          {!unavailableReason && loaded && items.length > 0 && ` · 成交 ${filled} · 拒单 ${rejected}`}
        </span>
        <span className="ml-auto text-slate-400">{open ? <ChevronUp className="w-4 h-4" /> : <ChevronDown className="w-4 h-4" />}</span>
      </button>
      {open && (
        <div className="max-h-52 overflow-y-auto px-3 pb-2 space-y-0.5 border-t border-slate-100 pt-1.5">
          {unavailableReason ? (
            <div className="py-3 px-2 text-[11px] leading-relaxed text-slate-500">{unavailableReason}</div>
          ) : (
            items.length === 0 && <div className="py-3 text-center text-[11px] text-slate-400">今日暂无委托/成交记录</div>
          )}
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
