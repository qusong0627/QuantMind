/** 后台管理 · MCP 服务页：二级菜单、工具开关、客户端列表的接线护栏。 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';

vi.mock('../../services/mcpAdminService', () => ({
  mcpAdminService: {
    getConfig: vi.fn(),
    updateConfig: vi.fn(),
    listClients: vi.fn(),
    updateClient: vi.fn(),
    revokeClient: vi.fn(),
    issueKey: vi.fn(),
    getAudit: vi.fn(),
    health: vi.fn(),
  },
}));

import { mcpAdminService } from '../../services/mcpAdminService';
import { AdminMcpPanel } from '../AdminMcpPanel';

const mocked = vi.mocked(mcpAdminService);

beforeEach(() => {
  mocked.getConfig.mockResolvedValue({
    global: {
      enabled: true,
      tool_flags: { qm_capabilities: true, qm_sim_place_order: false },
      limits: { calls_per_minute: 60, sim_order_max_value: 50000 },
      updated_at: null,
      updated_by: null,
    },
    tools: [
      {
        name: 'qm_capabilities',
        description: '能力查询',
        permission: 'mcp.use',
        category: 'meta',
        version: 'v1',
        write: false,
        default_enabled: true,
        destructive: false,
      },
      {
        name: 'qm_sim_place_order',
        description: '模拟下单',
        permission: 'trade.write',
        category: 'trading',
        version: 'v1',
        write: true,
        default_enabled: false,
        destructive: true,
      },
    ],
    defaults: {
      tool_flags: { qm_capabilities: true, qm_sim_place_order: false },
      limits: { calls_per_minute: 60, sim_order_max_value: 50000 },
    },
    limit_specs: {
      calls_per_minute: { min: 1, max: 10000, label: '每分钟调用' },
      sim_order_max_value: { min: 0, max: 100000000, label: '单笔金额' },
    },
    permissions: [
      { code: 'mcp.use', label: '连接 MCP' },
      { code: 'trade.write', label: '模拟盘下单' },
    ],
    templates: { readonly: ['mcp.use'], sim_trading: ['mcp.use', 'trade.write'] },
    service_url: 'http://quantmind-mcp:8765',
  });
  mocked.health.mockResolvedValue({
    reachable: true,
    status_code: 200,
    detail: { status: 'ok' },
    url: 'http://quantmind-mcp:8765',
  });
  mocked.listClients.mockResolvedValue([]);
  mocked.getAudit.mockResolvedValue({ total: 0, items: [] });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('AdminMcpPanel', () => {
  it('默认概览：显示总开关与接入示例', async () => {
    render(<AdminMcpPanel />);

    expect(await screen.findByText('MCP 服务总开关')).toBeInTheDocument();
    expect(screen.getByText('客户端接入示例')).toBeInTheDocument();
    await waitFor(() => expect(mocked.getConfig).toHaveBeenCalledTimes(1));
  });

  it('工具权限页：列出工具与写工具标签', async () => {
    render(<AdminMcpPanel />);
    await screen.findByText('MCP 服务总开关');

    fireEvent.click(screen.getByRole('button', { name: /工具权限/ }));

    expect(await screen.findByText('qm_capabilities')).toBeInTheDocument();
    expect(screen.getByText('qm_sim_place_order')).toBeInTheDocument();
    expect(screen.getByText('写')).toBeInTheDocument();
  });

  it('客户端页：空态 + 签发入口', async () => {
    render(<AdminMcpPanel />);
    await screen.findByText('MCP 服务总开关');

    fireEvent.click(screen.getByRole('button', { name: /客户端与密钥/ }));

    expect(await screen.findByText('签发 MCP 密钥')).toBeInTheDocument();
    expect(screen.getByText('还没有签发过 MCP 密钥')).toBeInTheDocument();
  });
});
