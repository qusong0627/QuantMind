/** 美股资金与筹码面板 —— 内部人交易 / 机构持仓（13F） / 除息日历 / 近期拆股
 *
 * 内部人走 SEC Form 4（只统计 Purchase / Sale），机构持仓走 13F（按季披露、天然滞后），
 * 都不是实时资金流。金额单位 `*_yi` 后端已换算为「亿美元」。
 */

import React, { useEffect, useState } from 'react';
import { ArrowDownRight, ArrowUpRight, Building2, CalendarClock, Info, Scissors, Wallet } from 'lucide-react';
import {
  getDividendCalendar, getInsiderMovers, getInstitutionalHolders, getRecentSplits,
} from '../services/api';
import type {
  UsDividendCalendar, UsDividendCalendarItem, UsInsiderMovers, UsInsiderRow,
  UsInstitutionalHolders, UsInstitutionalRow, UsRecentSplits, UsSplitItem,
} from '../types';
import { DateBadge, EmptyHint, NumText, SectionCard, fmtInt } from '../../market-analysis-shared/ui';

type Tone = 'red' | 'green' | 'blue';

const TONE: Record<Tone, { box: string; text: string }> = {
  red: { box: 'bg-red-50/70 border-red-100', text: 'text-red-600' },
  green: { box: 'bg-green-50/70 border-green-100', text: 'text-green-600' },
  blue: { box: 'bg-blue-50/70 border-blue-100', text: 'text-blue-700' },
};
/** 表格统一列头 / 数据行样式（紧凑看盘密度）；列宽由各表 GRID 常量给定 */
const HEAD =
  'grid gap-1 px-1 py-[3px] text-[9px] font-bold text-slate-400 border-b border-slate-200 bg-white/95 sticky top-0 z-10';
const ROW = 'grid gap-1 px-1 py-[3px] text-[10px] items-center border-b border-slate-50 last:border-0';
const SCROLL = 'flex flex-col flex-1 min-h-0 overflow-y-auto';

const INS_GRID = 'grid-cols-[14px_1fr_58px_34px_58px]';
const INST_GRID = 'grid-cols-[14px_1fr_54px_40px_54px]';
const DIV_GRID = 'grid-cols-[14px_1fr_36px_40px_58px]';
const SPLIT_GRID = 'grid-cols-[14px_1fr_58px_48px]';

/** 表头 + 行渲染壳：四张表结构相同，只有列宽与行内容不同（grid 在此统一注入表头，避免两处写重） */
function Table<T>({ grid, labels, items, render, loading, emptyText }: {
  grid: string; labels: readonly (string | readonly [string, true])[];
  items: readonly T[]; loading: boolean; emptyText: string;
  render: (item: T, index: number) => React.ReactNode;
}) {
  if (items.length === 0) return <EmptyHint loading={loading} text={emptyText} />;
  return (
    <div className={SCROLL}>
      <div className={`${HEAD} ${grid}`}>
        {labels.map((l) => {
          const [text, right] = Array.isArray(l) ? l : [l, false];
          return <span key={text} className={right ? 'text-right' : ''}>{text}</span>;
        })}
      </div>
      {items.map((item, i) => (
        <div key={i} className={`${ROW} ${grid}`}>{render(item, i)}</div>
      ))}
    </div>
  );
}

/** 可空数值 → 固定小数位文本；可空百分数 → 'x.x%'（null / NaN 一律 '--'，绝不参与运算或 toFixed） */
function fmtNum(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return '--';
  return Number(value).toFixed(digits);
}

function pctOrDash(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return '--';
  return `${Number(value).toFixed(digits)}%`;
}

/** 距事件日的自然语言描述 */
function daysLabel(days: number | null | undefined): string {
  if (days === null || days === undefined || !Number.isFinite(Number(days))) return '--';
  return Number(days) <= 0 ? '今日' : `${Number(days)} 天后`;
}

