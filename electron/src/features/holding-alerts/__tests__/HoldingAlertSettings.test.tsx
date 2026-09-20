/**
 * 个人中心「持仓监控与提醒 ｜ 提醒通道」两列布局的契约测试。
 *
 * 这里锁的是**布局与分工**，不是像素：两列必须同层同级（左右对称）、各自纵向排布、
 * 末尾块贴底（底边齐平），且各列只放自己那半的设置——设置项串列比样式错乱更难被发现。
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { HoldingAlertSettings } from '../HoldingAlertSettings';
import { holdingAlertService, DEFAULT_ALERT_CONFIG } from '../../../services/holdingAlertService';
import type { HoldingAlertConfig } from '../../../services/holdingAlertService';

vi.mock('../../../services/holdingAlertService', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../../services/holdingAlertService')>();
  return {
    ...actual,
    holdingAlertService: {
      getConfig: vi.fn(),
      updateConfig: vi.fn(),
      getSentinelStatus: vi.fn(),
    },
  };
});

vi.mock('../../../services/alertDelivery', () => ({
  previewAlertSound: vi.fn(() => true),
}));

const svc = vi.mocked(holdingAlertService);

const mkConfig = (over: Partial<HoldingAlertConfig> = {}): HoldingAlertConfig => ({
  ...DEFAULT_ALERT_CONFIG,
  ...over,
});

/** 渲染并等到配置加载完（开关从 disabled 变可点，说明 load 已收口） */
const renderLoaded = async (config: HoldingAlertConfig = mkConfig()) => {
  svc.getConfig.mockResolvedValue(config);
  svc.getSentinelStatus.mockResolvedValue({
    running: true,
    lastScanEpoch: Math.floor(Date.now() / 1000) - 10,
    monitored: 42,
    mine: { monitored: 3 },
  });
  const view = render(<HoldingAlertSettings />);
  await screen.findByRole('heading', { name: '持仓监控与提醒' });
  await waitFor(() => expect(screen.getByRole('switch', { name: '持仓监控总开关' })).toBeEnabled());
  return view;
};

beforeEach(() => {
  vi.clearAllMocks();
  svc.updateConfig.mockImplementation(async (patch) => mkConfig(patch));
});

describe('两列对称布局', () => {
  it('两张卡是同一个两列栅格的直接子元素——同层同级才叫左右对称', async () => {
    // Arrange & Act
    const { container } = await renderLoaded();

    // Assert
    const grid = container.firstElementChild as HTMLElement;
    expect(grid.className).toContain('lg:grid-cols-2');
    const left = screen.getByTestId('holding-monitor-card');
    const right = screen.getByTestId('holding-alert-channels');
    expect(left.parentElement).toBe(grid);
    expect(right.parentElement).toBe(grid);
    expect(left).not.toBe(right);
  });

  it('两卡各自纵向排布且末块贴底——否则两列底边参差，看着就是不齐', async () => {
    // Arrange & Act
    await renderLoaded();

    // Assert
    for (const card of [screen.getByTestId('holding-monitor-card'), screen.getByTestId('holding-alert-channels')]) {
      expect(card.className).toContain('flex-col');
      const last = card.lastElementChild as HTMLElement;
      expect(last.className).toContain('mt-auto');
    }
  });

  it('两卡各有一条状态带（结论先行），互不重复', async () => {
    // Arrange & Act
    await renderLoaded();

    // Assert：左=哨兵是否在跑，右=通道实际会响几条
    const left = screen.getByTestId('holding-monitor-card');
    const right = screen.getByTestId('holding-alert-channels');
    expect(left.children[1].textContent).toContain('哨兵运行中');
    expect(right.children[1].textContent).toContain('3/3');
  });
});

describe('设置项分工：哪半在哪列', () => {
  it('左列只有监控类设置（总开关/范围/阈值），不带通道开关', async () => {
    // Arrange & Act
    await renderLoaded();

    // Assert
    const left = screen.getByTestId('holding-monitor-card');
    expect(left.textContent).toContain('持仓监控总开关');
    expect(left.textContent).toContain('监控范围');
    expect(left.textContent).toContain('分数阈值');
    expect(left.textContent).not.toContain('最低提醒级别');
    expect(left.textContent).not.toContain('试听提示音');
  });

  it('右列只有通道类设置（三通道/级别/试听），不带监控范围', async () => {
    // Arrange & Act
    await renderLoaded();

    // Assert
    const right = screen.getByTestId('holding-alert-channels');
    expect(right.textContent).toContain('站内面板');
    expect(right.textContent).toContain('桌面系统通知');
    expect(right.textContent).toContain('声音提示');
    expect(right.textContent).toContain('最低提醒级别');
    expect(right.textContent).toContain('试听提示音');
    expect(right.textContent).not.toContain('监控范围');
    expect(right.textContent).not.toContain('分数阈值');
  });

  it('加载失败时错误条横跨两列，且不冒充任何一列的设置', async () => {
    // Arrange
    svc.getConfig.mockRejectedValue(new Error('接口挂了'));
    svc.getSentinelStatus.mockRejectedValue(new Error('接口挂了'));

    // Act
    const { container } = render(<HoldingAlertSettings />);

    // Assert
    const banner = await screen.findByText('接口挂了');
    expect(banner.className).toContain('lg:col-span-2');
    expect(banner.parentElement).toBe(container.firstElementChild);
  });
});

describe('开关行为', () => {
  it('切总开关只提交 enabled 这一个字段（部分更新，不能顺带覆盖别的设置）', async () => {
    // Arrange
    await renderLoaded();

    // Act
    fireEvent.click(screen.getByRole('switch', { name: '持仓监控总开关' }));

    // Assert
    await waitFor(() => expect(svc.updateConfig).toHaveBeenCalledWith({ enabled: false }));
  });

  it('保存失败要回滚开关状态——乐观更新后不回滚，用户会以为自己关掉了', async () => {
    // Arrange
    await renderLoaded();
    svc.updateConfig.mockRejectedValueOnce(new Error('写入失败'));
    const toggle = screen.getByRole('switch', { name: '声音提示' });
    expect(toggle).toBeChecked();

    // Act
    fireEvent.click(toggle);

    // Assert
    await waitFor(() => expect(svc.updateConfig).toHaveBeenCalledWith({ notify_sound: false }));
    await waitFor(() => expect(screen.getByRole('switch', { name: '声音提示' })).toBeChecked());
  });

  it('总开关关闭时范围与通道开关一并置灰：关着总闸还能逐条改是骗人的', async () => {
    // Arrange & Act
    await renderLoaded(mkConfig({ enabled: false }));

    // Assert
    expect(screen.getByRole('switch', { name: '持仓监控总开关' })).not.toBeDisabled();
    for (const name of ['模拟盘持仓', '实盘持仓', '手工自选', '站内面板', '桌面系统通知', '声音提示']) {
      expect(screen.getByRole('switch', { name })).toBeDisabled();
    }
  });
});
