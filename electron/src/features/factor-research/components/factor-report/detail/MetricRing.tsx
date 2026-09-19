/**
 * 7 指标环 —— 详情区 hero 的常驻头。
 *
 * 设计要点：**弧长是该指标在全库的百分位，不是裸值**。
 * 裸值画弧毫无意义（IC 0.03 该画多长？），只有放进全库分布里才知道好不好。
 * 环心放原始值，环上标 25/50/75% 参考刻度。
 *
 * 配色遵 A 股口径（涨红跌绿）：位于全库**有利端**为红、**不利端**为绿、
 * 中间区段为中性灰 —— 中间段刻意留灰，"排在中游"既不算好也不算坏。
 */

import React from 'react';
import { DOWN, NEUTRAL, UP } from './chartKit';

/** 中性区段：百分位落在 [0.4, 0.6) 判为中游 */
const NEUTRAL_LO = 0.4;
const NEUTRAL_HI = 0.6;
/** 仪表盘张角（度）：从 135° 起顺时针 270°，底部留口 */
const START_DEG = 135;
const SWEEP_DEG = 270;

function polar(cx: number, cy: number, r: number, deg: number): [number, number] {
  const rad = (deg * Math.PI) / 180;
  return [cx + r * Math.cos(rad), cy + r * Math.sin(rad)];
}

/** 圆弧路径；degEnd < degStart 时自动退化成一个点（不画） */
function arcPath(cx: number, cy: number, r: number, degStart: number, degEnd: number): string {
  const [x0, y0] = polar(cx, cy, r, degStart);
  const [x1, y1] = polar(cx, cy, r, degEnd);
  const large = Math.abs(degEnd - degStart) > 180 ? 1 : 0;
  return `M ${x0} ${y0} A ${r} ${r} 0 ${large} 1 ${x1} ${y1}`;
}

export interface MetricRingProps {
  label: string;
  value: number | null;
  /** 全库百分位（0~1）；null = 全库缺该口径 → 只显示值，环留灰 */
  percentile: number | null;
  /** true = 该指标越小越有利（如换手） */
  lowerIsBetter?: boolean;
  format: (v: number) => string;
  /** 口径说明（来自后端 definitions） */
  info?: string;
  size?: number;
}

function toneOf(percentile: number | null, lowerIsBetter: boolean): string {
  if (percentile == null) return NEUTRAL;
  // 「有利端」取决于指标方向：换手越低越好 → 低分位才是有利
  const favorable = lowerIsBetter ? percentile <= 1 - NEUTRAL_HI : percentile >= NEUTRAL_HI;
  const unfavorable = lowerIsBetter ? percentile >= 1 - NEUTRAL_LO : percentile <= NEUTRAL_LO;
  if (favorable) return UP;
  if (unfavorable) return DOWN;
  return NEUTRAL;
}

export const MetricRing: React.FC<MetricRingProps> = ({
  label, value, percentile, lowerIsBetter = false, format, info, size = 92,
}) => {
  const cx = size / 2;
  const cy = size / 2;
  const r = size / 2 - 9;
  const color = toneOf(percentile, lowerIsBetter);
  const p = percentile == null ? 0 : Math.min(Math.max(percentile, 0), 1);
  const endDeg = START_DEG + SWEEP_DEG * p;
  const pctText = percentile == null ? null : `${Math.round(percentile * 100)}%`;

  return (
    <div className="flex flex-col items-center shrink-0" style={{ width: size }}>
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} role="img"
        aria-label={`${label} ${format(value ?? 0)}，全库百分位 ${pctText ?? '未知'}`}>
        {/* 轨道 */}
        <path d={arcPath(cx, cy, r, START_DEG, START_DEG + SWEEP_DEG)}
          fill="none" stroke="#eef2f7" strokeWidth={7} strokeLinecap="round" />
        {/* 参考刻度：25 / 50 / 75% */}
        {[0.25, 0.5, 0.75].map((t) => {
          const deg = START_DEG + SWEEP_DEG * t;
          const [ax, ay] = polar(cx, cy, r - 5.5, deg);
          const [bx, by] = polar(cx, cy, r + 5.5, deg);
          return (
            <line key={t} x1={ax} y1={ay} x2={bx} y2={by}
              stroke="#cbd5e1" strokeWidth={t === 0.5 ? 1.4 : 0.9} />
          );
        })}
        {/* 值弧 */}
        {p > 0 && (
          <path d={arcPath(cx, cy, r, START_DEG, endDeg)}
            fill="none" stroke={color} strokeWidth={7} strokeLinecap="round" />
        )}
        {/* 环心：原始值 */}
        <text x={cx} y={cy + 1} textAnchor="middle" dominantBaseline="middle"
          fontSize={size >= 88 ? 15 : 13} fontWeight={800} fill={value == null ? NEUTRAL : '#1e293b'}
          style={{ fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace' }}>
          {value == null ? '—' : format(value)}
        </text>
        {pctText && (
          <text x={cx} y={cy + 15} textAnchor="middle" fontSize={9} fill={color} fontWeight={700}>
            {pctText}
          </text>
        )}
      </svg>
      <div className="mt-0.5 flex items-center gap-0.5 text-[10px] font-bold text-slate-500 whitespace-nowrap">
        {label}
        {info && (
          <span className="relative inline-flex group/ring">
            <span className="cursor-help text-[9px] text-slate-300 hover:text-indigo-500" role="note">ⓘ</span>
            <span className="pointer-events-none absolute left-1/2 bottom-full z-30 mb-1 hidden w-64 -translate-x-1/2 rounded-xl border border-slate-200 bg-white p-2.5 text-[10px] font-normal leading-relaxed text-slate-600 shadow-lg group-hover/ring:block">
              {info}
            </span>
          </span>
        )}
      </div>
    </div>
  );
};

/**
 * 全库百分位。`pick` 决定用哪个数参与排名：
 * - `abs`：用 |值|（信号强度，方向可反做）—— 用于 IC / ICIR
 * - `signed`：用原值（组合实际盈亏）—— 用于 Returns / IR / Fitness / Margin
 *
 * 库内缺该口径的因子（旧快照）直接排除，不当作 0 参与排名 —— 否则会把
 * "没有数据" 排成 "最差"。
 */
export function percentileOf(
  values: Array<number | null | undefined>,
  value: number | null | undefined,
  pick: 'abs' | 'signed' = 'signed',
): number | null {
  if (value == null || !Number.isFinite(value)) return null;
  const pick_ = (v: number) => (pick === 'abs' ? Math.abs(v) : v);
  const pool = values
    .filter((v): v is number => v != null && Number.isFinite(v))
    .map(pick_)
    .sort((a, b) => a - b);
  if (pool.length < 5) return null; // 样本太少，百分位没有意义
  const target = pick_(value);
  // 二分找 <= target 的个数 → 秩百分位（并列取中点，避免并列项被判成两个极端）
  let lo = 0;
  let hi = pool.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (pool[mid] <= target) lo = mid + 1;
    else hi = mid;
  }
  const below = lo;
  let same = 0;
  for (let i = below - 1; i >= 0 && pool[i] === target; i--) same++;
  return (below - same / 2) / pool.length;
}
