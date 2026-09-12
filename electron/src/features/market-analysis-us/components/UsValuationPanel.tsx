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
  PeriodChips,
  RankRow,
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

/** 榜单行的副指标（跨榜展示另外两个估值维度，缺失显示 --） */
function rankSubText(row: UsValuationRankRow, kind: RankKind): string {
  const dy = `股息率 ${pctOrDash(row.dividend_yield, 2)}`;
  if (kind === 'dividend') return `PE ${fmtNum(row.pe_ratio)} · PB ${fmtNum(row.pb_ratio)}`;
  if (kind === 'pe') return `PB ${fmtNum(row.pb_ratio)} · ${dy}`;
  return `PE ${fmtNum(row.pe_ratio)} · ${dy}`;
}

function StatTile({ label, value, sub }: { label: string; value: React.ReactNode; sub?: React.ReactNode }) {
  return (
    <div className="rounded-xl border border-blue-100 bg-blue-50/60 px-3 py-2 flex flex-col gap-0.5">
      <span className="text-[10px] font-bold text-slate-500">{label}</span>
      <span className="text-sm font-extrabold font-mono text-blue-700 whitespace-nowrap">{value}</span>
      {sub && <span className="text-[9px] font-mono text-slate-400 whitespace-nowrap">{sub}</span>}
    </div>
  );
}

const TIER_GRID = 'grid grid-cols-[74px_58px_1fr_72px_84px] gap-1.5 px-1 items-center';

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
    getValuationRankings(kind, 20)
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
    <div className="flex flex-col gap-2.5">
      {/* 面板标题 */}
      <div className="rounded-2xl border border-blue-100/70 bg-gradient-to-r from-blue-50/90 via-sky-50/70 to-white px-4 py-2.5 flex items-center justify-between gap-3">
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

      <div className="grid grid-cols-1 xl:grid-cols-2 gap-2.5 items-start">
        {/* 估值概览 */}
        <SectionCard
          title={
            <span className="flex items-center gap-1.5">
              <LineChart className="w-3.5 h-3.5 text-blue-600" />
              估值概览（标的池中位数）
            </span>
          }
          extra={<DateBadge label="数据" date={overview?.as_of} />}
        >
          {!overview && overviewLoading ? (
            <EmptyHint loading />
          ) : (
            <div className="grid grid-cols-2 md:grid-cols-3 gap-2">
              <StatTile label="覆盖标的" value={`${fmtInt(overview?.coverage)} 只`} />
              <StatTile label="PE 中位" value={fmtNum(overview?.pe_median)} />
              <StatTile
                label="PE 四分位"
                value={`${fmtNum(overview?.pe_p25)} ~ ${fmtNum(overview?.pe_p75)}`}
                sub="P25 ~ P75"
              />
              <StatTile label="PB 中位" value={fmtNum(overview?.pb_median)} />
              <StatTile
                label="股息率中位"
                value={pctOrDash(overview?.dividend_yield_median, 2)}
              />
              <StatTile
                label="分红标的"
                value={`${fmtInt(overview?.dividend_payers)} 只`}
                sub="股息率 ≥ 0.5%"
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
          title={
            <span className="flex items-center gap-1.5">
              <Layers className="w-3.5 h-3.5 text-blue-600" />
              市值分层（总市值 / PE / 股息率）
            </span>
          }
          extra={
            <span className="text-[10px] font-mono text-slate-400 whitespace-nowrap">
              合计 US$ {fmtInt(tiers?.total_market_cap_yi)} 亿
            </span>
          }
        >
          {tierRows.length === 0 ? (
            <EmptyHint loading={overviewLoading} />
          ) : (
            <div className="flex flex-col overflow-x-auto">
              <div
                className={`${TIER_GRID} pb-1 text-[9px] font-extrabold text-slate-400 border-b border-slate-100`}
              >
                <span>分层</span>
                <span className="text-right">家数</span>
                <span className="text-right">总市值（亿美元）</span>
                <span className="text-right">PE 中位</span>
                <span className="text-right">股息率中位</span>
              </div>
              {tierRows.map((t) => (
                <div
                  key={t.key}
                  className={`${TIER_GRID} py-1.5 border-b border-slate-50 last:border-0`}
                >
                  <span className="text-[10px] font-extrabold text-blue-700 bg-blue-50 border border-blue-100 rounded px-1 py-0.5 text-center whitespace-nowrap">
                    {t.label}
                  </span>
                  <span className="text-right text-[11px] font-mono font-bold text-slate-600">
                    {fmtInt(t.count)}
                  </span>
                  <span className="text-right text-[11px] font-mono font-bold text-slate-600">
                    {fmtInt(t.market_cap_yi)}
                  </span>
                  <span className="text-right text-[11px] font-mono font-bold text-slate-600">
                    {fmtNum(t.pe_median)}
                  </span>
                  <span className="text-right text-[11px] font-mono font-bold text-slate-600">
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

      {/* 估值三榜 */}
      <SectionCard
        title={
          <span className="flex items-center gap-1.5">
            <Coins className="w-3.5 h-3.5 text-blue-600" />
            估值主题榜（Top 20）
          </span>
        }
        extra={
          <PeriodChips
            options={KIND_OPTIONS}
            value={kind}
            onChange={(id) => setKind(id as RankKind)}
            accent="blue"
          />
        }
      >
        {rankItems.length === 0 ? (
          <EmptyHint loading={rankLoading} text="暂无符合条件的标的" />
        ) : (
          <div className="flex flex-col max-h-[460px] overflow-y-auto">
            {rankItems.map((it, i) => (
              <RankRow
                key={it.symbol}
                rank={i + 1}
                name={it.name}
                nameSub={it.name === it.symbol ? undefined : it.symbol}
                main={
                  <span className="flex items-center gap-2 min-w-0">
                    {it.sector && (
                      <span className="text-[9px] font-bold text-blue-700 bg-blue-50 border border-blue-100 rounded px-1 py-0.5 whitespace-nowrap">
                        {it.sector}
                      </span>
                    )}
                    <span className="text-[10px] font-mono text-slate-400 whitespace-nowrap">
                      市值 US$ {fmtInt(it.market_cap_yi)}亿
                    </span>
                  </span>
                }
                right={
                  <div className="flex items-center gap-3 flex-shrink-0">
                    <span className="text-[9px] font-mono text-slate-400 whitespace-nowrap">
                      {rankSubText(it, kind)}
                    </span>
                    <span className="w-16 text-right text-xs font-extrabold font-mono text-blue-700 whitespace-nowrap">
                      {rankValueText(it, kind)}
                      {kind !== 'dividend' && <span className="text-[9px] text-slate-400">x</span>}
                    </span>
                  </div>
                }
              />
            ))}
          </div>
        )}
        <div className="flex items-start gap-1.5 px-1">
          <Info className="w-3 h-3 text-slate-300 mt-0.5 flex-shrink-0" />
          <p className="text-[9px] text-slate-400 leading-relaxed">
            榜单已施加健全性门槛（市值 ≥ 20 亿美元、PE ≥ 3、PB ≥ 0.3、股息率 ≥ 0.5%），
            用于剔除快照陈旧的空壳标的（如收购 / 退市残留）。数据源为 f10 基本面快照，非实时行情。
          </p>
        </div>
      </SectionCard>
    </div>
  );
};
