import { describe, expect, test } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ParameterHelpPopover } from '../ParameterHelpPopover';

describe('ParameterHelpPopover 参数用法', () => {
  test('点击后居中弹窗展示通用参数与按模型建议', async () => {
    const user = userEvent.setup();
    render(<ParameterHelpPopover />);
    const trigger = screen.getByRole('button', { name: /参数用法/ });
    expect(trigger).toBeInTheDocument();
    await user.click(trigger);
    await waitFor(() => {
      expect(screen.getByText('参数配置：用法与按模型建议')).toBeInTheDocument();
    });
    expect(screen.getByText('先看这三条，再动参数')).toBeInTheDocument();
    expect(screen.getByText('LightGBM')).toBeInTheDocument();
    expect(screen.getByText('Ridge / Linear')).toBeInTheDocument();
  });
});
