/** 美股分析师动向 —— 评级升降级流水 / 目标价隐含空间 / 全池评级分布（三端点并行取数）
 *
 * - upgrades：direction 由后端按档位词粗判（up/down/neutral）；目标价 current/prior
 *   均可为 null → 任一端缺失就不画箭头；targets：隐含空间 > ±200% 的离群值后端已
 *   剔除，快照无日期故以 close_date 为准；ratings：bull_ratio = 看多档占比
 */

import React, { useEffect, useState } from 'react';
import { Users, Target, ArrowUpRight, ArrowDownRight, Minus, ArrowRight } from 'lucide-react';
import { getAnalystRatings, getAnalystTargets, getAnalystUpgrades } from '../services/api';
import type { UsAnalystRatings, UsAnalystRatingRow, UsAnalystTargets, UsAnalystUpgrades } from '../types';
import { SectionCard, EmptyHint, PctText, DateBadge, fmtInt } from '../../market-analysis-shared/ui';

/** 三条榜单统一列头 / 数据行样式（紧凑看盘密度） */
const HEAD =
  'grid gap-1 px-1 py-[3px] text-[9px] font-bold text-slate-400 border-b border-slate-200 bg-slate-50/60';
const ROW = 'grid gap-1 px-1 py-[3px] text-[10px] items-center border-b border-slate-50 last:border-0';
const SCROLL = 'overflow-y-auto';

const UP_GRID = 'grid-cols-[14px_1fr_56px_50px]';
const TARGET_GRID = 'grid-cols-[14px_1fr_46px_52px_48px]';
const RATED_GRID = 'grid-cols-[14px_1fr_30px_26px_26px]';

/** 评级方向 → 文案/配色（红=上调、绿=下调、灰=维持，与红涨绿跌一致） */
const DIRECTION_STYLE: Record<string, { chip: string; label: string }> = {
  up: { chip: 'bg-red-50 text-red-600 border-red-100', label: '上调' },
  down: { chip: 'bg-green-50 text-green-600 border-green-100', label: '下调' },
  neutral: { chip: 'bg-slate-100 text-slate-500 border-slate-200', label: '维持' },
};

/** 目标价（美元）→ 文案；null / 非法值返回 null，调用方据此跳过箭头与数字 */
function priceLabel(v: number | null | undefined): string | null {
  if (v === null || v === undefined || Number.isNaN(v)) return null;
  return `$${Number(v).toLocaleString('zh-CN', { maximumFractionDigits: 2 })}`;
}

/** 已是百分数的数值安全格式化（网络数据可能缺字段，不能直接 toFixed） */
function pctLabel(v: number | null | undefined, digits = 1): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '--';
  return `${Number(v).toFixed(digits)}%`;
}

/** 夹在 0-100 的百分比（条形宽度用），非法值记 0 */
function clampPct(v: number | null | undefined): number {
  if (v === null || v === undefined || Number.isNaN(v)) return 0;
  return Math.min(100, Math.max(0, Number(v)));
}

/** 家数压缩显示（≥1 万折成「x.x万」），保证窄列不换行 */
function cCount(n: number | null | undefined): string {
  if (n === null || n === undefined || !Number.isFinite(Number(n))) return '--';
  const v = Number(n);
  if (Math.abs(v) < 10000) return String(v);
  return `${(v / 10000).toFixed(1)}万`;
}

type RatingKey = 'strong_buy' | 'buy' | 'hold' | 'sell' | 'strong_sell';

/** 五档评级：红 → 绿渐变（看多在前，看空在后） */
const RATING_BUCKETS: Array<{ key: RatingKey; label: string; cls: string }> = [
  { key: 'strong_buy', label: '强烈买入', cls: 'bg-red-600' },
  { key: 'buy', label: '买入', cls: 'bg-red-400' },
  { key: 'hold', label: '持有', cls: 'bg-amber-400' },
  { key: 'sell', label: '卖出', cls: 'bg-green-400' },
  { key: 'strong_sell', label: '强烈卖出', cls: 'bg-green-600' },
];

