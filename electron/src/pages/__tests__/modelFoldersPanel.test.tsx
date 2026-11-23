import type * as React from 'react';
import { describe, expect, test, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ModelFolderModal, ModelFoldersPanel } from '../modelFoldersPanel';

function renderPanel(overrides: Partial<React.ComponentProps<typeof ModelFoldersPanel>> = {}) {
  const callbacks = {
    onToggleOpen: vi.fn(),
    onToggleOrganize: vi.fn(),
    onSelectFolder: vi.fn(),
    onCreateFolder: vi.fn(),
    onRenameFolder: vi.fn(),
    onDeleteFolder: vi.fn(),
  };
  render(
    <ModelFoldersPanel
      folders={[{ id: 'f1', name: 'T+5' }]}
      counts={{ total: 3, unfiled: 1, byFolder: { f1: 2 } }}
      activeFolder={null}
      open
      organizeMode={false}
      {...callbacks}
      {...overrides}
    />,
  );
  return callbacks;
}

describe('ModelFoldersPanel', () => {
  test('渲染全部/未分组/文件夹及计数，点击回传筛选值', async () => {
    const user = userEvent.setup();
    const { onSelectFolder } = renderPanel();

    expect(screen.getByText('全部模型')).toBeInTheDocument();
    expect(screen.getByText('未分组')).toBeInTheDocument();
    expect(screen.getByText('T+5')).toBeInTheDocument();
    expect(screen.getByText('2')).toBeInTheDocument();

    await user.click(screen.getByText('T+5'));
    expect(onSelectFolder).toHaveBeenCalledWith('f1');

    await user.click(screen.getByText('未分组'));
    expect(onSelectFolder).toHaveBeenCalledWith('__unfiled__');
  });

  test('新建文件夹与整理模式按钮回传动作', async () => {
    const user = userEvent.setup();
    const { onCreateFolder, onToggleOrganize } = renderPanel();

    await user.click(screen.getByLabelText('新建文件夹'));
    expect(onCreateFolder).toHaveBeenCalledTimes(1);

    await user.click(screen.getByLabelText('整理模式'));
    expect(onToggleOrganize).toHaveBeenCalledTimes(1);
  });

  test('折叠后不渲染文件夹列表', () => {
    renderPanel({ open: false });
    expect(screen.queryByText('全部模型')).not.toBeInTheDocument();
  });

  test('文件夹行只做过滤，不内联展开模型（避免与下方列表重复）', () => {
    renderPanel();
    expect(screen.getByRole('button', { name: /^T\+5/ })).not.toHaveAttribute(
      'aria-expanded',
    );
    expect(screen.queryByText('这个文件夹还是空的')).not.toBeInTheDocument();
  });
});

describe('ModelFolderModal', () => {
  test('新建模式回传输入与提交，并提示将移入的模型数', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const onSubmit = vi.fn();
    render(
      <ModelFolderModal
        mode="create"
        value=""
        moveCount={2}
        onChange={onChange}
        onCancel={() => {}}
        onSubmit={onSubmit}
      />,
    );

    expect(screen.getByText('新建文件夹')).toBeInTheDocument();
    expect(screen.getByText('创建后自动移入 2 个模型。')).toBeInTheDocument();
    await user.type(screen.getByPlaceholderText('如：T+5 日频 / 港股 / 实验组'), 'A');
    expect(onChange).toHaveBeenCalled();
  });

  test('重命名模式按钮文案为保存', () => {
    render(
      <ModelFolderModal
        mode="rename"
        value="旧名"
        moveCount={0}
        onChange={() => {}}
        onCancel={() => {}}
        onSubmit={() => {}}
      />,
    );
    expect(screen.getByText('重命名文件夹')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /保\s*存/ })).toBeInTheDocument();
  });
});
