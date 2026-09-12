/** 美股财报季面板 —— 财报日历 / 超预期榜 / 盈利预期修正
 *
 * 美股是事件驱动市场：财报日历决定调仓窗口，超预期幅度是 PEAD
 * （财报后漂移）策略的原始信号，预期修正反映分析师的前瞻共识。
 *
 * 数值口径：EPS 为美元/股；营收为美元原始值（展示转亿）；
 * `surprise_pct` / `eps_growth_pct` / `revenue_growth_pct` 后端均已换算为百分数。
 */

import React, { useEffect, useState } from 'react';
import { CalendarDays, FileText, Info, LineChart, TrendingUp } from 'lucide-react';
import {
  getEarningsCalendar,
  getEarningsRevisions,
  getEarningsSurprises,
} from '../services/api';
import type {
  UsEarningsCalendar,
  UsEarningsRevisions,
  UsEarningsSurprises,
} from '../types';
import {
  DateBadge,
  EmptyHint,
  PctText,
  SectionCard,
  fmtInt,
  fmtYi,
} from '../../market-analysis-shared/ui';

/** 可空数值 → 固定小数位文本（null / NaN 一律 '--'，绝不参与运算或 toFixed） */
function fmtNum(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return '--';
  return Number(value).toFixed(digits);
}

/** 距财报日的自然语言描述 */
function daysLabel(days: number | null | undefined): string {
  if (days === null || days === undefined || !Number.isFinite(Number(days))) return '--';
  const n = Number(days);
  return n <= 0 ? '今日' : `${n} 天后`;
}

/** 三块列表统一列头样式（紧凑看盘密度） */
const HEAD =
  'grid gap-1 px-1 py-[3px] text-[9px] font-bold text-slate-400 border-b border-slate-200 bg-slate-50/60';
const ROW = 'grid gap-1 px-1 py-[3px] text-[10px] items-center border-b border-slate-50 last:border-0';
const SCROLL = 'overflow-y-auto';

const CAL_GRID = 'grid-cols-[14px_1fr_34px_32px_40px_52px]';
const SUR_GRID = 'grid-cols-[14px_1fr_34px_42px_42px_50px]';
const REV_GRID = 'grid-cols-[14px_1fr_38px_34px]';

