/**
 * 排行榜筛选契约（2026-10-10 增强）：
 *
 * 1. 列头漏斗 = 按列筛选：阈值按**显示单位**输入（年化筛 "15" 表示 15%，不是 0.15）；
 * 2. 标签单个筛选：点一个标签就只筛它；组内（环境/时效各自）OR、组间 AND ——
 *    「牛市进攻型 + 近期转强」应落到同时满足两者的因子，而不是并集；
 * 3. 行内标签芯片点击 = 切换该标签筛选，且**不触发**行跳转（否则筛标签会误开单因子页）；
 * 4. 「清除筛选」一键回到全量（列筛选本地清空，标签/分类交回调由页面清）。
 */
import React from 'react';
import { describe, test, expect, vi } from 'vitest';
import { render, screen, fireEvent, within } from '@testing-library/react';
import { LeaderboardTab } from '../LeaderboardTab';
import type { LeaderboardRow } from '../../types/factorResearch';

function row(over: Partial<LeaderboardRow>): LeaderboardRow {
  return {
    code: 'x',
    rank: 1,
    name_cn: 'x',
    l1: '大类',
    l2: '子类',
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
    env_tag: '',
    time_tag: '',
    ...over,
  } as LeaderboardRow;
}

function renderTab(
  rows: LeaderboardRow[],
  over: Partial<React.ComponentProps<typeof LeaderboardTab>> = {},
) {
  return render(
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

/** 三行样本：覆盖 环境/时效 两组标签与不同数值档位。 */
const ROWS: LeaderboardRow[] = [
  row({ code: 'MOM_A', name_cn: '动量A', sharpe: 2.0, annual_return: 0.2, ic_mean: 0.05, env_tag: '牛市进攻型', time_tag: '近期转强', mv_style: '大盘' }),
  row({ code: 'REV_B', name_cn: '反转B', sharpe: 0.4, annual_return: 0.05, ic_mean: -0.02, env_tag: '熊市防御型', time_tag: '长期稳定型', mv_style: '小盘' }),
  row({ code: 'VOL_C', name_cn: '波动C', sharpe: 1.2, annual_return: 0.12, ic_mean: 0.01, env_tag: '牛市进攻型', time_tag: '长期稳定型', mv_style: '中盘' }),
];

describe('LeaderboardTab：列头筛选', () => {
  test('夏普下限：只剩达标行，且命中计数同步', () => {
    renderTab(ROWS);

    fireEvent.click(screen.getByLabelText('筛选 夏普'));
    fireEvent.change(screen.getByTestId('lb-fmin'), { target: { value: '1' } });

    expect(screen.getByText('动量A')).toBeTruthy();
    expect(screen.getByText('波动C')).toBeTruthy();
    expect(screen.queryByText('反转B')).toBeNull();
    expect(screen.getByText(/命中 2 \/ 3/)).toBeTruthy();
  });

  test('百分比列按显示单位输入：年化填 15 = 15%（不是 0.15 的小数口径）', () => {
    renderTab(ROWS);

    fireEvent.click(screen.getByLabelText('筛选 年化'));
    fireEvent.change(screen.getByTestId('lb-fmin'), { target: { value: '15' } });

    expect(screen.getByText('动量A')).toBeTruthy(); // 20% ≥ 15%
    expect(screen.queryByText('波动C')).toBeNull(); // 12% < 15%
    expect(screen.queryByText('反转B')).toBeNull();
  });

  test('因子列文本筛选：名称或代码包含即命中（忽略大小写）', () => {
    renderTab(ROWS);

    fireEvent.click(screen.getByLabelText('筛选 因子'));
    fireEvent.change(screen.getByTestId('lb-ftext'), { target: { value: 'rev' } });

    expect(screen.getByText('反转B')).toBeTruthy(); // code REV_B
    expect(screen.queryByText('动量A')).toBeNull();
  });

  test('市值风格下拉：只留该风格的行', () => {
    renderTab(ROWS);

    fireEvent.click(screen.getByLabelText('筛选 市值风格'));
    fireEvent.change(screen.getByTestId('lb-fmv'), { target: { value: '大盘' } });

    expect(screen.getByText('动量A')).toBeTruthy();
    expect(screen.queryByText('反转B')).toBeNull();
    expect(screen.queryByText('波动C')).toBeNull();
  });
});

describe('LeaderboardTab：标签筛选（组内 OR / 组间 AND）', () => {
  test('单个标签：点一个只筛它', () => {
    renderTab(ROWS, { tagFilter: ['熊市防御型'] });

    expect(screen.getByText('反转B')).toBeTruthy();
    expect(screen.queryByText('动量A')).toBeNull();
    expect(screen.queryByText('波动C')).toBeNull();
  });

  test('组间 AND：环境 + 时效同时满足才留下', () => {
    renderTab(ROWS, { tagFilter: ['牛市进攻型', '近期转强'] });

    expect(screen.getByText('动量A')).toBeTruthy(); // 牛市进攻型 且 近期转强
    expect(screen.queryByText('波动C')).toBeNull(); // 牛市进攻型 但长期稳定型
    expect(screen.queryByText('反转B')).toBeNull();
  });

  test('组内 OR：同组两个环境标签 = 并集', () => {
    renderTab(ROWS, { tagFilter: ['牛市进攻型', '熊市防御型'] });

    expect(screen.getByText('动量A')).toBeTruthy();
    expect(screen.getByText('波动C')).toBeTruthy();
    expect(screen.getByText('反转B')).toBeTruthy();
  });

  test('点行内标签芯片：发起该标签切换，且不触发行跳转', () => {
    const onToggleTag = vi.fn();
    const onOpenSingle = vi.fn();
    renderTab(ROWS, { onToggleTag, onOpenSingle });

    const tr = screen.getByText('动量A').closest('tr')!;
    fireEvent.click(within(tr).getByText('牛市进攻型'));

    expect(onToggleTag).toHaveBeenCalledWith('牛市进攻型');
    expect(onOpenSingle).not.toHaveBeenCalled();
  });
});

describe('LeaderboardTab：清除筛选', () => {
  test('点「清除筛选」：本地列筛选清空 + 交回调清标签/分类', () => {
    const onClearCategoryFilter = vi.fn();
    const onClearTagFilter = vi.fn();
    renderTab(ROWS, {
      tagFilter: ['牛市进攻型'],
      categoryFilter: { l1: '大类', l2: null },
      onClearCategoryFilter,
      onClearTagFilter,
    });

    fireEvent.click(screen.getByLabelText('筛选 夏普'));
    fireEvent.change(screen.getByTestId('lb-fmin'), { target: { value: '1' } });
    expect(screen.queryByText('反转B')).toBeNull(); // 已被夏普筛掉（且标签也不匹配）

    fireEvent.click(screen.getByTestId('lb-clear-filters'));

    expect(onClearCategoryFilter).toHaveBeenCalledTimes(1);
    expect(onClearTagFilter).toHaveBeenCalledTimes(1);
    // 列筛选已本地清空：反转B 虽然仍不在标签筛选里（prop 未变），但夏普条件不再拦它——
    // 用命中计数验证（只有标签还在起作用：动量A/波动C 两行）
    expect(screen.getByText(/命中 2 \/ 3/)).toBeTruthy();
  });
});