/** 标的单元格：中文名 + 代码（名字即代码时不重复显示） */
function SymbolCell({ name, symbol }: { name: string; symbol: string }) {
  return (
    <span className="flex items-center gap-1 min-w-0">
      <span className="font-bold text-slate-800 truncate" title={name}>{name}</span>
      {name !== symbol && <span className="text-[9px] font-mono text-slate-400 flex-shrink-0">{symbol}</span>}
    </span>
  );
}

/** 榜单小标题条（买入/卖出、增持/减持） */
function MiniHead({ tone, label, extra }: { tone: Tone; label: string; extra?: string }) {
  const Icon = tone === 'red' ? ArrowUpRight : ArrowDownRight;
  return (
    <span className="flex items-center gap-1.5 px-1 pb-0.5 border-b border-slate-100">
      <Icon className={`w-3 h-3 ${tone === 'red' ? 'text-red-500' : 'text-green-500'}`} />
      <span className={`text-[10px] font-extrabold ${TONE[tone].text}`}>{label}</span>
      {extra && <span className="text-[9px] font-mono text-slate-400">{extra}</span>}
    </span>
  );
}

/** 统计小卡（label 左、值右，一行） */
function MetricTile({ label, value, tone = 'blue' }: { label: string; value: React.ReactNode; tone?: Tone }) {
  return (
    <div className={`rounded-lg border px-2 py-1 flex items-baseline justify-between gap-1.5 ${TONE[tone].box}`}>
      <span className="text-[9px] font-bold text-slate-500 whitespace-nowrap">{label}</span>
      <span className={`text-[11px] font-extrabold font-mono whitespace-nowrap ${TONE[tone].text}`}>{value}</span>
    </div>
  );
}

// ---- 四张表 ----

const INS_HEAD = ['#', '标的 / 内部人', ['最近日期', true], ['笔数', true], ['金额', true]] as const;
const INST_HEAD = ['#', '标的', ['增家数/减家数', true], ['机构数', true], ['增减金额', true]] as const;
const DIV_HEAD = ['#', '标的', '除息日', ['距今', true], ['派息日', true]] as const;
const SPLIT_HEAD = ['#', '标的', '拆股日', ['比例（新:旧）', true]] as const;

/** 内部人榜行（人名 · 职位 / 最近日期 / 笔数 / 金额） */
function insiderCells(it: UsInsiderRow, i: number, tone: Tone) {
  return (
    <>
      <span className="text-[9px] font-extrabold text-slate-400">{i + 1}</span>
      <span className="flex flex-col min-w-0">
        <SymbolCell name={it.name} symbol={it.symbol} />
        <span className="flex items-center gap-1 text-[9px] text-slate-400 min-w-0">
          <span className="truncate max-w-[90px]" title={it.insider}>{it.insider || '--'}</span>
          <span className="text-slate-300">·</span>
          <span className="truncate max-w-[70px]" title={it.position}>{it.position || '--'}</span>
        </span>
      </span>
      <span className="text-right font-mono text-[9px] text-slate-400 whitespace-nowrap">
        {(it.last_date || '').slice(5) || '--'}
      </span>
      <span className="text-right font-mono text-[9px] text-slate-500 whitespace-nowrap">{fmtInt(it.trades)}</span>
      <span className={`text-right font-mono font-extrabold whitespace-nowrap ${TONE[tone].text}`}>
        {fmtInt(it.value_yi)}<span className="text-[9px] font-normal text-slate-400">亿</span>
      </span>
    </>
  );
}

