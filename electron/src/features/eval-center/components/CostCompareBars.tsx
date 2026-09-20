/**
 * 换手 × 成本对比条（模型卡 / turnover_cost 维）：毛利 → 成本拖累 → 净利三条同尺度并排。
 *
 * 一条净值曲线看不出「赚的是毛的还是净的」，这三条并排就能看出成本吃掉了多少。
 * 该维缺省时本组件不渲染 —— `costCompare` 的 note 与「维度明细」里那一行是同一条
 * 原因，不在这里重复第二遍。
 */

import React, { useMemo } from 'react';
import { Scale } from 'lucide-react';
import type { EvalScoreRow } from '../types/evalCenter';
import { formatSignedPct } from './evalCenterModel';
import { costCompare } from './evalInsightModel';
import { TONE_COLOR, TONE_TEXT } from './evalTones';

interface CostCompareBarsProps {
  row: EvalScoreRow;
}

export const CostCompareBars: React.FC<CostCompareBarsProps> = ({ row }) => {
  const { bars, note } = useMemo(() => costCompare(row), [row]);
  if (bars.length === 0) return null;

  return (
    <div>
      <div className="mb-2 flex flex-wrap items-baseline gap-x-2">
        <span className="inline-flex items-center gap-1.5 text-[12px] font-semibold text-slate-700">
          <Scale className="h-3.5 w-3.5 text-slate-400" />
          换手与成本
        </span>
        <span className="text-[10px] text-slate-400">{note}</span>
      </div>
      <div className="space-y-1.5">
        {bars.map((bar) => (
          <div key={bar.label} className="flex items-center gap-2">
            <span className="w-14 shrink-0 text-[10px] text-slate-500">{bar.label}</span>
            <div className="h-2 flex-1 overflow-hidden rounded-full bg-slate-100">
              <div
                className="h-full rounded-full"
                style={{
                  width: `${Math.round(bar.width * 100)}%`,
                  background: TONE_COLOR[bar.tone],
                }}
              />
            </div>
            <span
              className={`w-16 shrink-0 text-right text-[11px] font-bold tabular-nums ${TONE_TEXT[bar.tone]}`}
            >
              {formatSignedPct(bar.value)}
            </span>
          </div>
        ))}
      </div>
      <p className="mt-2 text-[10px] text-slate-400">
        净利 = 毛利 + 成本拖累（成本按单次往返成本 × 换手折算）
      </p>
    </div>
  );
};
