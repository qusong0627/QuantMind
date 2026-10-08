/**
 * Excel 式表头筛选下拉：勾选值集合 + 搜索 + 全选/清空。
 *
 * 面板（`ExcelFilterPanel`）与触发器（`ExcelFilterDropdown`）分开测：
 * 面板管语义（和 columnFilters 的纯逻辑对齐，`undefined`=全选、`[]`=清空），
 * 触发器管「激活态可见」（漏斗染蓝 + data-filter-active，用户据此知道这列被筛过）。
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import ExcelFilterDropdown, { ExcelFilterPanel } from '../ExcelFilterDropdown';

const OPTIONS = ['买入', '卖出'];

const checkedBoxes = (): HTMLInputElement[] =>
  (screen.getAllByRole('checkbox') as HTMLInputElement[]).filter((el) => el.checked);

describe('ExcelFilterPanel', () => {
  it('未筛选（undefined）时是 Excel 的全选态：全部勾选', () => {
    render(<ExcelFilterPanel options={OPTIONS} selected={undefined} onApply={() => {}} />);
    expect(checkedBoxes()).toHaveLength(OPTIONS.length);
    expect(screen.getByText('已选 2/2')).toBeInTheDocument();
  });

  it('从全选态取消一项 → 上抛「其余全部」的子集', () => {
    const onApply = vi.fn();
    render(<ExcelFilterPanel options={OPTIONS} selected={undefined} onApply={onApply} />);
    fireEvent.click(screen.getByRole('checkbox', { name: '买入' }));
    expect(onApply).toHaveBeenCalledWith(['卖出']);
  });

  it('子集态勾选一项 → 并集按 options 顺序上抛；恰好勾满时归一化回 undefined', () => {
    const onApply = vi.fn();
    const { rerender } = render(
      <ExcelFilterPanel options={['a', 'b', 'c']} selected={['a', 'c']} onApply={onApply} />,
    );
    fireEvent.click(screen.getByRole('checkbox', { name: 'b' }));
    expect(onApply).toHaveBeenLastCalledWith(undefined);

    rerender(<ExcelFilterPanel options={['a', 'b', 'c']} selected={['c']} onApply={onApply} />);
    fireEvent.click(screen.getByRole('checkbox', { name: 'a' }));
    expect(onApply).toHaveBeenLastCalledWith(['a', 'c']);
  });

  it('搜索只缩小可见行，不改勾选集合', () => {
    render(<ExcelFilterPanel options={['买入开仓', '卖出平仓']} selected={undefined} onApply={() => {}} />);
    fireEvent.change(screen.getByPlaceholderText('搜索'), { target: { value: '卖' } });
    expect(screen.getByText('卖出平仓')).toBeInTheDocument();
    expect(screen.queryByText('买入开仓')).not.toBeInTheDocument();
    // 勾选集合没被动过
    expect(screen.getByText('已选 2/2')).toBeInTheDocument();
  });

  it('清空 → 上抛空数组（一行都不展示）；全选 → 上抛 undefined', () => {
    const onApply = vi.fn();
    render(<ExcelFilterPanel options={OPTIONS} selected={['买入']} onApply={onApply} />);
    fireEvent.click(screen.getByRole('button', { name: '清空' }));
    expect(onApply).toHaveBeenLastCalledWith([]);
    fireEvent.click(screen.getByRole('button', { name: '全选' }));
    expect(onApply).toHaveBeenLastCalledWith(undefined);
  });

  it('没有可选值时给出占位文案', () => {
    render(<ExcelFilterPanel options={[]} selected={undefined} onApply={() => {}} />);
    expect(screen.getByText('无可筛选的值')).toBeInTheDocument();
  });
});

describe('ExcelFilterDropdown', () => {
  it('未筛选时不点亮；筛选后点亮并可从触发器展开面板', async () => {
    const { rerender } = render(
      <ExcelFilterDropdown title="方向" options={OPTIONS} selected={undefined} onApply={() => {}} />,
    );
    const trigger = screen.getByRole('button', { name: '方向筛选' });
    expect(trigger).toHaveAttribute('data-filter-active', 'false');

    rerender(
      <ExcelFilterDropdown title="方向" options={OPTIONS} selected={['买入']} onApply={() => {}} />,
    );
    expect(screen.getByRole('button', { name: '方向筛选' })).toHaveAttribute(
      'data-filter-active',
      'true',
    );

    fireEvent.click(trigger);
    expect(await screen.findByPlaceholderText('搜索')).toBeInTheDocument();
  });
});