/** 机构增减持行（增减家数 / 机构数 / 增减金额） */
function instCells(it: UsInstitutionalRow, i: number) {
  return (
    <>
      <span className="text-[9px] font-extrabold text-slate-400">{i + 1}</span>
      <span className="flex flex-col min-w-0">
        <SymbolCell name={it.name} symbol={it.symbol} />
        <span className="font-mono text-[9px] text-slate-400 whitespace-nowrap" title="明细表内头部机构合计占比（非全机构口径）">
          头部持股 {pctOrDash(it.top_holders_pct, 1)}
        </span>
      </span>
      <span className="text-right font-mono text-[9px] whitespace-nowrap">
        <span className="text-red-600 font-bold">{fmtInt(it.increased)}</span>
        <span className="text-slate-300">/</span>
        <span className="text-green-600 font-bold">{fmtInt(it.decreased)}</span>
      </span>
      <span className="text-right font-mono text-[9px] text-slate-500 whitespace-nowrap">{fmtInt(it.holders)}</span>
      <span className="text-right whitespace-nowrap">
        <NumText value={it.delta_value_yi} digits={1} suffix="亿" className="text-[10px]" />
      </span>
    </>
  );
}

/** 内部人交易卡（统计条 + 买卖双榜 + 口径脚注） */
const InsiderCard: React.FC<{ insider: UsInsiderMovers | null; loading: boolean }> = ({ insider, loading }) => (
  <SectionCard
    className="!p-2.5 gap-1.5 xl:col-span-2 h-full min-h-0"
    title={
      <span className="flex items-center gap-1.5">
        <Building2 className="w-3.5 h-3.5 text-blue-600" />
        内部人交易榜
        <span className="text-[9px] font-normal text-slate-400">高管 / 董事 / 大股东 · 按金额排序</span>
      </span>
    }
    extra={
      <span className="flex items-center gap-2">
        <span className="text-[9px] font-mono text-slate-400 whitespace-nowrap">近 {insider?.days ?? 90} 天</span>
        <DateBadge label="数据" date={insider?.as_of} />
      </span>
    }
  >
    <div className="grid grid-cols-2 md:grid-cols-4 gap-1">
      <MetricTile label="买入笔数" value={fmtInt(insider?.buy_count)} tone="red" />
      <MetricTile label="卖出笔数" value={fmtInt(insider?.sell_count)} tone="green" />
      <MetricTile label="买入金额" value={`US$ ${fmtInt(insider?.buy_amount_yi)}亿`} tone="red" />
      <MetricTile label="卖出金额" value={`US$ ${fmtInt(insider?.sell_amount_yi)}亿`} tone="green" />
    </div>
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-1.5 shrink-0 xl:h-[380px]">
      <div className="flex flex-col gap-0.5 min-w-0 min-h-0">
        <MiniHead tone="red" label="买入榜" extra={`Top ${insider?.top_buys?.length ?? 0}`} />
        <Table
          grid={INS_GRID} labels={INS_HEAD} items={insider?.top_buys ?? []}
          loading={loading} emptyText="近 90 天无内部人买入记录"
          render={(it, i) => insiderCells(it, i, 'red')}
        />
      </div>
      <div className="flex flex-col gap-0.5 min-w-0 min-h-0">
        <MiniHead tone="green" label="卖出榜" extra={`Top ${insider?.top_sells?.length ?? 0}`} />
        <Table
          grid={INS_GRID} labels={INS_HEAD} items={insider?.top_sells ?? []}
          loading={loading} emptyText="近 90 天无内部人卖出记录"
          render={(it, i) => insiderCells(it, i, 'green')}
        />
      </div>
    </div>
    <p className="text-[9px] text-slate-400 leading-relaxed">
      只统计 Purchase（买入）与 Sale（卖出）两类交易；授予（Grant/Award）、行权（Exercise）、
      赠与属薪酬事件，已排除。同一人同一标的的多笔交易已合并后再排序，「笔数」为合并后的成交笔数。
    </p>
  </SectionCard>
);

