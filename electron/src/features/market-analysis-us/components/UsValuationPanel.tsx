/** 美股估值主题面板 —— 估值概览 / 市值分层 / 估值三榜（高股息 · 低PE · 低PB）
 *
 * 数据源为 f10 基本面快照（单点截面，含中文名与市值），
 * PE/PB 只取正值参与排名；三个榜单均施加健全性门槛剔除陈旧快照标的（见脚注）。
 */

import React, { useEffect, useState } from 'react';
import { Coins, Info, Layers, LineChart } from 'lucide-react';
import { getSizeTiers, getValuationOverview, getValuationRankings } from '../services/api';
import type {
  UsSizeTiers,
  UsValuationOverview,
  UsValuationRankRow,
  UsValuationRankings,
} from '../types';
import {
  DateBadge,
  EmptyHint,
  SectionCard,
  fmtInt,
} from '../../market-analysis-shared/ui';

type RankKind = 'dividend' | 'pe' | 'pb';

const KIND_OPTIONS: Array<{ id: RankKind; label: string }> = [
  { id: 'dividend', label: '高股息' },
  { id: 'pe', label: '低PE' },
  { id: 'pb', label: '低PB' },
];

/** 可空数值 → 固定小数位文本（null / NaN 一律 '--'，绝不参与运算或 toFixed） */
function fmtNum(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return '--';
  return Number(value).toFixed(digits);
}

/** 可空百分数 → 'x.xx%'；无效值 '--' */
function pctOrDash(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return '--';
  return `${Number(value).toFixed(digits)}%`;
}

/** 榜单主数值（股息榜为百分数，PE/PB 为倍数） */
function rankValueText(row: UsValuationRankRow, kind: RankKind): string {
  return kind === 'dividend' ? pctOrDash(row.value, 2) : fmtNum(row.value);
}

const HEAD =
  'grid gap-1 px-1 py-[3px] text-[9px] font-bold text-slate-400 border-b border-slate-200 bg-slate-50/60';
const ROW = 'grid gap-1 px-1 py-[3px] text-[10px] items-center border-b border-slate-50 last:border-0';

const RANK_GRID = 'grid-cols-[14px_1fr_34px_44px_42px_44px_50px]';
const TIER_GRID = 'grid-cols-[68px_44px_1fr_60px_72px]';

/** 估值概览单行指标条：label / value（/ 次要值） */
function Metric({ label, value, sub }: { label: string; value: React.ReactNode; sub?: string }) {
  return (
    <span className="flex items-baseline gap-1 whitespace-nowrap">
      <span className="text-[9px] font-bold text-slate-400">{label}</span>
      <span className="text-[11px] font-extrabold font-mono text-blue-700">{value}</span>
      {sub && <span className="text-[9px] text-slate-400">{sub}</span>}
    </span>
  );
}

