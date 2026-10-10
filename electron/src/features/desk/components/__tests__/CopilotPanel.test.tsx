/**
 * 副驾驶面板「截至」= 数据时刻 + 陈旧横幅（审计 H15）。
 *
 * 旧实现 as_of 是响应时刻：哨兵/总线停摆数小时，面板照样写「截至 <现在>」，
 * 前台无从察觉。本测试钉住：陈旧 as_of → 横幅出现且带真实年龄；新鲜 → 不出横幅；
 * 事件行渲染事件自带 ts；无事件（as_of=null）不冒充新鲜。
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import type { Mock } from 'vitest';

vi.mock('../../services/copilotService', () => ({
  getCopilotPanel: vi.fn(),
  listAdvice: vi.fn(() => Promise.resolve([])),
  getAdviceStats: vi.fn(() => Promise.resolve(null)),
  annotateAlert: vi.fn(),
  executeAdvice: vi.fn(),
  rejectAdvice: vi.fn(),
}));

import { getCopilotPanel } from '../../services/copilotService';
import { CopilotPanel } from '../CopilotPanel';

const mockPanel = getCopilotPanel as unknown as Mock;

const eventAt = (ts: string) => ({
  alert_id: 'a1',
  ts,
  alert_type: 'news:risk_event',
  severity: 'warn',
  market: 'CN',
  symbol: '600036.SH',
  title: '某银行被立案调查',
});

beforeEach(() => {
  mockPanel.mockReset();
});

describe('副驾驶面板数据时刻（H15）', () => {
  it('数据陈旧（最新事件 30 分钟前）：挂横幅且带真实年龄', async () => {
    const old = new Date(Date.now() - 30 * 60 * 1000);
    mockPanel.mockResolvedValue({
      as_of: old.toISOString(),
      events: { available: true, items: [eventAt(old.toISOString())] },
    });

    render(<CopilotPanel />);

    const banner = await screen.findByTestId('copilot-stale-banner');
    expect(banner.textContent).toContain('情报数据陈旧');
    expect(banner.textContent).toContain('30 分钟前');
    expect(banner.textContent).toContain('按旧数据对待');
  });

  it('数据新鲜：不挂横幅', async () => {
    mockPanel.mockResolvedValue({
      as_of: new Date().toISOString(),
      events: { available: true, items: [] },
    });

    render(<CopilotPanel />);

    await screen.findByText(/截至/);
    expect(screen.queryByTestId('copilot-stale-banner')).toBeNull();
  });

  it('事件行渲染事件自带 ts（title=原始 ISO，文案为本地紧凑标签）', async () => {
    const ts = new Date(Date.now() - 60_000).toISOString();
    mockPanel.mockResolvedValue({ as_of: ts, events: { available: true, items: [eventAt(ts)] } });

    render(<CopilotPanel />);

    expect(await screen.findByTitle(ts)).toBeInTheDocument();
  });

  it('窗口内无事件（as_of=null）：头部如实显示「窗口内无事件」，不冒充新鲜', async () => {
    mockPanel.mockResolvedValue({ as_of: null, events: { available: true, items: [] } });

    render(<CopilotPanel />);

    expect(await screen.findByText('窗口内无事件')).toBeInTheDocument();
    expect(screen.queryByTestId('copilot-stale-banner')).toBeNull();
  });
});
