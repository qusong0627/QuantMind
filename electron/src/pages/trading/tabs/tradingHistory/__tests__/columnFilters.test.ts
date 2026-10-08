/**
 * 交易记录「Excel 式表头筛选」的纯逻辑。
 *
 * 语义按 Excel 口径钉住：
 * - `undefined` = 未筛选（全选）；
 * - 数组 = 显式勾选的子集，行展示当且仅当它的**显示值**在子集里；
 * - `[]` = 一个都不勾 → 一行都不展示（不是"恢复全部"——那是最容易写反的一格）；
 * - 多列叠加取交集（AND），单列内取并集（OR）。
 */
import { describe, it, expect } from 'vitest';
import {
  applyColumnFilters,
  collectUniqueValues,
  isFilterActive,
  type ColumnFilterState,
} from '../columnFilters';

interface Row {
  date: string;
  direction: string;
  action: string;
  status: string;
}

const ACCESSORS = {
  date: (r: Row) => r.date,
  direction: (r: Row) => r.direction,
  action: (r: Row) => r.action,
  status: (r: Row) => r.status,
};

const rows: Row[] = [
  { date: '2026/10/08', direction: '买入', action: '买入开仓', status: '已成交' },
  { date: '2026/10/08', direction: '卖出', action: '卖出平仓', status: '已成交' },
  { date: '2026/10/07', direction: '买入', action: '买入开仓', status: '已撤单' },
];

describe('isFilterActive', () => {
  it('未筛选（undefined）不算激活', () => {
    expect(isFilterActive(undefined)).toBe(false);
  });

  it('勾了子集算激活；全部清空（空数组）也算激活', () => {
    expect(isFilterActive(['买入'])).toBe(true);
    expect(isFilterActive([])).toBe(true);
  });
});

describe('applyColumnFilters', () => {
  it('没有任何筛选时原样返回', () => {
    const filters: ColumnFilterState = {};
    expect(applyColumnFilters(rows, filters, ACCESSORS)).toEqual(rows);
  });

  it('值为 undefined 的列不参与筛选', () => {
    const filters: ColumnFilterState = { direction: undefined };
    expect(applyColumnFilters(rows, filters, ACCESSORS)).toEqual(rows);
  });

  it('单列子集：只保留显示值在勾选集合里的行', () => {
    const filters: ColumnFilterState = { date: ['2026/10/08'] };
    expect(applyColumnFilters(rows, filters, ACCESSORS)).toEqual(rows.slice(0, 2));
  });

  it('多列叠加取交集（AND）', () => {
    const filters: ColumnFilterState = { date: ['2026/10/08'], direction: ['买入'] };
    expect(applyColumnFilters(rows, filters, ACCESSORS)).toEqual([rows[0]]);
  });

  it('单列勾选多个值取并集（OR）', () => {
    const filters: ColumnFilterState = { status: ['已撤单', '已成交'] };
    expect(applyColumnFilters(rows, filters, ACCESSORS)).toEqual(rows);
  });

  it('空数组 = 一个都不勾 = 一行都不展示（Excel 口径，别写成恢复全部）', () => {
    const filters: ColumnFilterState = { direction: [] };
    expect(applyColumnFilters(rows, filters, ACCESSORS)).toEqual([]);
  });

  it('筛选键没有对应取值器时忽略该列，而不是把行全滤掉', () => {
    const filters: ColumnFilterState = { unknownColumn: ['whatever'] };
    expect(applyColumnFilters(rows, filters, ACCESSORS)).toEqual(rows);
  });
});

describe('collectUniqueValues', () => {
  it('去重、排序、丢弃空值', () => {
    const withEmpty: Row[] = [...rows, { date: '', direction: '买入', action: '', status: '已成交' }];
    expect(collectUniqueValues(withEmpty, ACCESSORS.date)).toEqual(['2026/10/07', '2026/10/08']);
    expect(collectUniqueValues(rows, ACCESSORS.status)).toEqual(['已成交', '已撤单']);
  });

  it('空行集返回空数组', () => {
    expect(collectUniqueValues([], ACCESSORS.date)).toEqual([]);
  });
});
