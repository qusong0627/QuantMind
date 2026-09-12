/** 美股资金与筹码面板 —— 内部人交易 / 机构持仓（13F） / 除息日历 / 近期拆股
 *
 * 对应港股模块的「南向资金 + CCASS 席位」，但美股的制度性披露口径完全不同：内部人交易走
 * SEC Form 4（只统计 Purchase / Sale），机构持仓走 13F（按季度披露、天然滞后），都不是实时资金流。
 * 金额单位：`value_yi` / `buy_amount_yi` / `sell_amount_yi` / `delta_value_yi` 后端已换算为「亿美元」。
 */

import React, { useEffect, useState } from 'react';
import { ArrowDownRight, ArrowUpRight, Building2, CalendarClock, Info, Scissors, Wallet } from 'lucide-react';
import {
  getDividendCalendar, getInsiderMovers, getInstitutionalHolders, getRecentSplits,
} from '../services/api';
import type {
  UsDividendCalendar, UsInsiderMovers, UsInsiderRow, UsInstitutionalHolders,
  UsInstitutionalRow, UsRecentSplits,
} from '../types';
import {
  DateBadge, EmptyHint, NumText, RankRow, SectionCard, fmtInt,
} from '../../market-analysis-shared/ui';

type Tone = 'red' | 'green' | 'blue';

const TONE: Record<Tone, { box: string; text: string }> = {
  red: { box: 'bg-red-50/70 border-red-100', text: 'text-red-600' },
  green: { box: 'bg-green-50/70 border-green-100', text: 'text-green-600' },
  blue: { box: 'bg-blue-50/70 border-blue-100', text: 'text-blue-700' },
};

const LIST_SCROLL = 'flex flex-col max-h-[320px] overflow-y-auto';

/** 可空数值 → 固定小数位文本（null / NaN 一律 '--'，绝不参与运算或 toFixed） */
function fmtNum(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return '--';
  return Number(value).toFixed(digits);
}

/** 可空百分数 → 'x.x%'；无效值 '--' */
function pctOrDash(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return '--';
  return `${Number(value).toFixed(digits)}%`;
}

/** 距事件日的自然语言描述 */
function daysLabel(days: number | null | undefined): string {
  if (days === null || days === undefined || !Number.isFinite(Number(days))) return '--';
  const n = Number(days);
  return n <= 0 ? '今日' : `${n} 天后`;
}

function MetricTile({ label, value, tone = 'blue' }: { label: string; value: React.ReactNode; tone?: Tone }) {
  return (
    <div className={`rounded-xl border px-3 py-2 flex flex-col gap-0.5 ${TONE[tone].box}`}>
      <span className="text-[10px] font-bold text-slate-500">{label}</span>
      <span className={`text-sm font-extrabold font-mono whitespace-nowrap ${TONE[tone].text}`}>{value}</span>
    </div>
  );
}

function ListHeader({ icon, label, tone, extra }: { icon: React.ReactNode; label: string; tone: Tone; extra?: string }) {
  return (
    <div className="flex items-center gap-1.5 px-1 pb-1 border-b border-slate-100">
      {icon}
      <span className={`text-[10px] font-extrabold ${TONE[tone].text}`}>{label}</span>
      {extra && <span className="text-[9px] font-mono text-slate-400">{extra}</span>}
    </div>
  );
}

/** 内部人榜单（人名 · 职位 / 金额） */
function InsiderList({
  rows, tone, emptyText, loading,
}: { rows: UsInsiderRow[]; tone: Tone; emptyText: string; loading?: boolean }) {
  if (rows.length === 0) return <EmptyHint loading={loading} text={emptyText} />;
  return (
    <div className={LIST_SCROLL}>
      {rows.map((it, i) => (
        <RankRow
          key={`${it.symbol}-${it.insider}-${i}`}
          rank={i + 1}
          name={it.name}
          nameSub={it.name === it.symbol ? undefined : it.symbol}
          main={
            <span className="flex items-center gap-1 min-w-0">
              <span className="text-[10px] text-slate-500 truncate max-w-[110px]" title={it.insider}>{it.insider || '--'}</span>
              <span className="text-[9px] text-slate-400 truncate max-w-[80px]" title={it.position}>{it.position || ''}</span>
            </span>
          }
          right={
            <span className={`text-[11px] font-extrabold font-mono whitespace-nowrap flex-shrink-0 ${TONE[tone].text}`}>
              US$ {fmtInt(it.value_yi)}亿
            </span>
          }
        />
      ))}
    </div>
  );
}

