/**
 * 排行榜「因子」列的宽度契约。
 *
 * 用户的原话是「因子列不用那么宽、外观挤压后面的显示界面了」。根因不是列宽没设，
 * 而是这一列**把同一串字符印了两遍**：私人因子库 2754/2754 个因子的 `name_cn`
 * 与 `code` 逐字相同（实测），而后端模板旧代码里两个都渲染——于是这一列的内容
 * 天然比别人长一倍，在 `table-layout: auto` 下把后面的 9 个数值列挤成一条缝。
 *
 * 所以这里守的不是「宽度是多少像素」（那是样式，改版就会变），而是**同一行里
 * 这个名字出现几次**：私人库一次、经典库两次（那边 name_cn 是「动量」这类中文名，
 * code 另有其值，并列才有信息量）。
 */
import React from 'react';
import { describe, test, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { LeaderboardTab } from '../LeaderboardTab';
import type { LeaderboardRow } from '../../types/factorResearch';

function row(over: Partial<LeaderboardRow>): LeaderboardRow {
  return {
    code: 'x',
    rank: 1,
    name_cn: 'x',
    l1: '私人因子库',
    l2: 'alpha_library',
    composite: 1,
    annual_return: 0.1,
    sharpe: 1,
    max_drawdown: -0.1,
    win_rate: 0.5,
    excess_300: 0.01,
    excess_800: 0.01,
    ic_mean: 0.03,
    ic_ir: 0.3,
    median_mv_yi: 100,
    ...over,
  } as LeaderboardRow;
}

function renderTab(
  rows: LeaderboardRow[],
  over: Partial<React.ComponentProps<typeof LeaderboardTab>> = {},
) {
  render(
    <LeaderboardTab
      rows={rows}
      loading={false}
      error={null}
      selected={[]}
      tagFilter={[]}
      categoryFilter={null}
      onClearCategoryFilter={vi.fn()}
      n={30}
      onNChange={vi.fn()}
      onToggle={vi.fn()}
      onToggleTag={vi.fn()}
      onSendCompare={vi.fn()}
      onSendCompose={vi.fn()}
      onOpenSingle={vi.fn()}
      meta={{}}
      {...over}
    />,
  );
}

/** 该名字在整行里出现了几次。 */
const occurrencesInRow = (text: string): number => {
  const tr = screen.getByText(text).closest('tr');
  expect(tr).toBeTruthy();
  return (tr!.textContent || '').split(text).length - 1;
};

describe('LeaderboardTab：因子列不重复印同一串字符', () => {
  test('私人库（name_cn 与 code 相同）只渲染一次', () => {
    renderTab([row({ code: 'a101_001', name_cn: 'a101_001' })]);

    expect(occurrencesInRow('a101_001')).toBe(1);
  });

  test('经典库（name_cn 与 code 不同）两个都要在——少一个就丢了信息', () => {
    renderTab([
      row({ code: 'MOM12_1', name_cn: '动量12-1', l1: '动量', l2: '动量' }),
    ]);

    expect(screen.getByText('动量12-1')).toBeTruthy();
    expect(screen.getByText('MOM12_1')).toBeTruthy();
  });

  test('数据质量徽章不会被列宽裁掉（它们是告警，裁了等于没告警）', () => {
    renderTab([
      row({ code: 'a101_002', name_cn: 'a101_002', insufficient: true, n_months: 6, suspicious: true }),
    ]);

    expect(screen.getByText(/数据不足（6 月）/)).toBeTruthy();
    expect(screen.getByText('疑似未来函数')).toBeTruthy();
  });
});

/**
 * 分类限定（左侧目录点分类 → 榜单只排该类）与首载加载提示。
 *
 * 限定只该改变**视图**（榜单内的名次/综合分本是全库截面口径，不清洗数值），
 * 且要有一眼可见的出口（顶部徽章）；首载数秒必须给可读提示而不是无字灰屏。
 */
describe('LeaderboardTab：分类限定与加载提示', () => {
  const ROWS = [
    row({ code: 'rd_x1', name_cn: 'rd_x1', l1: 'rd_mined', l2: 'rd_mined', composite: 0.9 }),
    row({ code: 'gap_x2', name_cn: 'gap_x2', l1: 'gap_mined', l2: 'gap_mined', composite: 0.5 }),
    row({ code: 'a101', name_cn: 'a101', l1: 'alpha_library', l2: 'a101', composite: 0.1 }),
  ];

  test('L1 限定只留该类的行；标题给「本类 / 全部」两个计数', () => {
    renderTab(ROWS, { categoryFilter: { l1: 'rd_mined', l2: null } });

    expect(screen.getByText('rd_x1')).toBeTruthy();
    expect(screen.getByText('因子排行榜（1 / 全部 3）')).toBeTruthy();
    expect(screen.queryByText('gap_x2')).toBeNull();
    expect(screen.queryByText('a101')).toBeNull();
  });

  test('L2 限定在 L1 内再收紧', () => {
    renderTab(
      [
        row({ code: 'rd_a', name_cn: 'rd_a', l1: 'rd_mined', l2: 'sub_a' }),
        row({ code: 'rd_b', name_cn: 'rd_b', l1: 'rd_mined', l2: 'sub_b' }),
      ],
      { categoryFilter: { l1: 'rd_mined', l2: 'sub_a' } },
    );

    expect(screen.getByText('rd_a')).toBeTruthy();
    expect(screen.queryByText('rd_b')).toBeNull();
  });

  test('徽章显分类名，点击把「清除限定」交回页面', () => {
    const onClearCategoryFilter = vi.fn();
    renderTab(ROWS, {
      categoryFilter: { l1: 'rd_mined', l2: null },
      onClearCategoryFilter,
    });

    fireEvent.click(screen.getByText(/分类：rd_mined/));
    expect(onClearCategoryFilter).toHaveBeenCalledTimes(1);
  });

  test('限定后一行都不剩：空态行指出去哪清除，而不是一张无名空表', () => {
    renderTab(ROWS, { categoryFilter: { l1: '不存在的类', l2: null } });

    expect(screen.getByText(/该分类下没有因子/)).toBeTruthy();
  });

  test('首次加载给可读提示，不是无字灰屏', () => {
    renderTab([], { loading: true });

    expect(screen.getByText('正在加载因子数据…')).toBeTruthy();
    expect(screen.getByText(/首次进入需要数秒/)).toBeTruthy();
  });

  test('已有数据时重算：表格留在原位变暗 + 「重算中」角标，而不是整块替换', () => {
    renderTab(ROWS, { loading: true });

    expect(screen.getByText('重算中…')).toBeTruthy();
    expect(screen.getByText('rd_x1')).toBeTruthy();
  });
});
