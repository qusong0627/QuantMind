/**
 * 因子研究左侧目录 —— 两种视图与分类点击契约。
 *
 * 1. **分类（树）**：点分类名（L1/L2）= 把右侧排行榜限定到该类、再点取消；
 *    折叠箭头是**独立**的按钮 —— 「看子类」和「筛榜单」是两个动作，合并成一键
 *    后点箭头会顺带改榜单、点名字又折叠树，用户永远猜不到哪个动作会发生；
 * 2. **列表（平铺）**：整个列表按综合分降序，不用切到右侧就能排序浏览；
 * 3. **加载态**：目录 1.1 MB 在途时要显「正在加载」，不能落成「无匹配因子」——
 *    后者会把一个网络问题说成「你的搜索没有结果」。
 */
import React from 'react';
import { describe, test, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { CatalogSidebar } from '../CatalogSidebar';
import type { CategoryFilter, FactorMeta, LeaderboardRow } from '../../types/factorResearch';

function meta(over: Partial<FactorMeta>): FactorMeta {
  return {
    code: 'x',
    name_cn: 'x',
    l1: '大类甲',
    l2: '子类甲一',
    direction: 1,
    description: '',
    formula: '',
    wind_source: '',
    env_tag: '',
    time_tag: '',
    available: true,
    unavailable_reason: '',
    ...over,
  };
}

function lbRow(code: string, composite: number): LeaderboardRow {
  return {
    code,
    composite,
    rank: 1,
    name_cn: code,
    l1: '',
    l2: '',
    env_tag: '',
    time_tag: '',
  } as LeaderboardRow;
}

const FACTORS: FactorMeta[] = [
  meta({ code: 'fA1', name_cn: '因子A1', l1: '大类甲', l2: '子类甲一' }),
  meta({ code: 'fA2', name_cn: '因子A2', l1: '大类甲', l2: '子类甲二' }),
  meta({ code: 'fB1', name_cn: '因子B1', l1: '大类乙', l2: '子类乙一' }),
];

const ROWS = [lbRow('fA1', 0.5), lbRow('fA2', 0.1), lbRow('fB1', 0.9)];

interface SidebarOver {
  factors?: FactorMeta[];
  rows?: LeaderboardRow[];
  loading?: boolean;
  categoryFilter?: CategoryFilter | null;
  onSelectCategory?: (f: CategoryFilter | null) => void;
}

function renderSidebar(over: SidebarOver = {}) {
  const rows = over.rows ?? ROWS;
  return render(
    <CatalogSidebar
      factors={over.factors ?? FACTORS}
      l1Order={['大类甲', '大类乙']}
      rowsByCode={new Map(rows.map((r) => [r.code, r]))}
      selected={[]}
      activeCode={null}
      tagFilter={[]}
      loading={over.loading}
      categoryFilter={over.categoryFilter ?? null}
      onSelectCategory={over.onSelectCategory ?? vi.fn()}
      onToggle={vi.fn()}
      onOpen={vi.fn()}
    />,
  );
}

describe('CatalogSidebar：分类点击与视图模式', () => {
  test('点 L1 分类名发起大类限定（l2=null）；箭头只折叠、绝不筛榜', () => {
    const onSelectCategory = vi.fn();
    renderSidebar({ onSelectCategory });

    // 提示行把两个动作讲明白
    expect(screen.getByText(/点分类名 → 右侧只排这一类/)).toBeTruthy();

    fireEvent.click(screen.getByText('大类甲'));
    expect(onSelectCategory).toHaveBeenCalledWith({ l1: '大类甲', l2: null });

    // 折叠箭头：只收起来，不发筛选
    onSelectCategory.mockClear();
    fireEvent.click(screen.getByLabelText('折叠 大类甲'));
    expect(onSelectCategory).not.toHaveBeenCalled();
    expect(screen.queryByText('子类甲一')).toBeNull();
  });

  test('已限定的 L1 再点一次 = 取消限定（回全部）', () => {
    const onSelectCategory = vi.fn();
    renderSidebar({ onSelectCategory, categoryFilter: { l1: '大类甲', l2: null } });

    fireEvent.click(screen.getByText('大类甲'));
    expect(onSelectCategory).toHaveBeenCalledWith(null);
  });

  test('点 L2 分类名限定到小类', () => {
    const onSelectCategory = vi.fn();
    renderSidebar({ onSelectCategory });

    fireEvent.click(screen.getByText('子类甲二'));
    expect(onSelectCategory).toHaveBeenCalledWith({ l1: '大类甲', l2: '子类甲二' });
  });

  test('「列表」模式：整个列表按综合分降序平铺（不用切右侧页签）', () => {
    const { container } = renderSidebar();

    fireEvent.click(screen.getByText('列表'));

    expect(screen.getByText(/全库 3 个因子，按综合分降序/)).toBeTruthy();
    const rowTexts = Array.from(container.querySelectorAll('input[type="checkbox"]')).map(
      (cb) => (cb.closest('div.group') as HTMLElement).textContent || '',
    );
    expect(rowTexts).toHaveLength(3);
    expect(rowTexts[0]).toContain('因子B1'); // composite 0.9
    expect(rowTexts[1]).toContain('因子A1'); // 0.5
    expect(rowTexts[2]).toContain('因子A2'); // 0.1
  });

  test('目录在途显加载态；确无匹配才说「无匹配因子」', () => {
    const { unmount } = renderSidebar({ factors: [], rows: [], loading: true });
    expect(screen.getByText('正在加载因子目录…')).toBeTruthy();
    expect(screen.queryByText('无匹配因子')).toBeNull();
    unmount();

    renderSidebar({ factors: [], rows: [], loading: false });
    expect(screen.getByText('无匹配因子')).toBeTruthy();
  });
});

/**
 * 标签筛选语义必须与排行榜一致（同一 matchTagFilter）：组内 OR、组间 AND。
 * 两边不一致 = 左侧目录还列着右侧榜单已经滤掉的因子，用户点进去会「查无此人」。
 */
describe('CatalogSidebar：标签筛选与排行榜同语义', () => {
  const TAGGED_FACTORS: FactorMeta[] = [
    meta({ code: 'tA', name_cn: '因子TA' }),
    meta({ code: 'tB', name_cn: '因子TB' }),
    meta({ code: 'tC', name_cn: '因子TC' }),
  ];
  const TAGGED_ROWS = [
    { ...lbRow('tA', 0.9), env_tag: '牛市进攻型', time_tag: '近期转强' },
    { ...lbRow('tB', 0.5), env_tag: '牛市进攻型', time_tag: '长期稳定型' },
    { ...lbRow('tC', 0.1), env_tag: '熊市防御型', time_tag: '近期转强' },
  ] as LeaderboardRow[];

  function renderTagged(tagFilter: string[]) {
    render(
      <CatalogSidebar
        factors={TAGGED_FACTORS}
        l1Order={['大类甲']}
        rowsByCode={new Map(TAGGED_ROWS.map((r) => [r.code, r]))}
        selected={[]}
        activeCode={null}
        tagFilter={tagFilter}
        categoryFilter={null}
        onSelectCategory={vi.fn()}
        onToggle={vi.fn()}
        onOpen={vi.fn()}
      />,
    );
  }

  test('组间 AND：环境 + 时效同时满足的因子才留下', () => {
    renderTagged(['牛市进攻型', '近期转强']);

    expect(screen.getByText('因子TA')).toBeTruthy();
    expect(screen.queryByText('因子TB')).toBeNull(); // 环境匹配、时效不匹配
    expect(screen.queryByText('因子TC')).toBeNull(); // 时效匹配、环境不匹配
  });

  test('组内 OR：同组两个标签取并集', () => {
    renderTagged(['牛市进攻型', '熊市防御型']);

    expect(screen.getByText('因子TA')).toBeTruthy();
    expect(screen.getByText('因子TB')).toBeTruthy();
    expect(screen.getByText('因子TC')).toBeTruthy();
  });
});