export const UsEarningsPanel: React.FC = () => {
  const [calendar, setCalendar] = useState<UsEarningsCalendar | null>(null);
  const [surprises, setSurprises] = useState<UsEarningsSurprises | null>(null);
  const [revisions, setRevisions] = useState<UsEarningsRevisions | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    Promise.all([
      getEarningsCalendar(60, 60),
      getEarningsSurprises(60, 120),
      getEarningsRevisions(40),
    ])
      .then(([cal, sur, rev]) => {
        if (!alive) return;
        setCalendar(cal);
        setSurprises(sur);
        setRevisions(rev);
      })
      .catch(() => undefined)
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, []);

  const calItems = calendar?.items ?? [];
  const surItems = surprises?.items ?? [];
  const revItems = revisions?.items ?? [];

  return (
    <div className="flex flex-col gap-1.5">
      {/* 面板标题 */}
      <div className="rounded-2xl border border-blue-100/70 bg-gradient-to-r from-blue-50/90 via-sky-50/70 to-white px-4 py-2 flex items-center justify-between gap-3">
        <div className="flex items-center gap-2 min-w-0">
          <FileText className="w-4 h-4 text-blue-600 flex-shrink-0" />
          <span className="text-sm font-extrabold text-slate-800 whitespace-nowrap">财报季</span>
          <span className="text-[11px] text-slate-500 truncate">
            美股是事件驱动市场：财报日历定调仓窗口，超预期幅度是 PEAD 信号，预期修正看分析师共识
          </span>
        </div>
        <span className="text-[10px] font-mono text-slate-400 whitespace-nowrap hidden md:inline">
          {fmtInt(calItems.length + surItems.length + revItems.length)} 条记录
        </span>
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-6 gap-1.5">
        {/* 财报日历 */}
        <div className="xl:col-span-2 min-w-0 h-full">
          <SectionCard
            className="!p-2.5 gap-1.5 h-full"
            title={
              <span className="flex items-center gap-1.5">
                <CalendarDays className="w-3.5 h-3.5 text-blue-600" />
                财报日历
                <span className="text-[9px] font-normal text-slate-400">
                  未来 {calendar?.days ?? 30} 天
                </span>
              </span>
            }
            extra={
              <span className="flex items-center gap-2">
                <span className="text-[9px] font-mono text-slate-400 whitespace-nowrap">
                  共 {fmtInt(calendar?.total)} 家
                </span>
                <DateBadge label="数据" date={calendar?.as_of} />
              </span>
            }
          >
            {calItems.length === 0 ? (
              <EmptyHint loading={loading} />
            ) : (
              <div className={`flex flex-col flex-1 min-h-0 ${SCROLL}`}>
                <div className={`${HEAD} ${CAL_GRID}`}>
                  <span>#</span>
                  <span>标的</span>
                  <span>日期</span>
                  <span className="text-right">距今</span>
                  <span className="text-right">EPS预期</span>
                  <span className="text-right">营收预期</span>
                </div>
                {calItems.map((it, i) => (
                  <div key={`${it.symbol}-${it.earnings_date}`} className={`${ROW} ${CAL_GRID}`}>
                    <span
                      className={`text-[9px] font-extrabold ${
                        i < 3 ? 'text-blue-600' : 'text-slate-400'
                      }`}
                    >
                      {i + 1}
                    </span>
                    <span className="flex items-center gap-1 min-w-0">
                      <span className="font-bold text-slate-800 truncate" title={it.name}>
                        {it.name}
                      </span>
                      {it.name !== it.symbol && (
                        <span className="font-mono text-[9px] text-slate-400 flex-shrink-0">
                          {it.symbol}
                        </span>
                      )}
                    </span>
                    <span className="font-mono text-[9px] text-slate-500 whitespace-nowrap">
                      {(it.earnings_date || '').slice(5)}
                    </span>
                    <span
                      className={`text-right text-[9px] font-extrabold whitespace-nowrap ${
                        it.days_until <= 3 ? 'text-blue-600' : 'text-slate-400'
                      }`}
                    >
                      {it.days_until <= 3 ? '临披露' : daysLabel(it.days_until)}
                    </span>
                    <span
                      className="text-right font-mono text-slate-700 whitespace-nowrap"
                      title={`EPS 预期区间 ${fmtNum(it.eps_low)} ~ ${fmtNum(it.eps_high)}`}
                    >
                      {fmtNum(it.eps_avg)}
                    </span>
                    <span
                      className="text-right font-mono text-slate-500 whitespace-nowrap"
                      title={`营收预期区间 US$ ${fmtYi(it.revenue_low)} ~ ${fmtYi(it.revenue_high)} 亿`}
                    >
                      {fmtYi(it.revenue_avg)}
                      <span className="text-[9px] text-slate-400">亿</span>
                    </span>
                  </div>
                ))}
              </div>
            )}
            <p className="text-[9px] text-slate-400 leading-relaxed">
              按披露日期由近及远排序；「临披露」= 距今 3 天内，是 PEAD 的实际建仓/调仓窗口。
            </p>
          </SectionCard>
        </div>

        {/* 超预期榜 */}
        <div className="xl:col-span-2 min-w-0 h-full">
          <SectionCard
            className="!p-2.5 gap-1.5 h-full"
            title={
              <span className="flex items-center gap-1.5">
                <TrendingUp className="w-3.5 h-3.5 text-blue-600" />
                超预期榜
                <span className="text-[9px] font-normal text-slate-400">实际 vs 预期 EPS</span>
              </span>
            }
            extra={
              <span className="flex items-center gap-2">
                <span className="text-[9px] font-mono text-slate-400 whitespace-nowrap">
                  近 {surprises?.lookback_days ?? 120} 天
                </span>
                <DateBadge label="数据" date={surprises?.as_of} />
              </span>
            }
          >
            {surItems.length === 0 ? (
              <EmptyHint loading={loading} />
            ) : (
              <div className={`flex flex-col flex-1 min-h-0 ${SCROLL}`}>
                <div className={`${HEAD} ${SUR_GRID}`}>
                  <span>#</span>
                  <span>标的</span>
                  <span>财报日</span>
                  <span className="text-right">实际</span>
                  <span className="text-right">预期</span>
                  <span className="text-right">超预期</span>
                </div>
                {surItems.map((it, i) => (
                  <div key={it.symbol} className={`${ROW} ${SUR_GRID}`}>
                    <span
                      className={`text-[9px] font-extrabold ${
                        i < 3 ? 'text-blue-600' : 'text-slate-400'
                      }`}
                    >
                      {i + 1}
                    </span>
                    <span className="flex items-center gap-1 min-w-0">
                      <span className="font-bold text-slate-800 truncate" title={it.name}>
                        {it.name}
                      </span>
                      {it.name !== it.symbol && (
                        <span className="font-mono text-[9px] text-slate-400 flex-shrink-0">
                          {it.symbol}
                        </span>
                      )}
                    </span>
                    <span className="font-mono text-[9px] text-slate-500 whitespace-nowrap">
                      {(it.report_date || '').slice(5)}
                    </span>
                    <span className="text-right font-mono text-slate-700 whitespace-nowrap">
                      {fmtNum(it.reported_eps)}
                    </span>
                    <span className="text-right font-mono text-slate-500 whitespace-nowrap">
                      {fmtNum(it.estimate_eps)}
                    </span>
                    <span className="text-right whitespace-nowrap">
                      <PctText value={it.surprise_pct} className="text-[10px]" />
                    </span>
                  </div>
                ))}
              </div>
            )}
            <p className="text-[9px] text-slate-400 leading-relaxed">
              超预期幅度 =（实际 EPS − 预期 EPS）/ |预期 EPS|。200%+ 的极端值源于源数据的
              预期列本身偏低（已用两张独立来源交叉验证），照实显示、不作裁剪或修正。
            </p>
          </SectionCard>
        </div>

        {/* 盈利预期修正 */}
        <div className="xl:col-span-2 min-w-0 h-full">
          <SectionCard
            className="!p-2.5 gap-1.5 h-full"
            title={
              <span className="flex items-center gap-1.5">
                <LineChart className="w-3.5 h-3.5 text-blue-600" />
                盈利预期修正
                <span className="text-[9px] font-normal text-slate-400">当季（0q）同比</span>
              </span>
            }
            extra={
              <span className="text-[9px] font-mono text-slate-400 whitespace-nowrap">
                共 {fmtInt(revItems.length)} 家
              </span>
            }
          >
            {revItems.length === 0 ? (
              <EmptyHint loading={loading} />
            ) : (
              <div className={`flex flex-col flex-1 min-h-0 ${SCROLL}`}>
                <div className={`${HEAD} ${REV_GRID}`}>
                  <span>#</span>
                  <span>标的 / 覆盖</span>
                  <span className="text-right">EPS 预期</span>
                  <span className="text-right">EPS / 营收 同比</span>
                </div>
                {revItems.map((it, i) => (
                  <div key={it.symbol} className={`${ROW} ${REV_GRID}`}>
                    <span
                      className={`text-[9px] font-extrabold ${
                        i < 3 ? 'text-blue-600' : 'text-slate-400'
                      }`}
                    >
                      {i + 1}
                    </span>
                    <span className="flex items-center gap-1 min-w-0">
                      <span className="font-bold text-slate-800 truncate" title={it.name}>
                        {it.name}
                      </span>
                      <span className="font-mono text-[9px] text-slate-400 flex-shrink-0">
                        {it.symbol}
                      </span>
                      <span
                        className="text-[9px] font-mono text-slate-400 flex-shrink-0 whitespace-nowrap"
                        title="覆盖分析师家数"
                      >
                        {fmtInt(it.analyst_count)}家
                      </span>
                    </span>
                    <span className="text-right font-mono text-slate-700 whitespace-nowrap">
                      {fmtNum(it.eps_avg)}
                    </span>
                    <span className="flex flex-col items-end gap-px whitespace-nowrap">
                      <span className="flex items-center gap-1">
                        <span className="text-[8px] font-bold text-slate-400">EPS</span>
                        <PctText value={it.eps_growth_pct} className="text-[10px]" />
                      </span>
                      <span className="flex items-center gap-1">
                        <span className="text-[8px] font-bold text-slate-400">营收</span>
                        <PctText value={it.revenue_growth_pct} className="text-[9px]" />
                      </span>
                    </span>
                  </div>
                ))}
              </div>
            )}
            <p className="text-[9px] text-slate-400 leading-relaxed">
              增速为当前季度（0q）分析师一致预期的同比增速；营收增速缺失显示 --。
              「N家」为覆盖该标的的分析师家数。
            </p>
          </SectionCard>
        </div>
      </div>

      {/* 口径脚注 */}
      <div className="flex items-start gap-1.5 px-1">
        <Info className="w-3 h-3 text-slate-300 mt-0.5 flex-shrink-0" />
        <p className="text-[9px] text-slate-400 leading-relaxed">
          标的池为标普 500 + 纳指补充（约 517 只），非全市场；EPS 与营收均为分析师一致预期快照，
          随财报披露滚动更新。日历按披露日期由近及远排序。
        </p>
      </div>
    </div>
  );
};
