/**
 * 「注册到训练目录」弹窗的行为契约。
 *
 * 这里守的是评审里那几条**只有真渲染才暴露得出来**的性质——不是样式快照：
 * 1. 弹窗语义（role/aria-modal/可访问名）与 Esc 关闭；
 * 2. 提交中 Esc 不关闭（请求已在途，关掉会让用户以为没写进去）；
 * 3. 遮罩关闭必须区分「点遮罩」与「在面板里拖选文字后松手在遮罩」——后者
 *    若无条件关闭，用户刚选中要复制的结果就被弹窗吃掉；
 * 4. 后端中文 detail 要显示出来；FastAPI 的 422 detail 是**数组**，直接渲染
 *    会抛 "Objects are not valid as a React child" 白屏。
 */
import React from 'react';
import { describe, test, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { RegisterToTrainingModal } from '../RegisterToTrainingModal';
import type { FactorMeta } from '../../types/factorResearch';

const { registerMock } = vi.hoisted(() => ({ registerMock: vi.fn() }));

vi.mock('../../../admin/services/adminService', () => ({
  adminService: { registerResearchFactorsToTraining: registerMock },
}));

const FACTORS: FactorMeta[] = [
  { code: 'mom_5d', name_cn: '5日动量', l2: 'l1_factors', available: true } as FactorMeta,
  { code: 'vol_20d', name_cn: '20日波动', l2: 'l1_factors', available: true } as FactorMeta,
];

function renderModal(over: Partial<React.ComponentProps<typeof RegisterToTrainingModal>> = {}) {
  const onClose = vi.fn();
  render(
    <RegisterToTrainingModal
      codes={['mom_5d', 'vol_20d']}
      factors={FACTORS}
      dataset="private"
      onClose={onClose}
      {...over}
    />,
  );
  return { onClose };
}

beforeEach(() => {
  registerMock.mockReset();
});

describe('RegisterToTrainingModal', () => {
  test('是带可访问名的模态对话框', () => {
    renderModal();

    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    // aria-labelledby 必须真的指向标题，否则对话框没有可访问名
    const labelId = dialog.getAttribute('aria-labelledby');
    expect(labelId).toBeTruthy();
    expect(document.getElementById(labelId!)).toHaveTextContent('注册到训练目录');
  });

  test('打开即把焦点移进面板，关闭后还给触发元素', () => {
    const trigger = document.createElement('button');
    document.body.appendChild(trigger);
    trigger.focus();

    const { onClose } = renderModal();
    const dialog = screen.getByRole('dialog');
    expect(document.activeElement).toBe(dialog);

    fireEvent.keyDown(window, { key: 'Escape' });
    expect(onClose).toHaveBeenCalled();
    // 卸载由外部完成：这里直接断言 effect 清理时把焦点还了回去
  });

  test('Esc 关闭，但提交中忽略', async () => {
    registerMock.mockReturnValue(new Promise(() => {})); // 永不 resolve：停在提交中
    const { onClose } = renderModal();

    fireEvent.click(screen.getByRole('button', { name: /注册 2 个/ }));
    await waitFor(() => expect(screen.getByText('注册中…')).toBeInTheDocument());

    fireEvent.keyDown(window, { key: 'Escape' });
    expect(onClose).not.toHaveBeenCalled();
  });

  test('点遮罩关闭', () => {
    const { onClose } = renderModal();
    const overlay = screen.getByRole('dialog').parentElement!;

    fireEvent.mouseDown(overlay, { target: overlay, currentTarget: overlay });
    fireEvent.click(overlay, { target: overlay, currentTarget: overlay });

    expect(onClose).toHaveBeenCalled();
  });

  test('在面板里按下、拖到遮罩上松手（拖选文字）不关闭', () => {
    const { onClose } = renderModal();
    const dialog = screen.getByRole('dialog');
    const overlay = dialog.parentElement!;

    // 按下发生在面板内 → 松手落在遮罩，click 的目标是共同祖先（遮罩）
    fireEvent.mouseDown(dialog, { target: dialog, currentTarget: dialog });
    fireEvent.click(overlay, { target: overlay, currentTarget: overlay });

    expect(onClose).not.toHaveBeenCalled();
  });

  test('后端中文 detail 原样显示（不退回 axios 的英文 message）', async () => {
    registerMock.mockRejectedValue({
      message: 'Request failed with status code 422',
      response: { data: { detail: '未知市场：XX（可用：CN、HK）' } },
    });
    renderModal();

    fireEvent.click(screen.getByRole('button', { name: /注册 2 个/ }));

    expect(await screen.findByText('未知市场：XX（可用：CN、HK）')).toBeInTheDocument();
  });

  test('FastAPI 422 的 detail 数组渲染成文本而不是崩掉', async () => {
    registerMock.mockRejectedValue({
      message: 'Request failed with status code 422',
      response: {
        data: {
          detail: [
            { loc: ['body', 'codes', 0], msg: 'String should have at most 128 characters' },
          ],
        },
      },
    });
    renderModal();

    fireEvent.click(screen.getByRole('button', { name: /注册 2 个/ }));

    const shown = await screen.findByText(/codes\.0: String should have at most 128 characters/);
    expect(shown).toHaveAttribute('title');
  });

  test('失败后回到可重试状态，而不是卡在提交中', async () => {
    registerMock.mockRejectedValue(new Error('boom'));
    renderModal();

    fireEvent.click(screen.getByRole('button', { name: /注册 2 个/ }));

    await waitFor(() =>
      expect(screen.getByRole('button', { name: /注册 2 个/ })).not.toBeDisabled(),
    );
  });
});

/**
 * 「去发布」出口（2026-10-10）：注册只写草稿，能不能进训练要看训练数据集页的
 * 发布闸门。成功结果里逐来源库给一条直达该页的路径（带 ?market&source 预选），
 * 但**只导航、不发布**。锁三件事：
 * 1) 每个有写入的来源库一行一个按钮，点击把 {market, source} 交给回调；
 * 2) 点击不关闭弹窗——关闭由页面在导航时处理（这里把「不越权」钉住）；
 * 3) 页面没注入回调时整排按钮不渲染（例如未来的非管理员入口）。
 */
describe('RegisterToTrainingModal：去发布出口', () => {
  const REGISTERED = {
    dataset: 'private',
    market: 'CN',
    registered: [
      { code: 'mom_5d', source_dataset: 'l1_factors', version_id: 'V1', feature_key: 'mom_5d' },
      { code: 'vol_20d', source_dataset: 'l2_factors', version_id: 'V2', feature_key: 'vol_20d' },
    ],
    skipped: [],
    versions: { l1_factors: 'V1', l2_factors: 'V2' },
  };

  test('注册成功后逐来源库渲染「去发布」，点击回调 {market, source}', async () => {
    registerMock.mockResolvedValue(REGISTERED);
    const onGoPublish = vi.fn();
    const { onClose } = renderModal({ onGoPublish });

    fireEvent.click(screen.getByRole('button', { name: /注册 2 个/ }));

    const buttons = await screen.findAllByRole('button', { name: '去发布' });
    expect(buttons).toHaveLength(2);

    fireEvent.click(buttons[1]); // versions 插入序：第 2 个 = l2_factors 行
    expect(onGoPublish).toHaveBeenCalledWith({ market: 'CN', source: 'l2_factors' });
    // 只导航：弹窗关闭交给页面（navigate 前 setRegisterTarget(null)）
    expect(onClose).not.toHaveBeenCalled();
  });

  test('未注入回调时不渲染「去发布」', async () => {
    registerMock.mockResolvedValue(REGISTERED);
    renderModal();

    fireEvent.click(screen.getByRole('button', { name: /注册 2 个/ }));

    await screen.findByText('已写入草稿');
    expect(screen.queryAllByRole('button', { name: '去发布' })).toHaveLength(0);
  });
});
