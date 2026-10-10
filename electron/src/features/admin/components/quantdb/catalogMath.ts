/**
 * 训练因子目录的纯计算（从 AdminTrainingDatasets.tsx 迁出，2026-10-10）。
 *
 * 迁出动机：发布状态条要在页头展示「线上启用数 vs 草稿启用数」，这些口径
 * 必须与发布确认框共用一份实现——两处各算各的，迟早一处先漂移。
 */
import type { QuantDBFactorStat } from '../../types';

/**
 * 数一份目录版本里**启用**的特征数（草稿与已发布版本同一形状，一个函数通吃）。
 *
 * 口径必须是 enabled，**不能**用后端给的 `feature_count`：那个字段是
 * `sum(category.feature_count)`，而计数器在遍历 mapping 时每个都 +1，
 * 不按 enabled 过滤（`quantdb_factor_catalog.py:471`）；训练侧却只用启用的
 * （`trainingUtils.tsx:1145` 的 `feature.enabled !== false`）。发布确认里拿
 * 含 disabled 的数去比大小，会在口径其实没变时报「减少」、真减少时又少报。
 *
 * 这里用 `!== false` 而不是真值判断，是为了和训练侧**逐字同口径**：
 * 后端两个入口都发 `bool(...)`，正常不会有 undefined，但比较口径一旦分家，
 * 这个确认框就开始撒谎。
 */
export const countEnabledFeatures = (catalog: any): number =>
  (catalog?.categories || [])
    .flatMap((category: any) => category.features || [])
    .filter((feature: any) => feature.enabled !== false).length;

/** 启用特征的稳定键（catalog feature 的 `key`=映射的逻辑因子 ID）。 */
const enabledKeys = (catalog: any): Set<string> =>
  new Set(
    (catalog?.categories || [])
      .flatMap((category: any) => category.features || [])
      .filter((feature: any) => feature.enabled !== false)
      .map((feature: any) => String(feature?.key ?? ''))
      .filter((key: string) => key.length > 0),
  );

/**
 * 草稿相对线上版本的启用特征增减（按逻辑因子 ID 逐键比对）。
 *
 * 返回 null 表示「没有可比对象」（首次发布，published 为空）——此时不该
 * 显示 +N/−M，那会把「从无到有」误报成结构变化。
 */
export function diffEnabledFeatures(
  draft: any,
  published: any,
): { added: number; removed: number } | null {
  if (!published) return null;
  const next = enabledKeys(draft);
  const current = enabledKeys(published);
  let added = 0;
  let removed = 0;
  next.forEach((key) => {
    if (!current.has(key)) added += 1;
  });
  current.forEach((key) => {
    if (!next.has(key)) removed += 1;
  });
  return { added, removed };
}

/**
 * 行级统计挂接：物理列名（source_column）优先，逻辑因子 ID（factor）二级兜底。
 *
 * 后端 `stats` 以**物理列名**为键（与 /fields 的 column_name 同源），正常情况
 * 一条直接命中。二级兜底只服务「草稿映射的 key ≠ 物理列名」（改过逻辑 ID）的
 * 行——此时按 key 去统计索引里再找一次。
 *
 * 撞列守卫：兜底命中必须唯一归属，两道闸——
 * 1) 若该 key 同时是**某行的物理列名**，它归那行直接命中，兜底跳过（否则同一条
 *    统计会被两行同时展示，其中一行是假的）；
 * 2) 同一 key 只允许被兜底认领一次。
 * 宁缺毋滥：兜底再拿不到就回 null（渲染「—」），绝不张冠李戴。
 */
export function attachStatsToRows<T extends { source_column: string; factor: string }>(
  rows: T[],
  stats: Record<string, QuantDBFactorStat | null> | null | undefined,
): Array<T & { stat: QuantDBFactorStat | null }> {
  if (!stats) return rows.map((row) => ({ ...row, stat: null }));
  const columnOwners = new Set(rows.map((row) => row.source_column));
  const claimedFallback = new Set<string>();
  return rows.map((row) => {
    const direct = stats[row.source_column] ?? null;
    if (direct) return { ...row, stat: direct };
    const key = row.factor;
    const eligible = key.length > 0 && !columnOwners.has(key) && !claimedFallback.has(key);
    const candidate = eligible ? stats[key] ?? null : null;
    if (candidate) claimedFallback.add(key);
    return { ...row, stat: candidate };
  });
}