/** 看多 / 看空榜单（各取前 12，含买/持/卖家数明细） */
const RatedList: React.FC<{ title: string; rows: UsAnalystRatingRow[]; tone: 'red' | 'green' }> = ({
  title, rows, tone,
}) => {
  const bar = tone === 'red' ? 'bg-red-500' : 'bg-green-500';
  const text = tone === 'red' ? 'text-red-600' : 'text-green-600';
  return (
    <div className="rounded-lg border border-slate-100 overflow-hidden">
      <div className={`flex items-center justify-between px-2 py-1 text-[10px] font-extrabold ${tone === 'red' ? 'bg-red-50 text-red-700' : 'bg-green-50 text-green-700'}`}>
        <span>{title}</span>
        <span className="text-[9px] font-bold text-slate-400">看多占比 · 买/持/卖家数</span>
      </div>
      {rows.length === 0 ? (
        <EmptyHint text="暂无数据" />
      ) : (
        <>
          <div className={`${HEAD} ${RATED_GRID}`}>
            <span>#</span>
            <span>标的</span>
            <span className="text-right">占比</span>
            <span className="text-right">买</span>
            <span className="text-right">持/卖</span>
          </div>
          {rows.map((r, i) => (
            <div key={r.symbol} className={`${ROW} ${RATED_GRID}`}>
              <span className="text-[9px] font-extrabold text-slate-300">{i + 1}</span>
              <span className="flex items-center gap-1 min-w-0">
                <span className="font-bold text-slate-800 truncate" title={r.name}>{r.name}</span>
                <span className="text-[9px] font-mono text-slate-400 truncate flex-shrink-0">{r.symbol}</span>
                <span className="w-8 h-1 rounded-full bg-slate-100 overflow-hidden flex-shrink-0">
                  <span className={`block h-full rounded-full ${bar}`} style={{ width: `${clampPct(r.bull_ratio)}%` }} />
                </span>
              </span>
              <span className={`text-right font-mono font-extrabold whitespace-nowrap ${text}`}>
                {pctLabel(r.bull_ratio)}
              </span>
              <span
                className="text-right font-mono text-[9px] text-slate-600 whitespace-nowrap"
                title={`强烈买入 ${fmtInt(r.strong_buy)} · 买入 ${fmtInt(r.buy)} · 持有 ${fmtInt(r.hold)} · 卖出 ${fmtInt(r.sell)} · 强烈卖出 ${fmtInt(r.strong_sell)}`}
              >
                {cCount((r.strong_buy ?? 0) + (r.buy ?? 0))}
              </span>
              <span
                className="text-right font-mono text-[9px] text-slate-400 whitespace-nowrap"
                title={`持有 ${fmtInt(r.hold)} · 卖出 ${fmtInt(r.sell)} · 强烈卖出 ${fmtInt(r.strong_sell)}`}
              >
                {cCount(r.hold)}/{cCount((r.sell ?? 0) + (r.strong_sell ?? 0))}
              </span>
            </div>
          ))}
        </>
      )}
    </div>
  );
};

