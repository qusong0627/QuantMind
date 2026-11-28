/**
 * 后台管理 · 系统设置二级菜单。
 *
 * 用户口径（2026-11-28）：系统设置下面走二级菜单，**第一个是金融情感**；
 * SDK 接入凭证 / API 密钥从模拟交易「设置中心」迁入，排第二。
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';

import { AdminSystemSettings } from '../AdminSystemSettings';

vi.mock('../../services/adminService', () => ({
    adminService: {
        getFinbertStatus: vi.fn(),
        setFinbertEnabled: vi.fn(),
    },
}));

import { adminService } from '../../services/adminService';

const mockedStatus = vi.mocked(adminService.getFinbertStatus);

beforeEach(() => {
    mockedStatus.mockResolvedValue({
        enabled: false,
        data: { installed: true, framework_ok: true, model_ready: false, device: -1 },
    } as never);
    vi.stubGlobal(
        'fetch',
        vi.fn().mockResolvedValue({
            ok: true,
            json: async () => ({
                id: 1,
                access_key: 'ak_1234567890abcdef',
                name: '交易系统默认Key',
                permissions: ['trade.read', 'trade.write'],
                is_active: true,
                created_at: '2026-11-01T00:00:00Z',
                expires_at: null,
                last_used_at: null,
            }),
        }),
    );
});

afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
});

describe('AdminSystemSettings 二级菜单', () => {
    it('默认进入金融情感；SDK 接入凭证是第二项、MCP 服务第三项', async () => {
        render(<AdminSystemSettings />);

        expect(screen.getByRole('button', { name: /金融情感/ })).toBeInTheDocument();
        expect(screen.getByRole('button', { name: /SDK 接入凭证/ })).toBeInTheDocument();
        expect(screen.getByRole('button', { name: /MCP 服务/ })).toBeInTheDocument();
        expect(await screen.findByText('FinBERT 中文金融情感')).toBeInTheDocument();
        // 未点第二项时不请求接入凭证
        expect(fetch).not.toHaveBeenCalled();
    });

    it('点 SDK 接入凭证：挂载密钥面板并读取 /api-keys/init', async () => {
        render(<AdminSystemSettings />);

        fireEvent.click(screen.getByRole('button', { name: /SDK 接入凭证/ }));

        expect(screen.getByText('SDK 接入凭证 / API 密钥')).toBeInTheDocument();
        await waitFor(() => expect(fetch).toHaveBeenCalled());
        expect(String((fetch as any).mock.calls[0][0])).toContain('/api/v1/api-keys/init');
        expect(await screen.findByText('Access Key')).toBeInTheDocument();
    });
});
