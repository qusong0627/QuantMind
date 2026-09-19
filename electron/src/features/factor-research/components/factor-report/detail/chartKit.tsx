/**
 * 因子详情区的共享视觉层：配色、图表卡壳、口径 ⓘ、数值格式化、ECharts 通用片段。
 *
 * 沿用栏目既有语言（Tailwind / slate 底 / indigo→violet 主色 / font-mono 数据 /
 * **涨红跌绿**），不引入第二套设计系统。
 *
 * ⚠️ 图表容器一律 `min-w-0`：内层固定像素宽会撑住栅格 min-content，
 * 窗口缩放后 ECharts 永不重绘（既有事故）。
 */

import React from 'react';

// ─────────────────────────── 配色 ───────────────────────────

/** 上涨 / 有利（A 股口径：红涨） */
export const UP = '#e11d48';
/** 下跌 / 不利（绿跌） */
export const DOWN = '#059669';
export const ACCENT = '#6366f1';
export const ACCENT_2 = '#8b5cf6';
export const NEUTRAL = '#94a3b8';
export const WARN = '#f59e0b';
export const GRID = '#f1f5f9';

/** 分位色阶：G1（因子值最小）绿 → G10 红，与既有「分位净值曲线」保持一致 */
export function quantileColor(i: number, total: number): string {
  const t = total > 1 ? i / (total - 1) : 0;
  const hue = 152 - 152 * t; // 绿(152) → 红(0)
  return `hsl(${hue}, 62%, ${t > 0.5 ? 48 : 42}%)`;
}

/** 涨红跌绿：正值红、负值绿。用于柱状图逐项着色。 */
export const bySign = (v: number | null): string => (v != null && v < 0 ? DOWN : UP);

// ─────────────────────────── 数值格式化 ───────────────────────────

export const DASH = '—';

/** 小数 → 百分数文本（0.0012 → 0.12%），带正负号 */
export function fmtPct(v: number | null | undefined, digits = 2, signed = true): string {
  if (v == null || !Number.isFinite(v)) return DASH;
  const p = v * 100;
  const sign = signed && p > 0 ? '+' : '';
  return `${sign}${p.toFixed(digits)}%`;
}

/** 原样数值 + 正负号（IC / IR / Fitness 这类无量纲量） */
export function fmtNum(v: number | null | undefined, digits = 4, signed = true): string {
  if (v == null || !Number.isFinite(v)) return DASH;
  const sign = signed && v > 0 ? '+' : '';
  return `${sign}${v.toFixed(digits)}`;
}

export function fmtInt(v: number | null | undefined): string {
  return v == null || !Number.isFinite(v) ? DASH : String(Math.round(v));
}

/** 紧凑日期：2026-09-18 → 09-18、20260918 → 09-18；其余原样 */
export const fmtDate = (d: string): string => {
  if (!d) return d;
  if (d.length === 10 && d[4] === '-') return d.slice(5);
  if (d.length === 8 && !d.includes('-')) return `${d.slice(4, 6)}-${d.slice(6, 8)}`;
  return d;
};

/** 日期轴标签抽样间隔（避免 250 个标签糊成一团） */
export const axisInterval = (n: number, target = 6): number =>
  Math.max(1, Math.floor(n / Math.max(target, 1)));

// ─────────────────────────── 图表卡壳 ───────────────────────────

interface ChartShellProps {
  title: string;
  hint?: string;
  /** 口径说明；给了就在标题旁挂 ⓘ */
  info?: string;
  /** 主图去边框（视觉层级：主图浮起、辅图卡壳） */
  primary?: boolean;
  className?: string;
  children: React.ReactNode;
  /** 右上角操作区（导出该图数据等） */
  actions?: React.ReactNode;
}

