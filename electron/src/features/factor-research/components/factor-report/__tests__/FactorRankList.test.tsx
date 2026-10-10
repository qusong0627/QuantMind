/**
 * 因子报告左栏（FactorRankList）筛选/排序/自选契约（2026-10-10 增强）：
 *
 * 用户原话：「那么多因子，我要选一些因子出来看，不知道怎么选，一个一个看太久」。
 * 于是左栏从「搜索 + 库筛选」升级为研究控制台：
 * - 排序可切（|IC| / IC / ICIR / 多空 / 换手 / 名称）+ 升降序；
 * - 质量门槛（|ICIR| ≥ / |IC| ≥ / 换手 ≤）与方向（正向/反向）；
 * - ★ 自选收藏（localStorage 按数据集持久化）+ 只看自选；
 * - 选中项被筛掉时给出提示与恢复出口，避免「明细在右、左栏找不到人」。
 */
import React from 'react';
import { describe, test, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { FactorRankList } from '../FactorRankList';
import type { FactorSummary } from '../../../types/factorReport';

function f(over: Partial<FactorSummary>): FactorSummary {
  return {
    name: 'f',
    library: 'alpha158',
    ic_mean: 0.01,
    icir: 0.1,
    t_value: 1,
    win_rate: 0.5,
    quantiles: [],
    ls_mean: 0.01,
    monotonicity: null,
    turnover: 0.3,
    ...over,
  };
}

const FACTORS: FactorSummary[] = [
  f({ name: 'f1', display_name: '动量因子', ic_mean: 0.05, icir: 1.5, turnover: 0.2, ls_mean: 0.02, library: 'alpha158' }),
  f({ name: 'f2', display_name: '反转因子', ic_mean: -0.06, icir: 0.8, turnover: 0.5, ls_mean: 0.03, library: 'alpha101' }),
  f({ name: 'f3', display_name: '弱因子', ic_mean: 0.01, icir: 0.1, turnover: 0.9, ls_mean: 0.001, library: 'gtja191' }),
  f({ name: 'f4', display_name: '强反向', ic_mean: -0.08, icir: 1.2, turnover: 0.35, ls_mean: 0.04, library: 'alpha158' }),
];

function renderList(over: Partial<React.ComponentProps<typeof FactorRankList>> = {}) {
  return render(
    <FactorRankList
      factors={FACTORS}
      selected={null}
      onSelect={vi.fn()}
      dataset="alpha_library"
      {...over}
    />,
  );
}

/** 当前列表的行序（按展示名）。 */
function order(): string[] {
  return Array.from(document.querySelectorAll('[data-testid^="frl-item-"]')).map(
    (el) => el.getAttribute('data-testid')!.replace('frl-item-', ''),
  );
}

beforeEach(() => {
  localStorage.clear();
});

describe('FactorRankList：排序', () => {
  test('默认按 |IC| 降序（强反向因子 f4 在最前）', () => {
    renderList();
    expect(order()).toEqual(['f4', 'f2', 'f1', 'f3']);
  });

  test('切换排序键到 ICIR：顺序随之变化', () => {
    renderList();
    fireEvent.change(screen.getByTestId('frl-sort'), { target: { value: 'icir' } });
    expect(order()).toEqual(['f1', 'f4', 'f2', 'f3']);
  });

  test('方向按钮切升序：顺序整体反转（空值沉底规则除外）', () => {
    renderList();
    fireEvent.click(screen.getByTestId('frl-sort-dir'));
    expect(order()).toEqual(['f3', 'f1', 'f2', 'f4']);
  });

  test('按换手排序：默认升序（低换手优先），切到换手自动换方向', () => {
    renderList();
    fireEvent.change(screen.getByTestId('frl-sort'), { target: { value: 'turnover' } });
    expect(order()).toEqual(['f1', 'f4', 'f2', 'f3']);
  });
});

describe('FactorRankList：筛选', () => {
  test('|ICIR| 门槛 0.5：弱因子被剔除（顺序仍按当前排序键 |IC|）', () => {
    renderList();
    fireEvent.change(screen.getByTestId('frl-min-icir'), { target: { value: '0.5' } });
    expect(order()).toEqual(['f4', 'f2', 'f1']);
  });

  test('|IC| 门槛 0.04：只留强因子（正反不限）', () => {
    renderList();
    fireEvent.change(screen.getByTestId('frl-min-ic'), { target: { value: '0.04' } });
    expect(order()).toEqual(['f4', 'f2', 'f1']);
  });

  test('换手 ≤ 30%：高换手被剔除', () => {
    renderList();
    fireEvent.change(screen.getByTestId('frl-max-turnover'), { target: { value: '30' } });
    expect(order()).toEqual(['f1']);
  });

  test('方向=反向：只留 IC<0 的因子', () => {
    renderList();
    fireEvent.click(screen.getByTestId('frl-dir-neg'));
    expect(order()).toEqual(['f4', 'f2']);
  });

  test('搜索关键词仍按名称/代码过滤', () => {
    renderList();
    fireEvent.change(screen.getByPlaceholderText(/搜索因子/), { target: { value: '动量' } });
    expect(order()).toEqual(['f1']);
  });

  test('命中计数展示「N / M」', () => {
    renderList();
    expect(screen.getByText('4 / 4')).toBeTruthy();
    fireEvent.change(screen.getByTestId('frl-min-icir'), { target: { value: '1' } });
    expect(screen.getByText('2 / 4')).toBeTruthy();
  });
});

describe('FactorRankList：自选收藏', () => {
  test('点亮收藏：计数出现在「自选」开关上，并写入 localStorage（按数据集）', () => {
    renderList();
    fireEvent.click(screen.getByTestId('frl-fav-f1'));
    fireEvent.click(screen.getByTestId('frl-fav-f4'));

    expect(screen.getByTestId('frl-favs-only').textContent).toContain('2');
    const raw = localStorage.getItem('qm:factor-report:favs:alpha_library') || '';
    expect(JSON.parse(raw).sort()).toEqual(['f1', 'f4']);
  });

  test('只看自选：列表只剩收藏项；再点恢复全量', () => {
    renderList();
    fireEvent.click(screen.getByTestId('frl-fav-f2'));

    fireEvent.click(screen.getByTestId('frl-favs-only'));
    expect(order()).toEqual(['f2']);

    fireEvent.click(screen.getByTestId('frl-favs-only'));
    expect(order()).toHaveLength(4);
  });

  test('切数据集读各自的收藏（不串库）', () => {
    localStorage.setItem('qm:factor-report:favs:other_ds', JSON.stringify(['f3']));
    renderList({ dataset: 'other_ds' });
    expect(screen.getByTestId('frl-favs-only').textContent).toContain('1');
  });
});

describe('FactorRankList：选中项被筛选挡住', () => {
  test('给出提示并可一键恢复显示', () => {
    renderList({ selected: 'f3' });
    fireEvent.change(screen.getByTestId('frl-min-icir'), { target: { value: '0.5' } });

    // f3 被门槛筛掉：提示条说明它在哪、给出恢复出口
    expect(screen.getByTestId('frl-selected-hidden').textContent).toContain('f3');

    fireEvent.click(screen.getByText('恢复显示'));
    expect(order()).toContain('f3');
  });

  test('选中项仍在筛选结果里时不出现提示', () => {
    renderList({ selected: 'f1' });
    fireEvent.change(screen.getByTestId('frl-min-icir'), { target: { value: '0.5' } });
    expect(screen.queryByTestId('frl-selected-hidden')).toBeNull();
  });
});