export const UsAnalystPanel: React.FC = () => {
  const [upgrades, setUpgrades] = useState<UsAnalystUpgrades | null>(null);
  const [targets, setTargets] = useState<UsAnalystTargets | null>(null);
  const [ratings, setRatings] = useState<UsAnalystRatings | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    Promise.all([getAnalystUpgrades(30, 60), getAnalystTargets(30), getAnalystRatings()])
      .then(([u, t, r]) => {
        if (!alive) return;
        setUpgrades(u);
        setTargets(t);
        setRatings(r);
      })
      .catch(() => undefined)
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, []);

  const upgradeItems = upgrades?.items ?? [];
  const targetItems = targets?.items ?? [];
  const ratingTotal = Math.max(ratings?.total_coverage ?? 0, 0);
  const share = (key: RatingKey): number => {
    const count = ratings ? Number(ratings[key]) : NaN;
    return ratingTotal > 0 && Number.isFinite(count) ? (count / ratingTotal) * 100 : 0;
  };

  return (
    <div className="flex flex-col gap-1.5">
      {/* 全池评级分布 */}
      <SectionCard
        className="!p-2.5 gap-1.5"
        title={
          <span className="flex items-center gap-1.5">
            <Users className="w-3.5 h-3.5 text-blue-600" />全池评级分布
            <span className="text-[9px] font-normal text-slate-400">五档占比 · 看多/看空前 12</span>
          </span>
        }
        extra={
          <span className="flex items-center gap-2">
            <span className="px-2 py-0.5 rounded-full bg-blue-50 text-blue-700 text-[10px] font-extrabold border border-blue-100 whitespace-nowrap">
              覆盖 {fmtInt(ratings?.total_coverage)} 只
            </span>
            <DateBadge label="快照" date={ratings?.as_of} />
          </span>
        }
      >
        {!ratings ? (
          <EmptyHint loading={loading} />
        ) : (
          <>
            <div className="flex h-2 w-full rounded-full overflow-hidden bg-slate-100">
              {RATING_BUCKETS.map((b) => (
                <span
                  key={b.key}
                  className={b.cls}
                  style={{ width: `${share(b.key)}%` }}
                  title={`${b.label} ${fmtInt(ratings[b.key])} 只（${pctLabel(share(b.key))}）`}
                />
              ))}
            </div>
            <div className="flex flex-wrap items-center gap-x-3 gap-y-0.5">
              {RATING_BUCKETS.map((b) => (
                <span key={b.key} className="flex items-center gap-1 text-[10px] font-bold text-slate-500">
                  <span className={`w-2 h-2 rounded-sm ${b.cls}`} />
                  {b.label}
                  <span className="font-mono text-slate-700">{fmtInt(ratings[b.key])}</span>
                  <span className="font-mono text-slate-400">{pctLabel(share(b.key))}</span>
                </span>
              ))}
              <span className="ml-auto flex items-baseline gap-1.5">
                <span className="text-[10px] font-bold text-slate-400">看多占比</span>
                <span className="text-sm font-extrabold font-mono text-red-600">
                  {pctLabel(ratings.bull_ratio)}
                </span>
              </span>
            </div>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-1.5">
              <RatedList title="看多前列 · Top 12" rows={ratings.top_rated.slice(0, 12)} tone="red" />
              <RatedList title="看空前列 · Top 12" rows={ratings.bottom_rated.slice(0, 12)} tone="green" />
            </div>
          </>
        )}
      </SectionCard>

      <div className="grid grid-cols-1 xl:grid-cols-2 gap-1.5 items-start">
        {/* 评级升降级流水 */}
        <SectionCard
          className="!p-2.5 gap-1.5"
          title={
            <span className="flex items-center gap-1.5">
              <ArrowUpRight className="w-3.5 h-3.5 text-blue-600" />评级升降级流水
              <span className="text-[9px] font-normal text-slate-400">日期 · 标的 · 机构 · 评级/目标价变化</span>
            </span>
          }
          extra={
            <span className="text-[9px] font-mono text-slate-400 whitespace-nowrap">
              近 {upgrades?.days ?? 30} 天 · {fmtInt(upgrades?.total)} 条
            </span>
          }
        >
          {upgradeItems.length === 0 ? (
            <EmptyHint loading={loading} />
          ) : (
            <div className={`flex flex-col max-h-[600px] ${SCROLL}`}>
              <div className={`${HEAD} ${UP_GRID} sticky top-0 z-10 bg-white/95 backdrop-blur`}>
                <span>#</span>
                <span>标的 · 机构 · 评级变化</span>
                <span className="text-right">方向</span>
                <span className="text-right">目标价</span>
              </div>
              {upgradeItems.map((it, i) => {
                const style = DIRECTION_STYLE[it.direction] ?? DIRECTION_STYLE.neutral;
                const cur = priceLabel(it.current_price_target);
                const prior = priceLabel(it.prior_price_target);
                const grades =
                  [it.from_grade, it.to_grade].map((g) => (g ?? '').trim()).filter(Boolean).join(' → ') || '--';
                return (
                  <div key={`${it.symbol}-${it.grade_date}-${it.firm}-${i}`} className={`${ROW} ${UP_GRID}`}>
                    <span className="font-mono text-[9px] text-slate-400 whitespace-nowrap">
                      {(it.grade_date || '').slice(5)}
                    </span>
                    <div className="min-w-0">
                      <div className="flex items-center gap-1 min-w-0">
                        <span className="font-bold text-slate-800 truncate" title={it.name}>{it.name}</span>
                        <span className="text-[9px] font-mono text-slate-400 flex-shrink-0">{it.symbol}</span>
                      </div>
                      <div className="flex items-center gap-1 text-[9px] text-slate-400 min-w-0">
                        <span className="truncate" title={it.firm}>{it.firm || '机构未标注'}</span>
                        <span className="text-slate-300">·</span>
                        <span className="font-mono truncate" title={grades}>{grades}</span>
                      </div>
                    </div>
                    <span className="flex justify-end">
                      <span
                        className={`flex items-center gap-0.5 px-1 py-px rounded border text-[9px] font-extrabold whitespace-nowrap ${style.chip}`}
                        title={`action ${it.action || '未标注'} · 目标价动作 ${it.price_target_action || '未标注'}`}
                      >
                        {it.direction === 'up' ? (
                          <ArrowUpRight className="w-2.5 h-2.5" />
                        ) : it.direction === 'down' ? (
                          <ArrowDownRight className="w-2.5 h-2.5" />
                        ) : (
                          <Minus className="w-2.5 h-2.5" />
                        )}
                        {style.label}
                      </span>
                    </span>
                    {/* 目标价：任一端为 null 就不画箭头 */}
                    <span className="flex flex-col items-end gap-px whitespace-nowrap">
                      {cur === null && prior === null ? (
                        <span className="font-mono text-slate-300">--</span>
                      ) : (
                        <span className="inline-flex items-center justify-end gap-0.5 font-mono text-[9px]">
                          {prior && <span className="text-slate-400 line-through">{prior}</span>}
                          {prior && cur && <ArrowRight className="w-2.5 h-2.5 text-slate-300" />}
                          {cur && <span className="font-extrabold text-slate-800">{cur}</span>}
                        </span>
                      )}
                      {it.price_target_change_pct !== null && (
                        <PctText value={it.price_target_change_pct} className="text-[9px]" />
                      )}
                    </span>
                  </div>
                );
              })}
            </div>
          )}
        </SectionCard>

        {/* 目标价隐含空间 */}
        <SectionCard
          className="!p-2.5 gap-1.5"
          title={
            <span className="flex items-center gap-1.5">
              <Target className="w-3.5 h-3.5 text-blue-600" />目标价隐含空间
              <span className="text-[9px] font-normal text-slate-400">现价 vs 目标均价</span>
            </span>
          }
          extra={<DateBadge label="收盘" date={targets?.close_date} />}
        >
          {targetItems.length === 0 ? (
            <EmptyHint loading={loading} />
          ) : (
            <div className={`flex flex-col max-h-[600px] ${SCROLL}`}>
              <p className="text-[9px] text-slate-400 pb-1">
                按隐含空间降序；后端已剔除 |空间| &gt; 200% 的离群值（退市/并购残留标的）；目标价快照无日期字段，以收盘价日期为准。
              </p>
              <div className={`${HEAD} ${TARGET_GRID} sticky top-0 z-10 bg-white/95 backdrop-blur`}>
                <span>#</span>
                <span>标的</span>
                <span className="text-right">现价</span>
                <span className="text-right">目标均价</span>
                <span className="text-right">距目标价</span>
              </div>
              {targetItems.map((it, i) => (
                <div key={it.symbol} className={`${ROW} ${TARGET_GRID}`}>
                  <span className="text-[9px] font-extrabold text-slate-400">{i + 1}</span>
                  <span className="flex items-center gap-1 min-w-0">
                    <span className="font-bold text-slate-800 truncate" title={it.name}>{it.name}</span>
                    <span className="text-[9px] font-mono text-slate-400 truncate flex-shrink-0">{it.symbol}</span>
                  </span>
                  <span className="text-right font-mono text-slate-600 whitespace-nowrap">{fmtInt(it.close)}</span>
                  <span
                    className="text-right font-mono font-extrabold text-slate-800 whitespace-nowrap"
                    title={`最高 ${fmtInt(it.target_high)} / 最低 ${fmtInt(it.target_low)}`}
                  >
                    {fmtInt(it.target_mean)}
                  </span>
                  <span className="text-right whitespace-nowrap">
                    <PctText value={it.upside_pct} className="text-[10px]" />
                  </span>
                </div>
              ))}
            </div>
          )}
        </SectionCard>
      </div>
    </div>
  );
};
