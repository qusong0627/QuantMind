/** 美股板块估值温度计 —— GICS 板块 × PE/PB/股息率中位数 + 市值合计（价值洼地识别）
 *
 * 后端口径（backend/services/api/market_analysis_us/feed/sectors.py）：
 * - 数据源 `f10` 快照（不是 valuation 分区，后者曾长期静默写空）
 * - PE/PB 中位数只取正值（亏损公司的负 PE 会污染中位数）；无有效值时后端返回
 *   **null 而非 0**，前端必须渲染 `--`（0 会被误读为真实估值）
 * - market_cap_yi 后端已换算为「亿美元」，直接展示即可，不要再除 1e8
 * - 后端按 PE 中位数升序返回（null 排最后），低 PE 在前
 * 排版按看盘密度：表头 + 行高 py-[3px]，PE 色条压薄到 h-2.5，另加「市值占比」列。
 */

import React, { useEffect, useState } from 'react';
import { ThermometerSun } from 'lucide-react';
import { getSectorValuation } from '../services/api';
import type { UsSectorValuationRow } from '../types';
import { SectionCard, EmptyHint, fmtInt } from '../../market-analysis-shared/ui';

/** 列：板块 / PE中位 / PB中位 / 股息率 / 市值(亿$) / 市值占比 / 股数 */
const GRID = 'grid grid-cols-[1fr_56px_40px_48px_62px_44px_30px] gap-1';

interface PeBand {
  bar: string;
  text: string;
  /** 相对位置 0-1（用于淡色条宽度） */
  t: number;
}

/** PE 分档：按当前展示集合的相对位置三等分（低=绿 洼地，高=红 偏贵） */
function peBand(pe: number, min: number, max: number): PeBand {
  const span = max - min;
  const t = span > 0 ? Math.min(1, Math.max(0, (pe - min) / span)) : 0.5;
  if (t <= 1 / 3) return { bar: 'bg-emerald-100', text: 'text-emerald-700', t };
  if (t <= 2 / 3) return { bar: 'bg-amber-100', text: 'text-amber-700', t };
  return { bar: 'bg-rose-100', text: 'text-rose-700', t };
}

export const UsSectorValuationPanel: React.FC = () => {
  const [items, setItems] = useState<UsSectorValuationRow[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    getSectorValuation(30)
      .then((d) => {
        if (alive) setItems(d);
      })
      .catch(() => undefined)
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, []);

  // PE 分档只统计有效正值，null 不参与极值计算
  const peVals = items
    .map((i) => i.pe_median)
    .filter((v): v is number => v !== null && Number.isFinite(v));
  const peMin = peVals.length ? Math.min(...peVals) : 0;
  const peMax = peVals.length ? Math.max(...peVals) : 0;

  // 市值占比 = 该板块市值 / 本次展示板块市值合计（非全市场口径，标题里已注明）
  const capTotal = items.reduce(
    (sum, i) => (Number.isFinite(i.market_cap_yi) ? sum + i.market_cap_yi : sum),
    0,
  );

  return (
    <SectionCard
      className="!p-2.5 !gap-1.5"
      title={
        <span className="flex items-center gap-1.5">
          <ThermometerSun className="w-3.5 h-3.5 text-blue-600" />
          板块估值温度计
          <span className="text-[9px] font-normal text-slate-400">
            f10 快照 · 按 PE 升序 · 绿=洼地 红=偏贵
          </span>
        </span>
      }
    >
      <p className="text-[9px] text-slate-400 leading-snug font-medium">
        PE / PB 中位数只取正值（亏损公司已剔除），无有效值显示 --；分档按当前展示集合 PE
        相对位置三等分。市值与占比单位：亿美元。
      </p>

      {items.length === 0 ? (
        <EmptyHint loading={loading} />
      ) : (
        <div className="flex flex-col max-h-[560px] overflow-y-auto">
          <div
            className={`${GRID} px-1 py-[3px] text-[9px] font-bold text-slate-400 border-b border-slate-200 sticky top-0 bg-white/95 backdrop-blur z-10`}
          >
            <span>板块</span>
            <span className="text-right" title="成分股 PE 中位数（只取正值）">
              PE中位
            </span>
            <span className="text-right" title="成分股 PB 中位数（只取正值）">
              PB
            </span>
            <span className="text-right" title="成分股股息率中位数">
              股息率
            </span>
            <span className="text-right" title="板块市值合计（亿美元）">
              市值(亿$)
            </span>
            <span className="text-right" title="占本次展示板块市值合计的比例（非全市场口径）">
              占比
            </span>
            <span className="text-right" title="板块成分股数量（标的池内）">
              股数
            </span>
          </div>

          {items.map((it) => {
            const band = it.pe_median !== null ? peBand(it.pe_median, peMin, peMax) : null;
            const capShare =
              capTotal > 0 && Number.isFinite(it.market_cap_yi)
                ? (it.market_cap_yi / capTotal) * 100
                : null;
            return (
              <div
                key={it.sector || it.name}
                className={`${GRID} px-1 py-[3px] border-b border-slate-50 last:border-0 items-center hover:bg-slate-50/80`}
              >
                <span className="flex items-center gap-1 min-w-0">
                  <span className="text-[10px] font-bold text-slate-800 truncate" title={it.sector}>
                    {it.name}
                  </span>
                </span>

                {band && it.pe_median !== null ? (
                  <span
                    className="relative h-2.5 rounded-sm bg-slate-100 overflow-hidden"
                    title={`PE 中位数 ${it.pe_median.toFixed(2)}`}
                  >
                    <span
                      className={`absolute inset-y-0 left-0 ${band.bar}`}
                      style={{ width: `${Math.max(band.t * 100, 10)}%` }}
                    />
                    <span
                      className={`relative z-10 block text-center text-[9px] font-mono font-extrabold leading-[10px] ${band.text}`}
                    >
                      {it.pe_median.toFixed(1)}
                    </span>
                  </span>
                ) : (
                  <span className="text-right text-[10px] font-mono text-slate-300">--</span>
                )}

                <span className="text-right text-[10px] font-mono text-slate-600">
                  {it.pb_median === null ? '--' : it.pb_median.toFixed(2)}
                </span>
                <span className="text-right text-[10px] font-mono font-bold text-emerald-600">
                  {it.dividend_yield_median === null
                    ? '--'
                    : `${it.dividend_yield_median.toFixed(2)}%`}
                </span>
                <span className="text-right text-[10px] font-mono text-slate-600">
                  {fmtInt(it.market_cap_yi)}
                </span>
                <span className="text-right text-[9px] font-mono text-slate-500">
                  {capShare === null ? '--' : `${capShare.toFixed(1)}%`}
                </span>
                <span className="text-right text-[9px] font-mono text-slate-400">
                  {fmtInt(it.stock_count)}
                </span>
              </div>
            );
          })}
        </div>
      )}
    </SectionCard>
  );
};
