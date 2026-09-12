/** 美股 GICS 板块多周期轮动面板 —— 1/5/20/60 日收益 + 相对标普强弱 + 板块内宽度
 *
 * 后端口径（backend/services/api/market_analysis_us/feed/sectors.py）：
 * - 各周期收益为**板块成分股中位数**（对拆股等异常值稳健），不是市值加权
 * - rs_20d = 板块 20 日收益 − 标普500 20 日收益（百分点）；指数分区通常滞后个股，
 *   故 trade_date（个股）与 index_date（指数）必须分开标注
 * - breadth_20d = 成分股中 20 日收益为正的占比（%）
 * 后端已按 20 日收益降序返回。
 */

import React, { useEffect, useState } from 'react';
import { Layers } from 'lucide-react';
import { getSectorRotation } from '../services/api';
import type { UsSectorRotation } from '../types';
import {
  SectionCard,
  EmptyHint,
  PctText,
  DateBadge,
  fmtInt,
} from '../../market-analysis-shared/ui';

/** 相对强弱超过 ±3 个百分点才铺底色，避免噪声把整表染花 */
const RS_TINT_THRESHOLD = 3;

const GRID = 'grid grid-cols-[1fr_42px_42px_46px_46px_58px_52px] gap-1.5';

function rsText(rs: number | null): string {
  if (rs === null || Number.isNaN(rs)) return '--';
  return `${rs > 0 ? '+' : ''}${rs.toFixed(1)}`;
}

function rsClass(rs: number | null): string {
  if (rs === null || Number.isNaN(rs)) return 'text-slate-400';
  if (rs >= RS_TINT_THRESHOLD) return 'bg-red-50 text-red-600';
  if (rs <= -RS_TINT_THRESHOLD) return 'bg-green-50 text-green-600';
  return 'text-slate-500';
}

/** 宽度条宽度（%）：null 记 0，并夹在 0-100 */
function breadthWidth(v: number | null): number {
  if (v === null || Number.isNaN(v)) return 0;
  return Math.min(100, Math.max(0, v));
}

export const UsSectorRotationPanel: React.FC = () => {
  const [data, setData] = useState<UsSectorRotation | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    getSectorRotation(24)
      .then((d) => {
        if (alive) setData(d);
      })
      .catch(() => undefined)
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, []);

  const sectors = data?.sectors ?? [];

  return (
    <SectionCard
      title={
        <span className="flex items-center gap-1.5">
          <Layers className="w-3.5 h-3.5 text-blue-600" />
          GICS 板块轮动（1/5/20/60日）
        </span>
      }
      extra={
        <>
          <DateBadge label="个股" date={data?.trade_date} />
          <DateBadge label="指数" date={data?.index_date} />
        </>
      }
    >
      {/* 标普 20 日收益：rs_20d 就是板块 20 日收益与它的差 */}
      <div className="flex items-center justify-between gap-2 px-2.5 py-1.5 rounded-xl bg-blue-50/70 border border-blue-100">
        <span className="text-[10px] font-bold text-slate-500">
          标普500 · 20日收益（相对强弱基准）
        </span>
        <PctText value={data?.benchmark_return_20d ?? null} className="text-[11px]" />
      </div>

      <p className="text-[9px] text-slate-400 leading-relaxed font-medium">
        口径：各周期涨跌幅均为板块成分股<span className="text-slate-500 font-bold">中位数</span>
        （对拆股等异常值稳健）；「相对标普」为板块20日收益减标普500（红=跑赢，绿=跑输）；
        「宽度」为 20 日收涨成分股占比。指数分区通常滞后个股，两个日期分别标注。
      </p>

      {sectors.length === 0 ? (
        <EmptyHint loading={loading} />
      ) : (
        <div className="flex flex-col max-h-[560px] overflow-y-auto">
          <div
            className={`${GRID} px-1 pb-1 text-[9px] font-extrabold text-slate-400 border-b border-slate-100 sticky top-0 bg-white/95 backdrop-blur z-10`}
          >
            <span>板块 · 成分股</span>
            <span className="text-right">1日</span>
            <span className="text-right">5日</span>
            <span className="text-right">20日</span>
            <span className="text-right">60日</span>
            <span className="text-right" title="板块20日收益 − 标普500 20日收益（百分点）">
              相对标普
            </span>
            <span className="text-right" title="20日收涨的成分股占比">
              宽度20日
            </span>
          </div>

          {sectors.map((it, i) => (
            <div
              key={it.sector || it.name}
              className={`${GRID} px-1 py-1 border-b border-slate-50 last:border-0 items-center`}
            >
              <span className="flex items-center gap-1.5 min-w-0">
                <span className="text-[9px] font-extrabold text-slate-300 w-4 flex-shrink-0">
                  {String(i + 1).padStart(2, '0')}
                </span>
                <span className="text-[10px] font-bold text-slate-800 truncate" title={it.sector}>
                  {it.name}
                </span>
                <span className="text-[9px] font-mono text-slate-300 flex-shrink-0">
                  {fmtInt(it.stock_count)}
                </span>
              </span>
              <span className="text-right">
                <PctText value={it.ret_1d} className="text-[10px]" />
              </span>
              <span className="text-right">
                <PctText value={it.ret_5d} className="text-[10px]" />
              </span>
              <span className="text-right">
                <PctText value={it.ret_20d} className="text-[10px]" />
              </span>
              <span className="text-right">
                <PctText value={it.ret_60d} className="text-[10px]" />
              </span>
              <span className="text-right">
                <span
                  className={`inline-block px-1 py-0.5 rounded-md font-mono text-[10px] font-extrabold ${rsClass(
                    it.rs_20d,
                  )}`}
                >
                  {rsText(it.rs_20d)}
                </span>
              </span>
              <span className="flex flex-col items-end gap-0.5">
                <span className="text-[9px] font-mono text-slate-500">
                  {it.breadth_20d === null ? '--' : `${it.breadth_20d.toFixed(0)}%`}
                </span>
                <span className="w-full h-1 rounded-full bg-slate-100 overflow-hidden">
                  <span
                    className="block h-full rounded-full bg-blue-500"
                    style={{ width: `${breadthWidth(it.breadth_20d)}%` }}
                  />
                </span>
              </span>
            </div>
          ))}
        </div>
      )}
    </SectionCard>
  );
};