export const ChartShell: React.FC<ChartShellProps> = ({
  title, hint, info, primary, className = '', children, actions,
}) => (
  <div
    className={`flex flex-col min-h-0 min-w-0 ${
      primary
        ? 'rounded-2xl bg-white p-3 shadow-[0_1px_2px_rgba(15,23,42,0.04)]'
        : 'rounded-2xl border border-slate-200/80 bg-white p-3 shadow-sm'
    } ${className}`}
  >
    <div className="flex items-baseline justify-between gap-2 mb-1.5 shrink-0">
      <h4 className="flex items-center gap-1 text-xs font-extrabold text-slate-800 truncate">
        {title}
        {info && <InfoDot text={info} />}
      </h4>
      <div className="flex items-center gap-2 shrink-0">
        {hint && <span className="text-[10px] text-slate-400 font-mono">{hint}</span>}
        {actions}
      </div>
    </div>
    <div className="flex-1 min-h-0 min-w-0">{children}</div>
  </div>
);

// ─────────────────────────── 口径 ⓘ ───────────────────────────

/**
 * 口径说明。文案来自后端 `/detail` 的 `definitions`（与计算代码同源），
 * 前端不另写一份 —— 两份文案必然漂移。
 */
export const InfoDot: React.FC<{ text: string }> = ({ text }) => (
  <span className="relative inline-flex group/i shrink-0">
    <span
      className="cursor-help select-none text-[10px] leading-none text-slate-300 hover:text-indigo-500"
      aria-label="口径说明"
      role="note"
    >
      ⓘ
    </span>
    <span
      className="pointer-events-none absolute left-1/2 top-full z-30 mt-1 hidden w-72 -translate-x-1/2 rounded-xl border border-slate-200 bg-white p-2.5 text-[10px] font-normal leading-relaxed text-slate-600 shadow-lg group-hover/i:block"
    >
      {text}
    </span>
  </span>
);

// ─────────────────────────── 降级占位 ───────────────────────────

/**
 * 不可用 ≠ 0。任何缺数据的图都必须显式给出原因，不能留白也不能画成 0
 * （空图与「值恰好是 0」在视觉上无法区分，这是本项目已有教训）。
 */
export const Degraded: React.FC<{ reason: string; compact?: boolean }> = ({ reason, compact }) => (
  <div
    className={`flex flex-col items-center justify-center gap-1 rounded-xl border border-dashed border-amber-200 bg-amber-50/40 text-center ${
      compact ? 'py-3 px-3' : 'py-6 px-4 h-full'
    }`}
  >
    <span className="text-[10px] font-bold text-amber-700">该项数据不可用</span>
    <span className="text-[10px] leading-relaxed text-amber-600/90 max-w-lg">{reason}</span>
  </div>
);

/** 空序列守卫：全 None / 空数组时不要画空图 */
export const hasData = (a: Array<number | null> | null | undefined): boolean =>
  !!a && a.some((v) => v != null && Number.isFinite(v));

// ─────────────────────────── ECharts 通用片段 ───────────────────────────

/** 统一网格：留白随轴标签长度收敛，避免每张图各写一套 */
export const grid = (o: Partial<{ left: number; right: number; top: number; bottom: number }> = {}) => ({
  left: 48, right: 16, top: 20, bottom: 26, ...o,
});

export const catAxis = (data: string[], interval?: number) => ({
  type: 'category' as const,
  data,
  axisTick: { show: false },
  axisLine: { lineStyle: { color: '#e2e8f0' } },
  axisLabel: { fontSize: 10, color: NEUTRAL, interval: interval ?? axisInterval(data.length) },
});

export const valAxis = (formatter?: string | ((v: number) => string), extra: Record<string, unknown> = {}) => ({
  type: 'value' as const,
  axisLabel: { fontSize: 10, color: NEUTRAL, ...(formatter ? { formatter } : {}) },
  splitLine: { lineStyle: { color: GRID } },
  ...extra,
});

export const tooltip = (extra: Record<string, unknown> = {}) => ({
  trigger: 'axis' as const,
  confine: true,
  backgroundColor: 'rgba(255,255,255,0.97)',
  borderColor: '#e2e8f0',
  textStyle: { fontSize: 11, color: '#334155' },
  ...extra,
});

/** 零轴参考线：正负混杂的图缺了它就读不出方向 */
export const zeroLine = { silent: true, lineStyle: { color: '#cbd5e1', width: 1, type: 'dashed' as const } };
