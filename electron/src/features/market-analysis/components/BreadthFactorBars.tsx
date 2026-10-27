import React from 'react';
import type { SentimentIndicator } from '../services/sentimentService';
import { BREADTH_META } from './breadthReadings';

/**
 * 广度分项「冷热分位图」。
 *
 * 为什么不是简单罗列百分位：各指标方向不一致 —— ADR 高是「热」，TRIN 高却是
 * 「冷」。直接画原始分位会让同一个位置既代表进攻又代表防守，普通人必然读反。
 * 这里先把方向统一成「越靠右＝市场越冷」，再画点位。
 */

const ZONES = [
  { max: 20, label: '很热', dot: 'bg-red-500', track: 'bg-red-100/80' },
  { max: 40, label: '偏热', dot: 'bg-amber-500', track: 'bg-amber-100/80' },
  { max: 60, label: '中性', dot: 'bg-slate-400', track: 'bg-slate-100' },
  { max: 80, label: '偏冷', dot: 'bg-teal-500', track: 'bg-teal-100/80' },
  { max: 101, label: '很冷', dot: 'bg-emerald-500', track: 'bg-emerald-100/80' },
];

/** 短名供热力图/紧凑列表复用 */
export function factorShortName(key: string, fallback: string): string {
  return BREADTH_META[key]?.short ?? fallback;
}

const zoneOf = (cold: number) => ZONES.find((z) => cold < z.max) ?? ZONES[ZONES.length - 1];

/** 原始分位 → 方向统一后的「冷度」（0=最热，100=最冷）。 */
export function coldPercentile(item: SentimentIndicator): number | null {
  if (typeof item.pct !== 'number') return null;
  const meta = BREADTH_META[item.key];
  const raw = item.pct * 100;
  return meta?.hotWhenHigh === false ? raw : 100 - raw;
}

interface Props {
  items: SentimentIndicator[];
  /** 左栏浓缩版：无轴、无解释，只保留短名 + 迷你冷热条 + 当前值 */
  compact?: boolean;
}

export const BreadthFactorBars: React.FC<Props> = ({ items, compact = false }) => {
  if (compact) {
    return (
      <div className="flex flex-col gap-1">
        <div className="flex items-center justify-between text-[9px] font-bold text-slate-400">
          <span className="text-red-500">← 热</span>
          <span>方向已统一，点越靠右越冷</span>
          <span className="text-emerald-600">冷 →</span>
        </div>
        {items.map((item) => {
          const meta = BREADTH_META[item.key];
          const cold = coldPercentile(item);
          const zone = zoneOf(cold ?? 50);
          return (
            <div
              key={item.key}
              className="flex flex-col gap-0.5 py-0.5 border-b border-slate-50 last:border-0"
              title={`${meta?.name ?? item.label}：${meta?.hint ?? ''}`}
            >
            <div className="grid grid-cols-[62px_1fr_52px] items-center gap-1.5">
              <span className="text-[10px] text-slate-600 truncate">{meta?.short ?? item.label}</span>
              <div className="relative h-3 rounded-sm overflow-hidden border border-slate-200/60">
                <div className="absolute inset-0 flex">
                  <div className="w-1/5 bg-red-100/80" />
                  <div className="w-1/5 bg-amber-100/80" />
                  <div className="w-1/5 bg-slate-100" />
                  <div className="w-1/5 bg-teal-100/80" />
                  <div className="w-1/5 bg-emerald-100/80" />
                </div>
                {cold !== null && (
                  <span
                    className={`absolute top-1/2 -translate-x-1/2 -translate-y-1/2 block w-2 h-2 rounded-full border border-white shadow ${zone.dot}`}
                    style={{ left: `${Math.min(Math.max(cold, 2), 98)}%` }}
                  />
                )}
              </div>
              <span className="text-[10px] font-mono font-bold text-slate-700 text-right">
                {typeof item.value === 'number'
                  ? item.unit === '%'
                    ? item.value.toFixed(1)
                    : item.value
                  : '—'}
                <span className="text-[8px] font-normal text-slate-400">{item.unit}</span>
              </span>
            </div>
            <span className="text-[9px] text-slate-400 leading-tight pl-[0.5px]">
              {meta ? meta.reading(typeof item.value === 'number' ? item.value : null) : ''}
            </span>
            </div>
          );
        })}
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-1.5">
      {/* 统一轴 */}
      <div className="grid grid-cols-[168px_1fr_104px] items-end gap-3">
        <div className="text-[10px] font-bold text-slate-400">
          指标（越靠右越冷）
        </div>
        <div>
          <div className="relative h-3">
            {[0, 20, 40, 60, 80, 100].map((t) => (
              <span
                key={t}
                className="absolute top-0 text-[9px] font-mono text-slate-400 -translate-x-1/2"
                style={{ left: `${t}%` }}
              >
                {t}
              </span>
            ))}
          </div>
          <div className="flex justify-between text-[9px] font-bold mt-0.5">
            <span className="text-red-500">← 很热（追高风险）</span>
            <span className="text-slate-400">中性</span>
            <span className="text-emerald-600">很冷（超卖机会）→</span>
          </div>
        </div>
        <div className="text-[10px] font-bold text-slate-400 text-right">当前值</div>
      </div>

      {items.map((item) => {
        const meta = BREADTH_META[item.key] ?? {
          name: item.label,
          hint: '',
          hotWhenHigh: true,
        };
        const cold = coldPercentile(item);
        const zone = zoneOf(cold ?? 50);
        return (
          <div
            key={item.key}
            className="grid grid-cols-[168px_1fr_104px] items-center gap-3 py-1 border-t border-slate-50"
          >
            <div className="min-w-0">
              <div className="text-[11px] font-bold text-slate-700 truncate">{meta.name}</div>
              <div className="text-[10px] text-slate-400 truncate" title={meta.hint}>
                {meta.hint}
              </div>
            </div>

            <div
              className="relative h-6 rounded-md overflow-hidden border border-slate-200/70"
              title={
                cold === null
                  ? '历史数据不足'
                  : `${meta.name}：历史分位 ${Math.round((item.pct ?? 0) * 100)}%，方向统一后冷度 ${Math.round(cold)}`
              }
            >
              <div className="absolute inset-0 flex">
                <div className="w-1/5 bg-red-100/80" />
                <div className="w-1/5 bg-amber-100/80" />
                <div className="w-1/5 bg-slate-100" />
                <div className="w-1/5 bg-teal-100/80" />
                <div className="w-1/5 bg-emerald-100/80" />
              </div>
              <div className="absolute inset-y-0 left-1/2 w-px bg-slate-300/70" />
              {cold !== null && (
                <div
                  className="absolute top-1/2 -translate-x-1/2 -translate-y-1/2"
                  style={{ left: `${Math.min(Math.max(cold, 1), 99)}%` }}
                >
                  <span className={`block w-3 h-3 rounded-full border-2 border-white shadow ${zone.dot}`} />
                </div>
              )}
            </div>

            <div className="text-right leading-tight">
              <div className="text-sm font-extrabold font-mono text-slate-800">
                {typeof item.value === 'number' ? item.value : '—'}
                <span className="text-[10px] font-normal text-slate-400 ml-0.5">{item.unit}</span>
              </div>
              <div className={`text-[10px] font-bold ${cold === null ? 'text-slate-300' : 'text-slate-500'}`}>
                {cold === null ? '样本不足' : zone.label}
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
};
