/**
 * MinerU 解析设置区 —— 因子挖掘内的唯一配置入口。
 *
 * 钉死的边：
 * - 密钥只回掩码：留空 = 保留已存密钥（回存掩码会把真密钥写坏）；
 * - 保存失败显示后端 400 原文（校验原因要能看见，用户不用对着按钮猜）；
 * - 部署固定本地模式：云端配置不生效的硬顶提示必须可见；
 * - 读故障（readable=false）≠ 没配：锁保存 + 明示读故障，绝不显示成
 *   「尚未配置」诱导用户覆盖已存密钥。
 */
import React from 'react';
import { describe, test, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { MineruSettingsSection } from '../MineruSettingsSection';
import type { MineruSettingsView } from '../../services-v2/docMiningApi';

const { getMineruSettingsMock, saveMineruSettingsMock, clearMineruSettingsMock } =
  vi.hoisted(() => ({
    getMineruSettingsMock: vi.fn(),
    saveMineruSettingsMock: vi.fn(),
    clearMineruSettingsMock: vi.fn(),
  }));

vi.mock('../../services-v2/docMiningApi', async (importOriginal) => {
  const actual =
    await importOriginal<typeof import('../../services-v2/docMiningApi')>();
  return {
    ...actual,
    getMineruSettings: getMineruSettingsMock,
    saveMineruSettings: saveMineruSettingsMock,
    clearMineruSettings: clearMineruSettingsMock,
  };
});

const VIEW_CLOUD_USER: MineruSettingsView = {
  settings: {
    mode: 'cloud',
    configured: true,
    api_token_set: true,
    api_token_masked: 'use****3456',
    local_url: 'http://192.168.1.10:8000',
    local_api_key_set: false,
    local_api_key_masked: '',
    local_tier: null,
  },
  readable: true,
  source: 'user',
  effective_mode: 'cloud',
  env_configured: false,
  env_mode: null,
};

const VIEW_NONE: MineruSettingsView = {
  settings: null,
  readable: true,
  source: 'none',
  effective_mode: null,
  env_configured: false,
  env_mode: null,
};

const VIEW_ENV_LOCAL: MineruSettingsView = {
  settings: null,
  readable: true,
  source: 'env',
  effective_mode: 'local',
  env_configured: true,
  env_mode: 'local',
};

const VIEW_UNREADABLE: MineruSettingsView = {
  settings: null,
  readable: false,
  source: 'none',
  effective_mode: null,
  env_configured: false,
  env_mode: null,
};

beforeEach(() => {
  getMineruSettingsMock.mockReset();
  saveMineruSettingsMock.mockReset();
  clearMineruSettingsMock.mockReset();
  getMineruSettingsMock.mockResolvedValue(VIEW_CLOUD_USER);
  saveMineruSettingsMock.mockResolvedValue(VIEW_CLOUD_USER);
  clearMineruSettingsMock.mockResolvedValue(VIEW_NONE);
});

describe('密钥只回掩码', () => {
  test('已存 Token 只在占位符里给掩码；留空保存原样下发（后端=保留原值）', async () => {
    const onChanged = vi.fn();
    render(<MineruSettingsSection onChanged={onChanged} />);
    await screen.findByText('解析设置（MinerU）');

    const tokenInput = screen.getByPlaceholderText(
      '已保存 use****3456（留空保持不变）',
    ) as HTMLInputElement;
    expect(tokenInput.value).toBe('');

    fireEvent.click(screen.getByRole('button', { name: '保存设置' }));

    await screen.findByText('已保存：后续上传的文档按新通道解析。');
    expect(saveMineruSettingsMock).toHaveBeenCalledWith({
      mode: 'cloud',
      api_token: '',
      local_url: 'http://192.168.1.10:8000',
      local_api_key: '',
      local_tier: '',
    });
    expect(onChanged).toHaveBeenCalledTimes(1);
  });
});

describe('通道切换与保存', () => {
  test('切到本地：local_url / local_tier / local_api_key 如实下发', async () => {
    getMineruSettingsMock.mockResolvedValue(VIEW_NONE);
    render(<MineruSettingsSection />);
    await screen.findByText('解析设置（MinerU）');

    fireEvent.click(screen.getByRole('radio', { name: /本地/ }));
    fireEvent.change(screen.getByLabelText('服务地址'), {
      target: { value: 'http://10.0.0.5:9000' },
    });
    fireEvent.change(screen.getByLabelText('解析档位'), {
      target: { value: 'advanced' },
    });
    fireEvent.change(screen.getByLabelText('API Key（可选）'), {
      target: { value: 'sk-local' },
    });

    fireEvent.click(screen.getByRole('button', { name: '保存设置' }));

    await screen.findByText('已保存：后续上传的文档按新通道解析。');
    expect(saveMineruSettingsMock).toHaveBeenCalledWith({
      mode: 'local',
      api_token: '',
      local_url: 'http://10.0.0.5:9000',
      local_api_key: 'sk-local',
      local_tier: 'advanced',
    });
  });

  test('保存被拒：显示后端 400 原文（不是「请求失败」）', async () => {
    getMineruSettingsMock.mockResolvedValue(VIEW_NONE);
    saveMineruSettingsMock.mockRejectedValue({
      response: {
        data: {
          detail:
            '云端模式需要填写 MinerU API Token（或在服务器 .env 配置 MINERU_API_TOKEN）',
        },
      },
    });
    render(<MineruSettingsSection />);
    await screen.findByText('解析设置（MinerU）');

    fireEvent.click(screen.getByRole('button', { name: '保存设置' }));

    expect(
      await screen.findByText(/云端模式需要填写 MinerU API Token/),
    ).toBeTruthy();
  });
});

describe('生效来源与部署硬顶', () => {
  test('部署固定本地模式：云端不生效的提示与生效来源都可见', async () => {
    getMineruSettingsMock.mockResolvedValue(VIEW_ENV_LOCAL);
    render(<MineruSettingsSection />);

    expect(
      await screen.findByText(/本部署已固定为本地解析模式/),
    ).toBeTruthy();
    expect(screen.getByText(/当前生效：部署默认/)).toBeTruthy();
  });

  test('未配置任何通道：给出「先保存设置」的引导', async () => {
    getMineruSettingsMock.mockResolvedValue(VIEW_NONE);
    render(<MineruSettingsSection />);
    expect(await screen.findByText(/尚未配置解析通道/)).toBeTruthy();
  });
});

describe('读故障 ≠ 没配', () => {
  test('readable=false：锁保存、显读故障、不出「尚未配置」引导', async () => {
    getMineruSettingsMock.mockResolvedValue(VIEW_UNREADABLE);
    render(<MineruSettingsSection />);
    await screen.findByText('设置存储暂不可用（读取失败）：为避免覆盖已存密钥，修改与保存已锁定，请稍后重试。');

    expect(
      (screen.getByRole('button', { name: '保存设置' }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
    expect(
      (
        screen.getByRole('button', {
          name: '清除全部设置',
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(true);
    expect(screen.getByText('设置存储读取失败')).toBeTruthy();
    expect(screen.queryByText(/尚未配置解析通道/)).toBeNull();
  });
});

describe('清除全部设置', () => {
  test('二次确认才发请求，成功后提示回落', async () => {
    render(<MineruSettingsSection />);
    await screen.findByText('解析设置（MinerU）');

    fireEvent.click(screen.getByRole('button', { name: '清除全部设置' }));
    expect(clearMineruSettingsMock).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: '确认清除' }));
    await screen.findByText('已清除本页设置，回落到部署默认通道。');
    expect(clearMineruSettingsMock).toHaveBeenCalledTimes(1);
  });
});