export const UsValuationPanel: React.FC = () => {
  const [overview, setOverview] = useState<UsValuationOverview | null>(null);
  const [tiers, setTiers] = useState<UsSizeTiers | null>(null);
  const [overviewLoading, setOverviewLoading] = useState(true);
  const [kind, setKind] = useState<RankKind>('dividend');
  const [ranking, setRanking] = useState<UsValuationRankings | null>(null);
  const [rankLoading, setRankLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    Promise.all([getValuationOverview(), getSizeTiers()])
      .then(([ov, st]) => {
        if (!alive) return;
        setOverview(ov);
        setTiers(st);
      })
      .catch(() => undefined)
      .finally(() => {
        if (alive) setOverviewLoading(false);
      });
    return () => {
      alive = false;
    };
  }, []);

  useEffect(() => {
    let alive = true;
    setRankLoading(true);
    setRanking(null);
    getValuationRankings(kind, 30)
      .then((d) => {
        if (alive) setRanking(d);
      })
      .catch(() => undefined)
      .finally(() => {
        if (alive) setRankLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [kind]);

  const tierRows = tiers?.tiers ?? [];
  const rankItems = ranking?.items ?? [];

  return (
    <div className="flex flex-col gap-1.5">
      {/* 面板标题 */}
      <div className="rounded-2xl border border-blue-100/70 bg-gradient-to-r from-blue-50/90 via-sky-50/70 to-white px-4 py-2 flex items-center justify-between gap-3">
        <div className="flex items-center gap-2 min-w-0">
          <Coins className="w-4 h-4 text-blue-600 flex-shrink-0" />
          <span className="text-sm font-extrabold text-slate-800 whitespace-nowrap">估值主题</span>
          <span className="text-[11px] text-slate-500 truncate">
            标的池估值分位与市值分层，高股息 / 低 PE / 低 PB 三榜切换
          </span>
        </div>
        <span className="text-[10px] font-mono text-slate-400 whitespace-nowrap hidden md:inline">
          标的池 {fmtInt(overview?.coverage)} 只
        </span>
      </div>

      {/* 2 栏：左列 = 概览 + 市值分层（堆叠），右列 = 估值三榜。
          原先三榜独占整行（1696px），表格列只用掉一半宽度，右侧大片空白。 */}
      <div className="grid grid-cols-1 xl:grid-cols-2 gap-1.5">
        <div className="flex flex-col gap-1.5">
          {/* 估值概览：一行紧凑指标条 */}
        <SectionCard
          className="!p-2.5 gap-1.5 h-full"
          title={
            <span className="flex items-center gap-1.5">
              <LineChart className="w-3.5 h-3.5 text-blue-600" />
              估值概览
              <span className="text-[9px] font-normal text-slate-400">标的池中位数</span>
            </span>
          }
          extra={<DateBadge label="数据" date={overview?.as_of} />}
        >
          {!overview && overviewLoading ? (
            <EmptyHint loading />
          ) : (
            <div className="rounded-lg border border-blue-100 bg-blue-50/60 px-2 py-1 flex flex-wrap items-center gap-x-3 gap-y-1">
              <Metric label="覆盖" value={`${fmtInt(overview?.coverage)} 只`} />
              <Metric label="PE中位" value={fmtNum(overview?.pe_median)} />
              <Metric
                label="PE四分位"
                value={`${fmtNum(overview?.pe_p25)} ~ ${fmtNum(overview?.pe_p75)}`}
                sub="P25~P75"
              />
              <Metric label="PB中位" value={fmtNum(overview?.pb_median)} />
              <Metric label="股息率中位" value={pctOrDash(overview?.dividend_yield_median, 2)} />
              <Metric
                label="分红标的"
                value={`${fmtInt(overview?.dividend_payers)} 只`}
                sub="≥0.5%"
              />
            </div>
          )}
          <p className="text-[9px] text-slate-400 leading-relaxed">
            中位数只统计有效正值（PE ≥ 3、PB ≥ 0.3、股息率 ≥ 0.5%），
            避免亏损公司的负值拉偏分位。
          </p>
        </SectionCard>

        {/* 市值分层 */}
        <SectionCard
          className="!p-2.5 gap-1.5 h-full"
          title={
            <span className="flex items-center gap-1.5">
              <Layers className="w-3.5 h-3.5 text-blue-600" />
              市值分层
              <span className="text-[9px] font-normal text-slate-400">家数 / 总市值 / PE / 股息率</span>
            </span>
          }
          extra={
            <span className="text-[9px] font-mono text-slate-400 whitespace-nowrap">
              合计 US$ {fmtInt(tiers?.total_market_cap_yi)} 亿
            </span>
          }
        >
          {tierRows.length === 0 ? (
            <EmptyHint loading={overviewLoading} />
          ) : (
            <div className="flex flex-col overflow-x-auto">
              <div className={`${HEAD} ${TIER_GRID}`}>
                <span>分层</span>
                <span className="text-right">家数</span>
                <span className="text-right">总市值（亿美元）</span>
                <span className="text-right">PE 中位</span>
                <span className="text-right">股息率中位</span>
              </div>
              {tierRows.map((t) => (
                <div key={t.key} className={`${ROW} ${TIER_GRID}`}>
                  <span className="text-[9px] font-extrabold text-blue-700 bg-blue-50 border border-blue-100 rounded px-1 py-px text-center whitespace-nowrap">
                    {t.label}
                  </span>
                  <span className="text-right font-mono text-slate-600 whitespace-nowrap">
                    {fmtInt(t.count)}
                  </span>
                  <span className="text-right font-mono font-bold text-slate-700 whitespace-nowrap">
                    {fmtInt(t.market_cap_yi)}
                  </span>
                  <span className="text-right font-mono text-slate-600 whitespace-nowrap">
                    {fmtNum(t.pe_median)}
                  </span>
                  <span className="text-right font-mono text-slate-600 whitespace-nowrap">
                    {pctOrDash(t.dividend_yield_median, 2)}
                  </span>
                </div>
              ))}
            </div>
          )}
          <p className="text-[9px] text-slate-400 leading-relaxed">
            分层阈值：超大盘 ≥ 2000 亿 / 大盘 ≥ 100 亿 / 中盘 ≥ 20 亿 / 其余为小盘（美元）。
          </p>
        </SectionCard>
        </div>

        {/* 估值三榜（右列） */}
        <SectionCard
        className="!p-2.5 gap-1.5 h-full"
        title={
          <span className="flex items-center gap-1.5">
            <Coins className="w-3.5 h-3.5 text-blue-600" />
            估值主题榜
            <span className="text-[9px] font-normal text-slate-400">Top 30 · PE/PB/股息率三列同屏</span>
          </span>
        }
        extra={
          <div className="flex items-center gap-0.5 rounded-full bg-slate-100 p-0.5">
            {KIND_OPTIONS.map((opt) => (
              <button
                key={opt.id}
                onClick={() => setKind(opt.id)}
                className={`px-2 py-[3px] rounded-full text-[10px] font-extrabold transition-all ${
                  kind === opt.id
                    ? 'bg-white text-blue-700 shadow-2xs border border-blue-200'
                    : 'text-slate-500 hover:text-slate-800'
                }`}
              >
                {opt.label}
              </button>
            ))}
          </div>
        }
      >
        {rankItems.length === 0 ? (
          <EmptyHint loading={rankLoading} text="暂无符合条件的标的" />
        ) : (
          <div className="flex flex-col flex-1 min-h-0 overflow-y-auto">
            <div className={`${HEAD} ${RANK_GRID} sticky top-0 z-10 bg-white/95 backdrop-blur`}>
              <span>#</span>
              <span>标的</span>
              <span className="text-right">市值</span>
              <span className="text-right">PE</span>
              <span className="text-right">PB</span>
              <span className="text-right">股息率</span>
              <span className="text-right">榜值</span>
            </div>
            {rankItems.map((it, i) => (
              <div key={it.symbol} className={`${ROW} ${RANK_GRID}`}>
                <span className={`text-[9px] font-extrabold ${i < 3 ? 'text-blue-600' : 'text-slate-400'}`}>
                  {i + 1}
                </span>
                <span className="flex items-center gap-1 min-w-0">
                  <span className="font-bold text-slate-800 truncate" title={it.name}>{it.name}</span>
                  {it.name !== it.symbol && (
                    <span className="text-[9px] font-mono text-slate-400 truncate flex-shrink-0">{it.symbol}</span>
                  )}
                  {it.sector && (
                    <span
                      className="text-[8px] font-bold text-blue-700 bg-blue-50 border border-blue-100 rounded px-1 whitespace-nowrap hidden xl:inline"
                      title={it.sector}
                    >
                      {it.sector}
                    </span>
                  )}
                </span>
                <span className="text-right font-mono text-slate-500 whitespace-nowrap" title="总市值（亿美元）">
                  {fmtInt(it.market_cap_yi)}
                </span>
                <span className={`text-right font-mono whitespace-nowrap ${kind === 'pe' ? 'font-extrabold text-slate-800' : 'text-slate-600'}`}>
                  {fmtNum(it.pe_ratio)}
                </span>
                <span className={`text-right font-mono whitespace-nowrap ${kind === 'pb' ? 'font-extrabold text-slate-800' : 'text-slate-600'}`}>
                  {fmtNum(it.pb_ratio)}
                </span>
                <span className={`text-right font-mono whitespace-nowrap ${kind === 'dividend' ? 'font-extrabold text-slate-800' : 'text-slate-600'}`}>
                  {pctOrDash(it.dividend_yield, 2)}
                </span>
                <span className="text-right font-mono font-extrabold text-blue-700 whitespace-nowrap">
                  {rankValueText(it, kind)}
                  {kind !== 'dividend' && <span className="text-[9px] text-slate-400">x</span>}
                </span>
              </div>
            ))}
          </div>
        )}
        <div className="flex items-start gap-1.5">
          <Info className="w-3 h-3 text-slate-300 mt-0.5 flex-shrink-0" />
          <p className="text-[9px] text-slate-400 leading-relaxed">
            榜单已施加健全性门槛（市值 ≥ 20 亿美元、PE ≥ 3、PB ≥ 0.3、股息率 ≥ 0.5%），
            用于剔除快照陈旧的空壳标的（如收购 / 退市残留）。数据源为 f10 基本面快照，非实时行情。
            PE/PB/股息率三列对全部榜单同屏可见，加粗列为当前榜单的排序维度。
          </p>
        </div>
        </SectionCard>
      </div>
    </div>
  );
};
