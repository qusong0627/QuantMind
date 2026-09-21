/**
 * 注册页强制合规确认项（开源发行合规）：
 * 未勾选「我已阅读并理解…不构成投资建议、不代客理财」不得提交注册。
 *
 * 这是登录/注册主链路上的硬门槛，必须有用例锁住 —— 勾选框一旦被误删或
 * 按钮 disabled 被摘掉，注册流程就重新变成「无告知即注册」。
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import RegisterPage from '../RegisterPage';
import { listComplianceEvents } from '../../../../components/shared/compliance/complianceLog';
import { COMPLIANCE_CONSENT_TEXT } from '../../../../components/shared/compliance/ComplianceChrome';

vi.mock('../../hooks/useAuth', () => ({
  useAuth: () => ({ register: vi.fn(), isAuthenticated: false, isLoading: false }),
  useRegisterForm: () => ({
    email: '',
    password: '',
    confirmPassword: '',
    full_name: '',
    errors: {},
    updateField: vi.fn(),
    setErrors: vi.fn(),
    clearErrors: vi.fn(),
    setEmail: vi.fn(),
    setPassword: vi.fn(),
    setConfirmPassword: vi.fn(),
    setFullName: vi.fn(),
  }),
}));

vi.mock('../../../../store', () => ({
  useAppDispatch: () => vi.fn(),
  useAppSelector: () => undefined,
}));

beforeEach(() => {
  window.localStorage.clear();
});

const renderPage = () =>
  render(
    <MemoryRouter>
      <RegisterPage />
    </MemoryRouter>
  );

const submitButton = () =>
  screen.getByRole('button', { name: /注册账号/ }) as HTMLButtonElement;

describe('注册页合规确认项', () => {
  it('未勾选提交按钮禁用；勾选后放行并留痕', async () => {
    const user = userEvent.setup();
    renderPage();

    // 页面有 100ms 的初始化态，等提交按钮真正出现
    await waitFor(() => expect(submitButton()).toBeTruthy());

    // 确认项文案内嵌在页面上（用导出的常量做精确匹配 = 页面上确有一处与常量同源）
    expect(screen.getByText(COMPLIANCE_CONSENT_TEXT)).toBeTruthy();
    // 资质边界句同时出现在「必须勾选的确认项」和「卡内免责横条」两处
    expect(screen.getAllByText(/不构成投资建议、不代客理财/).length).toBeGreaterThanOrEqual(2);

    // 未勾选 ⇒ 不能提交
    expect(submitButton().disabled).toBe(true);
    expect(listComplianceEvents().some((e) => e.kind === 'terms_consent')).toBe(false);

    // 勾选 ⇒ 放行 + 留痕
    await user.click(screen.getByRole('checkbox'));
    await waitFor(() => expect(submitButton().disabled).toBe(false));
    expect(listComplianceEvents().some((e) => e.kind === 'terms_consent')).toBe(true);
  });
});
