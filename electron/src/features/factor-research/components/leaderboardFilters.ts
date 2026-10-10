/**
 * 因子排行榜 —— 筛选状态与纯函数（与 LeaderboardTab 的 UI 解耦，便于单测）。
 *
 * 约定：
 * - 阈值一律存**原值**（年化 15% 存 0.15）；「% 按显示单位输入」的换算只发生在
 *   输入框层（配置里的 `scale`），这样纯函数与后端数据口径永远一致；
 * - 回撤这类恒负列按**幅度**（|v|）比较（`magnitude`）—— 直接比原始值方向是反的，
 *   用户想说的「回撤不超过 20%」= |max_drawdown| ≤ 0.2；
 * - RankIC 可选「按绝对值」（`absToggle`）：找强因子时不关心方向；
 * - 数值缺失（null / undefined / NaN）的行**不匹配任何数值阈值**——缺失 ≠ 0
 *   （与站内「缺失一律 —」的展示口径一致，不把没算出来的因子当成 0 混进来）。
 */
import type { CategoryFilter, LeaderboardRow } from '../types/factorResearch';
import { matchTagFilter } from './common';

/** 单列数值阈值的原始值（%） */
export interface NumericFilter {
  min?: number;
  max?: number;
  /** 按 |value| 比较（RankIC 可选；回撤列由配置强制） */
  abs?: boolean;
}

export interface LeaderboardFilters {
  /** 因子名 / 代码 文本 */
  name: string;
  /** 前三行业名称包含 */
  industry: string;
  /** 市值风格（'' = 全部） */
  mvStyle: string;
  numeric: Record<string, NumericFilter>;
}

export const EMPTY_LEADERBOARD_FILTERS: LeaderboardFilters = {
  name: '',
  industry: '',
  mvStyle: '',
  numeric: {},
};

/** 列筛选控件形态（与列头漏斗一一对应；无配置 = 该列不提供筛选） */
export interface ColFilterConfig {
  kind: 'text' | 'range' | 'select';
  /** 界面单位 → 原值换算：界面输入 10（%）→ 存 0.1 即 scale=100；默认 1 */
  scale?: number;
  /** 比较 |value|（回撤列） */
  magnitude?: boolean;
  /** 提供 原值 / 绝对值 切换（RankIC 列） */
  absToggle?: boolean;
  /** select 的选项 */
  options?: string[];
  /** 输入说明（占位与注脚） */
  hint?: string;
}

export const COL_FILTERS: Record<string, ColFilterConfig> = {
  name: { kind: 'text', hint: '名称或代码包含…' },
  composite: { kind: 'range', hint: '综合分原值（如 0.5）' },
  annual_return: { kind: 'range', scale: 100, hint: '按百分比填（如 15 表示 15%）' },
  sharpe: { kind: 'range', hint: '夏普原值（如 1）' },
  max_drawdown: { kind: 'range', scale: 100, magnitude: true, hint: '回撤幅度按百分比填（如 20 表示回撤不超过 20%）' },
  win_rate: { kind: 'range', scale: 100, hint: '按百分比填（如 50 表示 50%）' },
  excess_300: { kind: 'range', scale: 100, hint: '按百分比填（如 5 表示 5%）' },
  excess_800: { kind: 'range', scale: 100, hint: '按百分比填（如 5 表示 5%）' },
  ic_mean: { kind: 'range', absToggle: true, hint: 'RankIC 原值（如 0.03）；可切「按绝对值」找强因子' },
  ic_ir: { kind: 'range', hint: 'IC_IR 原值（如 0.3）' },
  median_mv_yi: { kind: 'range', hint: '亿元（如 100）' },
  mv_style: { kind: 'select', options: ['大盘', '中盘', '小盘'] },
  industry: { kind: 'text', hint: '行业名包含…' },
};

export function countActiveFilters(f: LeaderboardFilters): number {
  const numeric = Object.values(f.numeric).filter(
    (x) => x && (x.min !== undefined || x.max !== undefined),
  ).length;
  return (f.name.trim() ? 1 : 0) + (f.industry.trim() ? 1 : 0) + (f.mvStyle ? 1 : 0) + numeric;
}

function numPass(v: number, f: NumericFilter, magnitude?: boolean): boolean {
  const x = magnitude || f.abs ? Math.abs(v) : v;
  if (f.min !== undefined && x < f.min) return false;
  if (f.max !== undefined && x > f.max) return false;
  return true;
}

/**
 * 应用全部筛选（分类限定 + 列筛选 + 标签）。排序不在这里做（UI 层的事）。
 */
export function applyLeaderboardFilters(
  rows: LeaderboardRow[],
  filters: LeaderboardFilters,
  tagFilter: string[],
  categoryFilter: CategoryFilter | null,
): LeaderboardRow[] {
  const q = filters.name.trim().toLowerCase();
  const ind = filters.industry.trim();
  return rows.filter((r) => {
    if (categoryFilter && !(r.l1 === categoryFilter.l1 && (!categoryFilter.l2 || r.l2 === categoryFilter.l2))) {
      return false;
    }
    if (q && !(r.name_cn.toLowerCase().includes(q) || r.code.toLowerCase().includes(q))) return false;
    if (filters.mvStyle && r.mv_style !== filters.mvStyle) return false;
    if (ind && !(r.top_industries || []).some((x) => x.name.includes(ind))) return false;

    for (const [key, f] of Object.entries(filters.numeric)) {
      if (!f || (f.min === undefined && f.max === undefined)) continue;
      const raw = r[key as keyof LeaderboardRow];
      const num = typeof raw === 'number' ? raw : NaN;
      if (Number.isNaN(num)) return false; // 缺失不匹配任何阈值
      if (!numPass(num, f, COL_FILTERS[key]?.magnitude)) return false;
    }

    return matchTagFilter(r.env_tag, r.time_tag, tagFilter);
  });
}
