/**
 * 交易记录「Excel 式表头筛选」的纯逻辑（无 React 依赖，单独测）。
 *
 * 语义按 Excel 口径：
 * - `undefined` = 该列未筛选（全选）；
 * - 数组 = 显式勾选的**显示值**子集，行展示当且仅当显示值在子集里；
 * - `[]` = 一个都不勾 → 一行都不展示；
 * - 多列叠加取交集（AND），单列内取并集（OR）——与顶部栏的搜索/状态快捷片
 *   再叠加，整体仍是 AND（各层各自收窄）。
 */

/** 列 → 勾选值集合；`undefined` 表示该列未筛选 */
export type ColumnFilterState = Record<string, readonly string[] | undefined>;

/** 列 → 取「该行在这列上显示的那个字符串」 */
export type ColumnAccessors<T> = Record<string, (row: T) => string>;

export function isFilterActive(selected: readonly string[] | undefined): boolean {
  return selected !== undefined;
}

export function applyColumnFilters<T>(
  rows: readonly T[],
  filters: ColumnFilterState,
  accessors: ColumnAccessors<T>,
): T[] {
  const active = Object.entries(filters).filter(
    (entry): entry is [string, readonly string[]] => entry[1] !== undefined,
  );
  if (active.length === 0) {
    return [...rows];
  }

  return rows.filter((row) =>
    active.every(([column, selected]) => {
      const accessor = accessors[column];
      // 取了名字却没有取值器的列忽略不筛，而不是把行全滤掉（写错列名不至于清空整个表）
      if (!accessor) {
        return true;
      }
      return selected.includes(accessor(row));
    }),
  );
}

/** 该列当前出现过的显示值（去重、排序、丢空串），供筛选面板列选项 */
export function collectUniqueValues<T>(rows: readonly T[], accessor: (row: T) => string): string[] {
  const seen = new Set<string>();
  for (const row of rows) {
    const value = accessor(row);
    if (value) {
      seen.add(value);
    }
  }
  return Array.from(seen).sort();
}
