/**
 * A 股详情体：简单模式收起机构级页签（筹码/融资/形态/股东/L2）；
 * 切到简单模式时被收起的当前页签自动回落（不留空白）。
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

vi.mock('../../../shared/useUiMode', () => ({
  useUiMode: vi.fn(),
}));
// 页签子体全部桩化：本测试只关心页签栏的升降维行为
vi.mock('../OverviewTab', () => ({ OverviewTab: () => <div>OVERVIEW_BODY</div> }));
vi.mock('../tabs/P2Tabs', () => ({
  FinancialsTab: () => <div>FIN_BODY</div>,
  ValuationTab: () => <div>VAL_BODY</div>,
  ChipFlowTab: () => <div>CHIP_BODY</div>,
  MarginTab: () => <div>MARGIN_BODY</div>,
  SentimentTab: () => <div>SENT_BODY</div>,
  HoldersTab: () => <div>HOLDERS_BODY</div>,
}));
vi.mock('../tabs/NewsTab', () => ({ NewsTab: () => <div>NEWS_BODY</div> }));
vi.mock('../L2FeatureCard', () => ({ L2FeatureCard: () => <div>L2_BODY</div> }));

import { CnDetailBody } from '../CnDetailBody';
import { useUiMode } from '../../../shared/useUiMode';

const mockedMode = vi.mocked(useUiMode);
const apiSimple = {
  mode: 'simple' as const,
  isSimple: true,
  isProfessional: false,
  setMode: vi.fn(),
  toggle: vi.fn(),
};
const apiPro = {
  mode: 'professional' as const,
  isSimple: false,
  isProfessional: true,
  setMode: vi.fn(),
  toggle: vi.fn(),
};

beforeEach(() => {
  mockedMode.mockReturnValue(apiSimple);
});

describe('CnDetailBody 页签降升维（T-FE-02 收尾）', () => {
  it('简单模式：机构级页签不渲染 + 提示可见；基础页签可用', async () => {
    const user = userEvent.setup();
    render(<CnDetailBody symbol="600036" profile={null} />);
    expect(screen.getByRole('button', { name: '概况' })).toBeTruthy();
    expect(screen.getByRole('button', { name: '财务' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: '筹码' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'L2' })).toBeNull();
    expect(screen.getByText(/简单模式已收起机构级页签/)).toBeTruthy();
    await user.click(screen.getByRole('button', { name: '财务' }));
    expect(screen.getByText('FIN_BODY')).toBeTruthy();
  });

  it('专业模式：九个页签全量可见', () => {
    mockedMode.mockReturnValue(apiPro);
    render(<CnDetailBody symbol="600036" profile={null} />);
    for (const label of ['概况', '财务', '估值', '筹码', '融资', '形态', '股东', '资讯', 'L2']) {
      expect(screen.getByRole('button', { name: label })).toBeTruthy();
    }
    expect(screen.queryByText(/简单模式已收起/)).toBeNull();
  });

  it('简单模式下当前页签被收起时回落到第一个可见页签', async () => {
    mockedMode.mockReturnValue(apiPro);
    const user = userEvent.setup();
    const { rerender } = render(<CnDetailBody symbol="600036" profile={null} />);
    await user.click(screen.getByRole('button', { name: 'L2' }));
    expect(screen.getByText('L2_BODY')).toBeTruthy();
    // 切回简单模式重渲染 → 收起的 L2 回落
    mockedMode.mockReturnValue(apiSimple);
    rerender(<CnDetailBody symbol="600036" profile={null} />);
    expect(screen.queryByText('L2_BODY')).toBeNull();
    expect(screen.getByText('OVERVIEW_BODY')).toBeTruthy();
  });
});