/** 机构持仓卡（中位占比 + 增减持双榜 + 13F 口径脚注） */
const InstCard: React.FC<{ inst: UsInstitutionalHolders | null; loading: boolean }> = ({ inst, loading }) => (
  <SectionCard
    className="!p-2.5 gap-1.5 h-full min-h-0"
    title={
      <span className="flex items-center gap-1.5">
        <Building2 className="w-3.5 h-3.5 text-blue-600" />
        机构持仓变动
        <span className="text-[9px] font-normal text-slate-400">13F · 增/减持</span>
      </span>
    }
    extra={
      <span className="flex items-center gap-2">
        <span className="text-[9px] font-mono text-slate-400 whitespace-nowrap">覆盖 {fmtInt(inst?.coverage)} 只</span>
        <DateBadge label="13F 披露日" date={inst?.report_date} />
      </span>
    }
  >
    <div className="grid grid-cols-2 gap-1">
      <MetricTile label="机构持股中位占比" value={pctOrDash(inst?.institutions_pct_median)} />
      <MetricTile label="内部人持股中位占比" value={pctOrDash(inst?.insiders_pct_median, 2)} tone="red" />
    </div>
    <div className="flex flex-col gap-0.5 min-w-0 min-h-0">
      <span className="flex items-center gap-1.5 px-1 pb-0.5 border-b border-slate-100">
        <span className="text-[10px] font-extrabold text-blue-700">增持榜</span>
        <span className="text-[9px] font-mono text-slate-400">按增减金额排序</span>
      </span>
      <Table
        grid={INST_GRID} labels={INST_HEAD} items={inst?.top_increases ?? []}
        loading={loading} emptyText="暂无增持记录" render={instCells}
      />
    </div>
    <div className="flex flex-col gap-0.5 min-w-0 min-h-0">
      <span className="flex items-center gap-1.5 px-1 pb-0.5 border-b border-slate-100">
        <span className="text-[10px] font-extrabold text-blue-700">减持榜</span>
        <span className="text-[9px] font-mono text-slate-400">按增减金额排序</span>
      </span>
      <Table
        grid={INST_GRID} labels={INST_HEAD} items={inst?.top_decreases ?? []}
        loading={loading} emptyText="暂无减持记录" render={instCells}
      />
    </div>
    <div className="flex items-start gap-1.5">
      <Info className="w-3 h-3 text-slate-300 mt-0.5 flex-shrink-0" />
      <p className="text-[9px] text-slate-400 leading-relaxed">
        13F 按季度披露、通常滞后一个季度以上，只反映机构多头持仓快照（不含空头与衍生品）；
        增减金额按机构自身持股变化率还原，为估算值。
      </p>
    </div>
  </SectionCard>
);

/** 除息日历行（除息日 / 距今 / 派息日）—— 距今 ≤7 天高亮为临近除息窗口 */
function divCells(it: UsDividendCalendarItem, rank: number) {
  return (
    <>
      <span className="text-[9px] font-extrabold text-slate-400">{rank}</span>
      <SymbolCell name={it.name} symbol={it.symbol} />
      <span className="font-mono text-[9px] text-amber-600 whitespace-nowrap">{(it.ex_dividend_date || '').slice(5)}</span>
      <span className={`text-right text-[9px] font-extrabold whitespace-nowrap ${it.days_until <= 7 ? 'text-blue-600' : 'text-slate-400'}`}>
        {daysLabel(it.days_until)}
      </span>
      <span className="text-right font-mono text-[9px] text-slate-500 whitespace-nowrap">{it.dividend_date || '--'}</span>
    </>
  );
}

/** 拆股行（拆股日 / 比例） */
function splitCells(it: UsSplitItem, rank: number) {
  return (
    <>
      <span className="text-[9px] font-extrabold text-slate-400">{rank}</span>
      <SymbolCell name={it.name} symbol={it.symbol} />
      <span className="font-mono text-[9px] text-slate-500 whitespace-nowrap">{it.split_date}</span>
      <span
        className="text-right font-mono font-extrabold text-blue-700 whitespace-nowrap"
        title={`每 1 股拆为 ${fmtNum(it.ratio)} 股`}
      >
        {fmtNum(it.ratio)}:1
      </span>
    </>
  );
}

