/**
 * 排行榜「自选」契约（2026-10-10）：
 *
 * 用户：几百个因子里挑出来看的就那十几个，每次都要重新翻。
 * 于是每行最前面加 ★ 收藏（按数据集持久化到 localStorage），筛选栏给
 * 「自选 N」开关一键只看收藏；「清除筛选」连自选视图一起复位。
 * 点星标只切换收藏，**不触发**行跳转（和行内标签芯片同一条纪律）。
 */
import React from 'react';
import { describe, test, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
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

const ROWS: LeaderboardRow[] = [
  row({ code: 'FAV_1', name_cn: '动量A' }),
  row({ code: 'FAV_2', name_cn: '反转B' }),
  row({ code: 'FAV_3', name_cn: '波动C' }),
];

/** 私人库的收藏键（与因子报告左栏的键是两套集合，互不串） */
const KEY = 'qm:factor-research:lb-favs:private';

function renderTab(over: Partial<React.ComponentProps<typeof LeaderboardTab>> = {}) {
  return render(
    <LeaderboardTab
      rows={ROWS}
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
      dataset="private"
      {...over}
    />,
  );
}

beforeEach(() => {
  localStorage.clear();
});

describe('LeaderboardTab：自选收藏', () => {
  test('点行首星标收藏/取消：写入 localStorage（按数据集），计数同步', () => {
    renderTab();

    fireEvent.click(screen.getByTestId('lb-fav-FAV_1'));
    expect(JSON.parse(localStorage.getItem(KEY) || '[]')).toEqual(['FAV_1']);
    expect(screen.getByTestId('lb-favs-only').textContent).toContain('1');

    fireEvent.click(screen.getByTestId('lb-fav-FAV_1'));
    expect(JSON.parse(localStorage.getItem(KEY) || '[]')).toEqual([]);
    expect(screen.getByTestId('lb-favs-only').textContent).toContain('0');
  });

  test('「只看自选」：列表只剩收藏行，再点恢复全量', () => {
    renderTab();
    fireEvent.click(screen.getByTestId('lb-fav-FAV_1'));
    fireEvent.click(screen.getByTestId('lb-fav-FAV_3'));

    fireEvent.click(screen.getByTestId('lb-favs-only'));
    expect(screen.getByText('动量A')).toBeTruthy();
    expect(screen.getByText('波动C')).toBeTruthy();
    expect(screen.queryByText('反转B')).toBeNull();

    fireEvent.click(screen.getByTestId('lb-favs-only'));
    expect(screen.getByText('反转B')).toBeTruthy();
  });

  test('「清除筛选」把只看自选也一并复位', () => {
    renderTab();
    fireEvent.click(screen.getByTestId('lb-fav-FAV_1'));
    fireEvent.click(screen.getByTestId('lb-favs-only'));
    expect(screen.queryByText('反转B')).toBeNull();

    fireEvent.click(screen.getByTestId('lb-clear-filters'));
    expect(screen.getByText('反转B')).toBeTruthy();
  });

  test('数据集隔离：经典库的收藏不出现在私人库', () => {
    localStorage.setItem('qm:factor-research:lb-favs:classic', JSON.stringify(['FAV_1']));
    renderTab(); // dataset="private"

    expect(screen.getByTestId('lb-favs-only').textContent).toContain('0');
  });

  test('一个收藏都没有时打开「只看自选」：给出怎么收藏的提示', () => {
    renderTab();
    fireEvent.click(screen.getByTestId('lb-favs-only'));

    expect(screen.getByText(/还没有收藏因子/)).toBeTruthy();
  });

  test('点星标不触发行跳转', () => {
    const onOpenSingle = vi.fn();
    renderTab({ onOpenSingle });

    fireEvent.click(screen.getByTestId('lb-fav-FAV_1'));

    expect(onOpenSingle).not.toHaveBeenCalled();
  });
});