/** 机构增减持榜单（增减金额 / 增减肥家数） */
function InstitutionalList({
  rows, emptyText, loading,
}: { rows: UsInstitutionalRow[]; emptyText: string; loading?: boolean }) {
  if (rows.length === 0) return <EmptyHint loading={loading} text={emptyText} />;
  return (
    <div className={LIST_SCROLL}>
      {rows.map((it, i) => (
        <RankRow
          key={it.symbol}
          rank={i + 1}
          name={it.name}
          nameSub={it.name === it.symbol ? undefined : it.symbol}
          main={
            <span className="text-[9px] font-mono text-slate-400 whitespace-nowrap">
              增 {fmtInt(it.increased)} · 减 {fmtInt(it.decreased)} 家
            </span>
          }
          right={
            <div className="flex flex-col items-end gap-0.5 flex-shrink-0">
              <NumText value={it.delta_value_yi} digits={2} suffix=" 亿" className="text-[11px]" />
              <span className="text-[9px] font-mono text-slate-400 whitespace-nowrap">头部机构 {fmtInt(it.holders)} 家</span>
            </div>
          }
        />
      ))}
    </div>
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
      getInsiderMovers(90, 20),
      getInstitutionalHolders(30),
      getDividendCalendar(60, 40),
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

  const buyRows = insider?.top_buys ?? [];
  const sellRows = insider?.top_sells ?? [];
  const divItems = dividends?.items ?? [];
  const splitItems = (splits?.items ?? []).slice(0, 8);

  return (
    <div className="flex flex-col gap-2.5">
      {/* 面板标题 */}
      <div className="rounded-2xl border border-blue-100/70 bg-gradient-to-r from-blue-50/90 via-sky-50/70 to-white px-4 py-2.5 flex items-center justify-between gap-3">
        <div className="flex items-center gap-2 min-w-0">
          <Wallet className="w-4 h-4 text-blue-600 flex-shrink-0" />
          <span className="text-sm font-extrabold text-slate-800 whitespace-nowrap">资金与筹码</span>
          <span className="text-[11px] text-slate-500 truncate">
            内部人买卖（SEC Form 4）/ 机构持仓（13F）/ 除息与拆股事件
          </span>
        </div>
        <DateBadge label="数据" date={insider?.as_of || dividends?.as_of} />
      </div>

      {/* 内部人交易榜 */}
      <SectionCard
        title={
          <span className="flex items-center gap-1.5">
            <Building2 className="w-3.5 h-3.5 text-blue-600" />
            内部人交易榜（高管 / 董事 / 大股东）
          </span>
        }
        extra={
          <span className="text-[10px] font-mono text-slate-400 whitespace-nowrap">
            近 {insider?.days ?? 90} 天 · 按金额排序
          </span>
        }
      >
        <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
          <MetricTile label="买入笔数" value={fmtInt(insider?.buy_count)} tone="red" />
          <MetricTile label="卖出笔数" value={fmtInt(insider?.sell_count)} tone="green" />
          <MetricTile label="买入金额" value={`US$ ${fmtInt(insider?.buy_amount_yi)} 亿`} tone="red" />
          <MetricTile label="卖出金额" value={`US$ ${fmtInt(insider?.sell_amount_yi)} 亿`} tone="green" />
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          <div className="flex flex-col gap-1">
            <ListHeader icon={<ArrowUpRight className="w-3 h-3 text-red-500" />} label="买入榜" tone="red" extra={`Top ${buyRows.length}`} />
            <InsiderList rows={buyRows} tone="red" emptyText="近 90 天无内部人买入记录" loading={loading} />
          </div>
          <div className="flex flex-col gap-1">
            <ListHeader icon={<ArrowDownRight className="w-3 h-3 text-green-500" />} label="卖出榜" tone="green" extra={`Top ${sellRows.length}`} />
            <InsiderList rows={sellRows} tone="green" emptyText="近 90 天无内部人卖出记录" loading={loading} />
          </div>
        </div>

        <p className="text-[9px] text-slate-400 leading-relaxed">
          只统计 Purchase（买入）与 Sale（卖出）两类交易；授予（Grant/Award）、行权（Exercise）、
          赠与属薪酬事件，已排除。同一人同一标的的多笔交易已合并后再排序。
        </p>
      </SectionCard>

      <div className="grid grid-cols-1 xl:grid-cols-2 gap-2.5 items-start">
        {/* 机构持仓 */}
        <SectionCard
          title={
            <span className="flex items-center gap-1.5">
              <Building2 className="w-3.5 h-3.5 text-blue-600" />
              机构持仓变动（13F）
            </span>
          }
          extra={
            <span className="flex items-center gap-2">
              <span className="text-[10px] font-mono text-slate-400 whitespace-nowrap">覆盖 {fmtInt(institutional?.coverage)} 只</span>
              <DateBadge label="13F 披露日" date={institutional?.report_date} />
            </span>
          }
        >
          <div className="grid grid-cols-2 gap-2">
            <MetricTile label="机构持股中位占比" value={pctOrDash(institutional?.institutions_pct_median)} />
            <MetricTile label="内部人持股中位占比" value={pctOrDash(institutional?.insiders_pct_median, 2)} tone="red" />
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            <div className="flex flex-col gap-1">
              <ListHeader icon={<ArrowUpRight className="w-3 h-3 text-red-500" />} label="增持榜" tone="red" />
              <InstitutionalList rows={institutional?.top_increases ?? []} emptyText="暂无增持记录" loading={loading} />
            </div>
            <div className="flex flex-col gap-1">
              <ListHeader icon={<ArrowDownRight className="w-3 h-3 text-green-500" />} label="减持榜" tone="green" />
              <InstitutionalList rows={institutional?.top_decreases ?? []} emptyText="暂无减持记录" loading={loading} />
            </div>
          </div>

          <div className="flex items-start gap-1.5 px-1">
            <Info className="w-3 h-3 text-slate-300 mt-0.5 flex-shrink-0" />
            <p className="text-[9px] text-slate-400 leading-relaxed">
              13F 按季度披露、通常滞后一个季度以上，只反映机构多头持仓快照（不含空头与衍生品）；
              增减金额按机构自身持股变化率还原，为估算值。
            </p>
          </div>
        </SectionCard>

        <div className="flex flex-col gap-2.5">
          {/* 除息日历 */}
          <SectionCard
            title={
              <span className="flex items-center gap-1.5">
                <CalendarClock className="w-3.5 h-3.5 text-blue-600" />
                除息日历（未来 {dividends?.days ?? 60} 天）
              </span>
            }
            extra={
              <span className="flex items-center gap-2">
                <span className="text-[10px] font-mono text-slate-400 whitespace-nowrap">共 {fmtInt(dividends?.total)} 家</span>
                <DateBadge label="数据" date={dividends?.as_of} />
              </span>
            }
          >
            {divItems.length === 0 ? (
              <EmptyHint loading={loading} text="未来 60 天内暂无披露除息" />
            ) : (
              <div className={LIST_SCROLL}>
                {divItems.map((it, i) => (
                  <RankRow
                    key={`${it.symbol}-${it.ex_dividend_date}`}
                    rank={i + 1}
                    name={it.name}
                    nameSub={it.name === it.symbol ? undefined : it.symbol}
                    main={
                      <span className="flex items-center gap-1.5 flex-shrink-0">
                        <span className="text-[9px] font-mono text-amber-600 bg-amber-50 border border-amber-100 rounded px-1 py-0.5 whitespace-nowrap">
                          {(it.ex_dividend_date || '').slice(5)}
                        </span>
                        <span className="text-[10px] font-extrabold text-slate-400 whitespace-nowrap">{daysLabel(it.days_until)}</span>
                      </span>
                    }
                    right={
                      <div className="flex flex-col items-end gap-0.5 flex-shrink-0">
                        <span className="text-[10px] font-mono text-slate-500 whitespace-nowrap">{it.dividend_date || '--'}</span>
                        <span className="text-[9px] font-bold text-slate-400">派息日</span>
                      </div>
                    }
                  />
                ))}
              </div>
            )}
          </SectionCard>

          {/* 近期拆股 */}
          <SectionCard
            title={
              <span className="flex items-center gap-1.5">
                <Scissors className="w-3.5 h-3.5 text-blue-600" />
                近期拆股（近 {splits?.days ?? 365} 天）
              </span>
            }
            extra={<span className="text-[10px] font-mono text-slate-400 whitespace-nowrap">前 {splitItems.length} 条</span>}
          >
            {splitItems.length === 0 ? (
              <EmptyHint loading={loading} text="近期无拆股事件" />
            ) : (
              <div className="flex flex-col">
                {splitItems.map((it, i) => (
                  <RankRow
                    key={`${it.symbol}-${it.split_date}`}
                    rank={i + 1}
                    name={it.name}
                    nameSub={it.name === it.symbol ? undefined : it.symbol}
                    main={
                      <span className="text-[9px] font-mono text-slate-500 bg-slate-100 rounded px-1 py-0.5 whitespace-nowrap">
                        {it.split_date}
                      </span>
                    }
                    right={
                      <span
                        className="text-[11px] font-extrabold font-mono text-blue-700 whitespace-nowrap flex-shrink-0"
                        title={`每 1 股拆为 ${fmtNum(it.ratio)} 股`}
                      >
                        {fmtNum(it.ratio)} : 1
                      </span>
                    }
                  />
                ))}
              </div>
            )}
            <p className="text-[9px] text-slate-400 leading-relaxed">
              拆股（比例为 新:旧）会污染未复权日线的跨期收益，出现异常区间收益时可在此排查。
            </p>
          </SectionCard>
        </div>
      </div>
    </div>
  );
};