export const UsHoldingsPanel: React.FC = () => {
  const [insider, setInsider] = useState<UsInsiderMovers | null>(null);
  const [institutional, setInstitutional] = useState<UsInstitutionalHolders | null>(null);
  const [dividends, setDividends] = useState<UsDividendCalendar | null>(null);
  const [splits, setSplits] = useState<UsRecentSplits | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    Promise.all([
      getInsiderMovers(90, 30),
      getInstitutionalHolders(30),
      getDividendCalendar(60, 60),
      getRecentSplits(365, 30),
    ])
      .then(([ins, inst, div, spl]) => {
        if (!alive) return;
        setInsider(ins);
        setInstitutional(inst);
        setDividends(div);
        setSplits(spl);
      })
      .catch(() => undefined)
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, []);

  const divItems = dividends?.items ?? [];
  const splitItems = (splits?.items ?? []).slice(0, 15);

  return (
    <div className="flex flex-col gap-1.5">
      {/* 面板标题 */}
      <div className="rounded-2xl border border-blue-100/70 bg-gradient-to-r from-blue-50/90 via-sky-50/70 to-white px-4 py-2 flex items-center justify-between gap-3">
        <div className="flex items-center gap-2 min-w-0">
          <Wallet className="w-4 h-4 text-blue-600 flex-shrink-0" />
          <span className="text-sm font-extrabold text-slate-800 whitespace-nowrap">资金与筹码</span>
          <span className="text-[11px] text-slate-500 truncate">
            内部人买卖（SEC Form 4）/ 机构持仓（13F）/ 除息与拆股事件
          </span>
        </div>
        <DateBadge label="数据" date={insider?.as_of || dividends?.as_of} />
      </div>

      {/* 内部人 + 机构：两行三列紧凑网格（宽屏） */}
      <div className="grid grid-cols-1 xl:grid-cols-3 gap-1.5 shrink-0 xl:h-[400px]">
        <InsiderCard insider={insider} loading={loading} />
        <InstCard inst={institutional} loading={loading} />

        {/* 除息日历 */}
        <SectionCard
          className="!p-2.5 gap-1.5 xl:col-span-2 h-full min-h-0"
          title={
            <span className="flex items-center gap-1.5">
              <CalendarClock className="w-3.5 h-3.5 text-blue-600" />
              除息日历
              <span className="text-[9px] font-normal text-slate-400">未来 {dividends?.days ?? 60} 天</span>
            </span>
          }
          extra={
            <span className="flex items-center gap-2">
              <span className="text-[9px] font-mono text-slate-400 whitespace-nowrap">共 {fmtInt(dividends?.total)} 家</span>
              <DateBadge label="数据" date={dividends?.as_of} />
            </span>
          }
        >
          <Table grid={DIV_GRID} labels={DIV_HEAD} items={divItems}
            loading={loading} emptyText="未来 60 天内暂无披露除息"
            render={(it, i) => divCells(it, i + 1)}
          />
          <p className="text-[9px] text-slate-400 leading-relaxed">
            除息日当天买入不享有本次派息（须在除息日前一交易日收盘持有）；
            「距今 ≤7 天」高亮为临近除息的调仓窗口。
          </p>
        </SectionCard>

        {/* 近期拆股 */}
        <SectionCard
          className="!p-2.5 gap-1.5 h-full min-h-0"
          title={
            <span className="flex items-center gap-1.5">
              <Scissors className="w-3.5 h-3.5 text-blue-600" />
              近期拆股
              <span className="text-[9px] font-normal text-slate-400">近 {splits?.days ?? 365} 天</span>
            </span>
          }
          extra={
            <span className="text-[9px] font-mono text-slate-400 whitespace-nowrap">前 {splitItems.length} 条</span>
          }
        >
          <Table grid={SPLIT_GRID} labels={SPLIT_HEAD} items={splitItems}
            loading={loading} emptyText="近期无拆股事件"
            render={(it, i) => splitCells(it, i + 1)}
          />
          <p className="text-[9px] text-slate-400 leading-relaxed">
            拆股（比例为 新:旧）会污染未复权日线的跨期收益，出现异常区间收益时可在此排查。
          </p>
        </SectionCard>
      </div>
    </div>
  );
};
