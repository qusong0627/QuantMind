/**
 * FactorTable —— 机构级密集表的核心契约（用户四项反馈的落点）。
 *
 * 这里钉死的边：
 * 1. **挖到多少显示多少**：25 行全渲染（旧实现前端 slice(0,10) + 后端 limit=20
 *    是双层截断；表格自身 200 行内不截断）；
 * 2. **指标显示正确**：缺失 → `—`、真 0 → `0.0000`（两者在同一行并存的场景）；
 * 有方向的量按 A 股红涨绿跌着色（0 不着色）；
 * 3. **排序**：点表头 desc → asc → 默认；undefined 恒排最后（缺失不是「小」）；
 * 4. **选中与只读**：勾选回调带 factorId；ownerless 行（历史因子）勾选框与
 *    两个操作按钮全部禁用；点行名打开详情；
 * 5. **行内状态**：回测失败 chip 文案「失败」+ title 带后端原文；
 *    完成态换「看图表」；物化 chip title 带训练库列名；
 * 6. **服务端上限诚实提示**：清单触到 limit 时显示「已达单次上限 N」。
 */
import React from 'react';
import { describe, test, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { FactorTable } from '../FactorTable';
import type { FactorTableProps } from '../FactorTable';
import type { Factor } from '../../types-v2';
import type { BacktestRunEntry } from '../../context-v2/RunQueueContext';

function mkFactor(over: Partial<Factor> & { factorId: string }): Factor {
  return {
    factorName: over.factorId,
    factorExpression: 'close / mean(close, 5)',
    factorDescription: '',
    quality: 'medium',
    round: 0,
    direction: '',
    createdAt: '2026-10-01T00:00:00Z',
    ...over,
  };
}

const noop = vi.fn();

function renderTable(factors: Factor[], over: Partial<FactorTableProps> = {}) {
  const props: FactorTableProps = {
    factors,
    selectedIds: new Set<string>(),
    onToggleSelect: noop,
    onToggleSelectAll: noop,
    onOpenDetail: noop,
    onBacktest: noop,
    onMaterialize: noop,
    onViewBacktest: noop,
    ...over,
  };
  return render(<FactorTable {...props} />);
}

/** 行名序列（按 DOM 顺序）——排序断言用 */
function rowNames(container: HTMLElement): string[] {
  return Array.from(container.querySelectorAll('tbody tr')).map(
    (tr) => tr.querySelector('button[title*="点击查看详情"] span')?.textContent ?? '',
  );
}

function rowOf(container: HTMLElement, name: string): HTMLTableRowElement {
  const tr = Array.from(container.querySelectorAll('tbody tr')).find((row) =>
    row.querySelector('button[title*="点击查看详情"] span')?.textContent?.includes(name),
  );
  expect(tr, `row ${name} not found`).toBeTruthy();
  return tr as HTMLTableRowElement;
}

beforeEach(() => {
  noop.mockReset();
  localStorage.clear();
});

describe('FactorTable：全量渲染不截断', () => {
  test('25 行全部渲染，不出现「显示全部」截断按钮', () => {
    const factors = Array.from({ length: 25 }, (_, i) =>
      mkFactor({ factorId: `f${i}`, factorName: `因子${i}` }),
    );
    const { container } = renderTable(factors);

    expect(container.querySelectorAll('tbody tr')).toHaveLength(25);
    expect(screen.getByText(/共 25 行/)).toBeTruthy();
    expect(screen.queryByText(/显示全部/)).toBeNull();
    // 第 25 行也在
    expect(screen.getByText('因子24')).toBeTruthy();
  });

  test('清单触到服务端单次上限时，诚实地说明只有最近 N 条', () => {
    const factors = Array.from({ length: 20 }, (_, i) =>
      mkFactor({ factorId: `f${i}`, factorName: `因子${i}` }),
    );
    renderTable(factors, { serverLimit: 20 });

    expect(screen.getByText(/已达单次上限 20/)).toBeTruthy();
  });
});

describe('FactorTable：缺失「—」与真 0 并存、红涨绿跌', () => {
  test('rankIc=0 显示 0.0000 且不着色；缺失显示「—」', () => {
    const zero = mkFactor({ factorId: 'zero', factorName: 'ZeroIC', rankIc: 0 });
    const missing = mkFactor({ factorId: 'miss', factorName: 'MissingIC' });
    const { container } = renderTable([zero, missing]);

    // RankIC 列 = 第 5 个 td（勾选/序号/名/市场/RankIC）
    const zeroCell = rowOf(container, 'ZeroIC').querySelectorAll('td')[4];
    expect(zeroCell.textContent).toBe('0.0000');
    expect(zeroCell.className).not.toContain('text-rose-500');
    expect(zeroCell.className).not.toContain('text-emerald-500');

    const missingCell = rowOf(container, 'MissingIC').querySelectorAll('td')[4];
    expect(missingCell.textContent).toBe('—');
  });

  test('正 IC 红（text-rose-500）、负 IC 绿（text-emerald-500）', () => {
    const up = mkFactor({ factorId: 'up', factorName: 'UpIC', rankIc: 0.0612 });
    const down = mkFactor({ factorId: 'down', factorName: 'DownIC', rankIc: -0.0412 });
    const { container } = renderTable([up, down]);

    expect(rowOf(container, 'UpIC').querySelectorAll('td')[4].className).toContain(
      'text-rose-500',
    );
    expect(rowOf(container, 'DownIC').querySelectorAll('td')[4].className).toContain(
      'text-emerald-500',
    );
  });
});

describe('FactorTable：排序 desc → asc → 默认，undefined 恒排最后', () => {
  test('点 RankIC 表头三轮回到默认顺序，缺失值两轮都垫底', () => {
    const a = mkFactor({ factorId: 'a', factorName: 'A', rankIc: 0.03 });
    const b = mkFactor({ factorId: 'b', factorName: 'B' }); // 缺失
    const c = mkFactor({ factorId: 'c', factorName: 'C', rankIc: 0.07 });
    const { container } = renderTable([a, b, c]);

    expect(rowNames(container)).toEqual(['A', 'B', 'C']); // 默认 = 给什么顺序是什么

    const header = () => screen.getByTitle('按 RankIC 排序');
    fireEvent.click(header()); // desc
    expect(rowNames(container)).toEqual(['C', 'A', 'B']);
    fireEvent.click(header()); // asc
    expect(rowNames(container)).toEqual(['A', 'C', 'B']);
    fireEvent.click(header()); // 默认
    expect(rowNames(container)).toEqual(['A', 'B', 'C']);
  });
});

describe('FactorTable：选中与只读', () => {
  test('勾选行回调 factorId；全选框回调 onToggleSelectAll', () => {
    const onToggleSelect = vi.fn();
    const onToggleSelectAll = vi.fn();
    const { container } = renderTable(
      [mkFactor({ factorId: 'fa', factorName: 'FA' })],
      { onToggleSelect, onToggleSelectAll },
    );

    fireEvent.click(container.querySelector('tbody input[type="checkbox"]')!);
    expect(onToggleSelect).toHaveBeenCalledWith('fa');

    fireEvent.click(container.querySelector('thead input[type="checkbox"]')!);
    expect(onToggleSelectAll).toHaveBeenCalledWith(true);
  });

  test('ownerless 行（历史因子）勾选框与回测/物化按钮全部禁用，并标「只读」', () => {
    const f = mkFactor({ factorId: 'old', factorName: 'Legacy', ownerless: true });
    const { container } = renderTable([f]);

    const row = rowOf(container, 'Legacy');
    expect((row.querySelector('input[type="checkbox"]') as HTMLInputElement).disabled).toBe(true);
    expect(screen.getByText('只读')).toBeTruthy();
    const btns = Array.from(row.querySelectorAll('button')).map((b) => b.textContent);
    expect(btns).toContain('回测');
    expect(btns).not.toContain('物化'); // ownerless 连物化按钮都不渲染
    fireEvent.click(screen.getByText('回测'));
    expect(noop).not.toHaveBeenCalled();
    // 全选框也因没有可勾选行而禁用
    expect((container.querySelector('thead input[type="checkbox"]') as HTMLInputElement).disabled).toBe(true);
  });

  test('点因子名打开详情', () => {
    const onOpenDetail = vi.fn();
    renderTable([mkFactor({ factorId: 'fd', factorName: 'Detail' })], { onOpenDetail });

    fireEvent.click(screen.getByText('Detail'));
    expect(onOpenDetail).toHaveBeenCalledWith('fd');
  });
});

describe('FactorTable：行内回测状态 chip', () => {
  test('失败 → 「失败」chip，title 是后端原文（失败终态仍可重试）', () => {
    const entry: BacktestRunEntry = {
      status: 'failed',
      error: '因子不存在或无权访问（归属校验未通过）',
    };
    const { container } = renderTable(
      [mkFactor({ factorId: 'bf', factorName: 'BTFail' })],
      { backtestEntries: { bf: entry } },
    );

    const chip = screen.getByText('失败');
    expect(chip.getAttribute('title')).toBe('因子不存在或无权访问（归属校验未通过）');
    // 失败终态下仍可再次发起回测
    const row = rowOf(container, 'BTFail');
    expect(Array.from(row.querySelectorAll('button')).map((b) => b.textContent)).toContain('回测');
  });

  test('回测中 → 「回测中」chip 且回测按钮禁用（防重复入队）', () => {
    const { container } = renderTable(
      [mkFactor({ factorId: 'br', factorName: 'BTRun' })],
      { backtestEntries: { br: { status: 'running' } } },
    );

    expect(screen.getByText('回测中')).toBeTruthy();
    const row = rowOf(container, 'BTRun');
    const btn = Array.from(row.querySelectorAll('button')).find((b) => b.textContent === '回测');
    expect((btn as HTMLButtonElement).disabled).toBe(true);
  });

  test('完成 → 「已完成」chip，操作列换「看图表」并回调 onViewBacktest', () => {
    const onViewBacktest = vi.fn();
    renderTable(
      [mkFactor({ factorId: 'bd', factorName: 'BTDone' })],
      { backtestEntries: { bd: { status: 'completed' } }, onViewBacktest },
    );

    expect(screen.getByText('已完成')).toBeTruthy();
    expect(screen.queryByText('回测')).toBeNull();
    fireEvent.click(screen.getByText('看图表'));
    expect(onViewBacktest).toHaveBeenCalledWith('bd');
  });
});

describe('FactorTable：物化 chip 与扩展列', () => {
  test('已物化 → chip title 带训练库列名', () => {
    const f = mkFactor({
      factorId: 'mf',
      factorName: 'MatDone',
      materialization: { status: 'materialized', column: 'rdm_mom_5d', at: '2026-10-08' },
    });
    renderTable([f]);

    const chip = screen.getByText('已物化');
    expect(chip.getAttribute('title')).toContain('rdm_mom_5d');
  });

  test('重复被拒 → chip title 写清需 force 重跑（防用户狂点）', () => {
    const f = mkFactor({
      factorId: 'md',
      factorName: 'MatDup',
      materialization: { status: 'rejected_duplicate', corr: 0.9987, corrAgainst: 'rdm_x' },
    });
    renderTable([f]);

    const chip = screen.getByText('重复被拒');
    expect(chip.getAttribute('title')).toContain('|ρ|=0.9987');
    expect(chip.getAttribute('title')).toContain('force');
    expect(chip.getAttribute('title')).toContain('rdm_x');
  });

  test('扩展列开关：localStorage 记住选择，默认只显示核心列', () => {
    renderTable([mkFactor({ factorId: 'x', factorName: 'X' })]);
    expect(screen.queryByTitle('按 PFS 排序')).toBeNull();

    localStorage.setItem('qa_factor_cols_ext', '1');
    const second = renderTable([mkFactor({ factorId: 'y', factorName: 'Y' })]);
    expect(screen.getAllByTitle('按 PFS 排序').length).toBeGreaterThan(0);

    fireEvent.click(second.container.querySelector('button[title="净ARR / 年化换手 / PFS / RRE / 公式"]') as HTMLElement);
    expect(localStorage.getItem('qa_factor_cols_ext')).toBe('0');
  });
});
