/**
 * 后台管理 · 系统设置 · MCP 服务。
 *
 * 三块：概览（总开关/健康/接入示例）、工具权限（开关 + 配额）、客户端与审计。
 * 密钥授权复用 SDK Access Key；这里只改 `api_keys.permissions`，不另造账号。
 */

import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Divider,
  Empty,
  Form,
  Input,
  InputNumber,
  Modal,
  Select,
  Space,
  Spin,
  Switch,
  Table,
  Tag,
  message,
} from 'antd';
import {
  ApiOutlined,
  AuditOutlined,
  CopyOutlined,
  PlusOutlined,
  ReloadOutlined,
  ToolOutlined,
  UserOutlined,
} from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import {
  mcpAdminService,
  type McpAuditRow,
  type McpClient,
  type McpConfigPayload,
  type McpHealth,
} from '../services/mcpAdminService';

type TabKey = 'overview' | 'tools' | 'clients' | 'audit';

const TABS: Array<{ key: TabKey; label: string; icon: React.ReactNode }> = [
  { key: 'overview', label: '概览', icon: <ApiOutlined /> },
  { key: 'tools', label: '工具权限', icon: <ToolOutlined /> },
  { key: 'clients', label: '客户端与密钥', icon: <UserOutlined /> },
  { key: 'audit', label: '审计', icon: <AuditOutlined /> },
];

const STATUS_TONE: Record<string, string> = {
  ok: 'success',
  denied: 'warning',
  quota: 'warning',
  error: 'error',
};

export const AdminMcpPanel: React.FC = () => {
  const [tab, setTab] = useState<TabKey>('overview');
  const [config, setConfig] = useState<McpConfigPayload | null>(null);
  const [health, setHealth] = useState<McpHealth | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [toolFlags, setToolFlags] = useState<Record<string, boolean>>({});
  const [limits, setLimits] = useState<Record<string, number>>({});

  const [clients, setClients] = useState<McpClient[]>([]);
  const [clientsLoading, setClientsLoading] = useState(false);
  const [editClient, setEditClient] = useState<McpClient | null>(null);
  const [editPerms, setEditPerms] = useState<string[]>([]);
  const [editEnabled, setEditEnabled] = useState(true);
  const [issueOpen, setIssueOpen] = useState(false);
  const [issued, setIssued] = useState<{ access_key: string; secret_key: string; user_id: string } | null>(null);
  const [issueForm] = Form.useForm();

  const [audit, setAudit] = useState<{ total: number; items: McpAuditRow[] }>({ total: 0, items: [] });
  const [auditLoading, setAuditLoading] = useState(false);
  const [auditPage, setAuditPage] = useState(1);
  const [auditFilter, setAuditFilter] = useState<{ user_id?: string; tool?: string; status?: string }>({});

  const loadConfig = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await mcpAdminService.getConfig();
      setConfig(data);
      setToolFlags({ ...data.defaults.tool_flags, ...data.global.tool_flags });
      setLimits({ ...data.defaults.limits, ...data.global.limits });
    } catch (err: any) {
      setError(err?.response?.data?.detail || err?.message || '读取 MCP 配置失败');
    } finally {
      setLoading(false);
    }
  }, []);

  const loadHealth = useCallback(async () => {
    try {
      setHealth(await mcpAdminService.health());
    } catch {
      setHealth({ reachable: false, status_code: null, detail: '请求失败', url: '' });
    }
  }, []);

  const loadClients = useCallback(async () => {
    setClientsLoading(true);
    try {
      setClients(await mcpAdminService.listClients());
    } catch (err: any) {
      message.error(err?.response?.data?.detail || '读取客户端失败');
    } finally {
      setClientsLoading(false);
    }
  }, []);

  const loadAudit = useCallback(async (page = auditPage, filter = auditFilter) => {
    setAuditLoading(true);
    try {
      const data = await mcpAdminService.getAudit({
        limit: 50,
        offset: (page - 1) * 50,
        ...filter,
      });
      setAudit(data);
    } catch (err: any) {
      message.error(err?.response?.data?.detail || '读取审计失败');
    } finally {
      setAuditLoading(false);
    }
  }, [auditFilter, auditPage]);

  useEffect(() => {
    void loadConfig();
    void loadHealth();
  }, [loadConfig, loadHealth]);

  useEffect(() => {
    if (tab === 'clients') void loadClients();
    if (tab === 'audit') void loadAudit(1, auditFilter);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab]);

  const saveGlobal = useCallback(async () => {
    setSaving(true);
    try {
      await mcpAdminService.updateConfig({ tool_flags: toolFlags, limits });
      message.success('MCP 配置已保存（热生效）');
      await loadConfig();
    } catch (err: any) {
      message.error(err?.response?.data?.detail || '保存失败');
    } finally {
      setSaving(false);
    }
  }, [limits, loadConfig, toolFlags]);

  const toggleGlobal = useCallback(
    async (checked: boolean) => {
      try {
        await mcpAdminService.updateConfig({ enabled: checked });
        message.success(checked ? 'MCP 服务已启用' : 'MCP 服务已停用');
        await loadConfig();
      } catch (err: any) {
        message.error(err?.response?.data?.detail || '切换失败');
      }
    },
    [loadConfig],
  );

  const openEdit = (client: McpClient) => {
    setEditClient(client);
    setEditPerms(client.permissions ?? []);
    setEditEnabled(client.mcp?.enabled ?? true);
  };

  const saveEdit = async () => {
    if (!editClient) return;
    try {
      await mcpAdminService.updateClient(editClient.access_key, {
        enabled: editEnabled,
        permissions: editPerms,
      });
      message.success('客户端已更新');
      setEditClient(null);
      await loadClients();
    } catch (err: any) {
      message.error(err?.response?.data?.detail || '更新失败');
    }
  };

  const revoke = (client: McpClient) => {
    Modal.confirm({
      title: `吊销 ${client.name || client.access_key_fp} 的 MCP 访问？`,
      content: '对应 API Key 会被停用、MCP 开关关闭；已签发的令牌会在下一次校验时失效。',
      okText: '吊销',
      okButtonProps: { danger: true },
      onOk: async () => {
        try {
          await mcpAdminService.revokeClient(client.access_key);
          message.success('已吊销');
          await loadClients();
        } catch (err: any) {
          message.error(err?.response?.data?.detail || '吊销失败');
        }
      },
    });
  };

  const issue = async () => {
    const values = await issueForm.validateFields();
    try {
      const data = await mcpAdminService.issueKey(values);
      setIssued(data);
      setIssueOpen(false);
      issueForm.resetFields();
      await loadClients();
    } catch (err: any) {
      message.error(err?.response?.data?.detail || '签发失败');
    }
  };

  const copy = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      message.success('已复制');
    } catch {
      message.error('复制失败');
    }
  };

  const mcpEndpoint = useMemo(
    () => (typeof window !== 'undefined' ? `${window.location.origin}/mcp` : '/mcp'),
    [],
  );

  const toolRows = config?.tools ?? [];
  const groupedTools = useMemo(() => {
    const groups: Record<string, typeof toolRows> = {};
    for (const tool of toolRows) (groups[tool.category] ||= []).push(tool);
    return groups;
  }, [toolRows]);

  const clientColumns: ColumnsType<McpClient> = [
    {
      title: '用户 / 名称',
      key: 'user',
      render: (_, row) => (
        <div>
          <div className="font-bold text-slate-800">{row.name || '未命名'}</div>
          <div className="font-mono text-[11px] text-slate-400">
            {row.tenant_id}:{row.user_id} · …{row.access_key_fp}
          </div>
        </div>
      ),
    },
    {
      title: '权限',
      dataIndex: 'permissions',
      render: (perms: string[]) => (
        <div className="flex flex-wrap gap-1">
          {(perms ?? []).map((code) => (
            <Tag key={code} className="m-0 text-[10px]">
              {config?.permissions.find((p) => p.code === code)?.label || code}
            </Tag>
          ))}
        </div>
      ),
    },
    {
      title: 'MCP',
      key: 'mcp',
      width: 110,
      render: (_, row) => {
        const hasMcp = (row.permissions ?? []).includes('mcp.use');
        if (!hasMcp) return <Tag>未开通</Tag>;
        return (row.mcp?.enabled ?? true) ? <Tag color="success">已启用</Tag> : <Tag>未启用</Tag>;
      },
    },
    {
      title: '最近使用',
      dataIndex: 'last_used_at',
      width: 160,
      render: (value: string | null) => (value ? value.replace('T', ' ').slice(0, 19) : '从未'),
    },
    {
      title: '操作',
      key: 'actions',
      width: 170,
      render: (_, row) => (
        <Space size={4}>
          <Button size="small" onClick={() => openEdit(row)}>编辑</Button>
          <Button size="small" danger onClick={() => revoke(row)}>吊销</Button>
        </Space>
      ),
    },
  ];

  const auditColumns: ColumnsType<McpAuditRow> = [
    { title: '时间', dataIndex: 'ts', width: 150, render: (v: string | null) => (v ? v.replace('T', ' ').slice(0, 19) : '—') },
    { title: '用户', dataIndex: 'user_id', width: 120 },
    { title: '工具', dataIndex: 'tool', width: 190 },
    {
      title: '状态',
      dataIndex: 'status',
      width: 90,
      render: (value: string, row) => (
        <Tag color={STATUS_TONE[value] || 'default'}>{row.error_code || value}</Tag>
      ),
    },
    { title: '耗时', dataIndex: 'latency_ms', width: 80, render: (v: number | null) => (v == null ? '—' : `${v}ms`) },
    { title: 'Key', dataIndex: 'access_key_fp', width: 100 },
    { title: 'request_id', dataIndex: 'request_id', width: 160, render: (v: string | null) => v || '—' },
  ];

  if (loading) {
    return <div className="py-16 text-center"><Spin /></div>;
  }
  if (error) {
    return (
      <Alert
        type="error"
        showIcon
        message="MCP 配置读取失败"
        description={error}
        action={<Button size="small" onClick={() => void loadConfig()}>重试</Button>}
      />
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-1 border-b border-slate-200 pb-2">
        {TABS.map((item) => (
          <button
            key={item.key}
            type="button"
            onClick={() => setTab(item.key)}
            className={`flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-bold transition-colors ${
              tab === item.key
                ? 'bg-slate-900 text-white'
                : 'text-slate-500 hover:bg-slate-100 hover:text-slate-700'
            }`}
          >
            {item.icon}
            {item.label}
          </button>
        ))}
        <Button
          size="small"
          className="ml-auto"
          icon={<ReloadOutlined />}
          onClick={() => {
            void loadConfig();
            void loadHealth();
            if (tab === 'clients') void loadClients();
            if (tab === 'audit') void loadAudit(1, auditFilter);
          }}
        >
          刷新
        </Button>
      </div>

      {tab === 'overview' && config && (
        <div className="space-y-4">
          <Card className="rounded-2xl border-slate-200 shadow-sm">
            <div className="flex items-center justify-between gap-4">
              <div>
                <div className="text-sm font-bold text-slate-800">MCP 服务总开关</div>
                <div className="mt-1 text-xs text-slate-500">
                  停用后除 /health 外的 MCP 调用一律返回 503；配置热生效，无需重启。
                </div>
              </div>
              <Switch
                checked={config.global.enabled}
                onChange={(checked) => void toggleGlobal(checked)}
                checkedChildren="启用"
                unCheckedChildren="停用"
              />
            </div>
          </Card>

          <Card
            className="rounded-2xl border-slate-200 shadow-sm"
            title={<span className="text-sm font-black text-slate-800">服务状态</span>}
            extra={<Button size="small" onClick={() => void loadHealth()}>探测</Button>}
          >
            <Descriptions size="small" column={1}>
              <Descriptions.Item label="健康">
                {health?.reachable ? <Tag color="success">可达</Tag> : <Tag color="error">不可达</Tag>}
              </Descriptions.Item>
              <Descriptions.Item label="内部地址">
                <code className="text-xs">{config.service_url}</code>
              </Descriptions.Item>
              <Descriptions.Item label="对外地址">
                <code className="text-xs">{mcpEndpoint}</code>
                <span className="ml-2 text-[11px] text-slate-400">需 nginx 将 /mcp 反代到 mcp:8765</span>
              </Descriptions.Item>
              <Descriptions.Item label="配置更新时间">
                {config.global.updated_at ? config.global.updated_at.replace('T', ' ').slice(0, 19) : '—'}
                <span className="ml-2 text-[11px] text-slate-400">{config.global.updated_by || ''}</span>
              </Descriptions.Item>
            </Descriptions>
            {health && !health.reachable && (
              <Alert
                className="mt-3"
                type="warning"
                showIcon
                message="MCP 容器不可达"
                description={String(health.detail)}
              />
            )}
          </Card>

          <Card
            className="rounded-2xl border-slate-200 shadow-sm"
            title={<span className="text-sm font-black text-slate-800">客户端接入示例</span>}
            extra={<Button size="small" icon={<CopyOutlined />} onClick={() => void copy(JSON.stringify({
              mcpServers: {
                quantmind: {
                  type: 'http',
                  url: mcpEndpoint,
                  headers: {
                    'X-QM-Access-Key': '<Access Key>',
                    'X-QM-Secret-Key': '<Secret Key>',
                  },
                },
              },
            }, null, 2))}>复制</Button>}
          >
            <pre className="m-0 overflow-x-auto rounded-xl bg-slate-950 p-3 text-[11px] leading-relaxed text-slate-200">{`{
  "mcpServers": {
    "quantmind": {
      "type": "http",
      "url": "${mcpEndpoint}",
      "headers": {
        "X-QM-Access-Key": "<Access Key>",
        "X-QM-Secret-Key": "<Secret Key>"
      }
    }
  }
}`}</pre>
          </Card>
        </div>
      )}

      {tab === 'tools' && config && (
        <div className="space-y-4">
          <Card
            className="rounded-2xl border-slate-200 shadow-sm"
            title={<span className="text-sm font-black text-slate-800">工具开关</span>}
            extra={<Button type="primary" size="small" loading={saving} onClick={() => void saveGlobal()}>保存</Button>}
          >
            {Object.entries(groupedTools).map(([category, tools]) => (
              <div key={category} className="mb-4 last:mb-0">
                <div className="mb-2 text-[11px] font-black uppercase tracking-wider text-slate-400">{category}</div>
                <div className="grid grid-cols-1 gap-2 lg:grid-cols-2">
                  {tools.map((tool) => (
                    <div key={tool.name} className="flex items-center justify-between gap-3 rounded-xl border border-slate-100 px-3 py-2">
                      <div className="min-w-0">
                        <div className="flex items-center gap-1.5">
                          <code className="text-[11px] font-bold text-slate-700">{tool.name}</code>
                          {tool.write && <Tag color="orange" className="m-0 text-[9px]">写</Tag>}
                          {tool.destructive && <Tag color="red" className="m-0 text-[9px]">需谨慎</Tag>}
                        </div>
                        <div className="truncate text-[11px] text-slate-400" title={tool.description}>{tool.description}</div>
                      </div>
                      <Switch
                        size="small"
                        checked={toolFlags[tool.name] ?? tool.default_enabled}
                        onChange={(checked) => setToolFlags((prev) => ({ ...prev, [tool.name]: checked }))}
                      />
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </Card>

          <Card
            className="rounded-2xl border-slate-200 shadow-sm"
            title={<span className="text-sm font-black text-slate-800">全局配额</span>}
            extra={<Button type="primary" size="small" loading={saving} onClick={() => void saveGlobal()}>保存</Button>}
          >
            <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
              {Object.entries(config.limit_specs).map(([key, spec]) => (
                <div key={key}>
                  <div className="mb-1 text-[11px] font-bold text-slate-500">{spec.label}</div>
                  <InputNumber
                    className="w-full"
                    min={spec.min}
                    max={spec.max}
                    value={limits[key]}
                    onChange={(value) => setLimits((prev) => ({ ...prev, [key]: Number(value ?? 0) }))}
                  />
                </div>
              ))}
            </div>
          </Card>
        </div>
      )}

      {tab === 'clients' && (
        <Card
          className="rounded-2xl border-slate-200 shadow-sm"
          title={<span className="text-sm font-black text-slate-800">MCP 客户端与密钥</span>}
          extra={<Button type="primary" size="small" icon={<PlusOutlined />} onClick={() => setIssueOpen(true)}>签发 MCP 密钥</Button>}
        >
          <Table
            rowKey="access_key"
            size="small"
            loading={clientsLoading}
            columns={clientColumns}
            dataSource={clients}
            pagination={{ pageSize: 20, showSizeChanger: false }}
            locale={{ emptyText: <Empty description="还没有签发过 MCP 密钥" /> }}
          />
        </Card>
      )}

      {tab === 'audit' && (
        <Card
          className="rounded-2xl border-slate-200 shadow-sm"
          title={<span className="text-sm font-black text-slate-800">MCP 调用审计</span>}
          extra={<Button size="small" onClick={() => void loadAudit(auditPage, auditFilter)}>刷新</Button>}
        >
          <div className="mb-3 flex flex-wrap items-center gap-2">
            <Input
              size="small"
              placeholder="用户 id"
              allowClear
              className="w-40"
              onChange={(e) => setAuditFilter((prev) => ({ ...prev, user_id: e.target.value.trim() || undefined }))}
            />
            <Select
              size="small"
              placeholder="工具"
              allowClear
              className="w-48"
              options={toolRows.map((tool) => ({ value: tool.name, label: tool.name }))}
              onChange={(value) => setAuditFilter((prev) => ({ ...prev, tool: value }))}
            />
            <Select
              size="small"
              placeholder="状态"
              allowClear
              className="w-32"
              options={['ok', 'denied', 'quota', 'error'].map((value) => ({ value, label: value }))}
              onChange={(value) => setAuditFilter((prev) => ({ ...prev, status: value }))}
            />
            <Button size="small" onClick={() => void loadAudit(1, auditFilter)}>查询</Button>
          </div>
          <Table
            rowKey="id"
            size="small"
            loading={auditLoading}
            columns={auditColumns}
            dataSource={audit.items}
            pagination={{
              current: auditPage,
              pageSize: 50,
              total: audit.total,
              showSizeChanger: false,
              onChange: (page) => {
                setAuditPage(page);
                void loadAudit(page, auditFilter);
              },
            }}
          />
        </Card>
      )}

      <Modal
        open={!!editClient}
        title="编辑 MCP 客户端"
        onCancel={() => setEditClient(null)}
        onOk={() => void saveEdit()}
        okText="保存"
        cancelText="取消"
        width={640}
      >
        {editClient && (
          <div className="space-y-3">
            <div className="text-xs text-slate-500">
              {editClient.tenant_id}:{editClient.user_id} · …{editClient.access_key_fp}
            </div>
            <div className="flex items-center justify-between">
              <span className="text-xs font-bold text-slate-600">MCP 启用</span>
              <Switch checked={editEnabled} onChange={setEditEnabled} />
            </div>
            <Divider className="!my-2" />
            <div className="text-xs font-bold text-slate-600">权限</div>
            <div className="grid grid-cols-1 gap-1.5 md:grid-cols-2">
              {(config?.permissions ?? []).map((perm) => (
                <label key={perm.code} className="flex items-center gap-2 rounded-lg border border-slate-100 px-2 py-1.5 text-[11px]">
                  <input
                    type="checkbox"
                    checked={editPerms.includes(perm.code)}
                    onChange={(e) =>
                      setEditPerms((prev) =>
                        e.target.checked ? [...prev, perm.code] : prev.filter((code) => code !== perm.code),
                      )
                    }
                  />
                  <span className="font-bold text-slate-700">{perm.label}</span>
                  <code className="ml-auto text-[10px] text-slate-400">{perm.code}</code>
                </label>
              ))}
            </div>
          </div>
        )}
      </Modal>

      <Modal
        open={issueOpen}
        title="签发 MCP 密钥"
        onCancel={() => setIssueOpen(false)}
        onOk={() => void issue()}
        okText="签发"
        cancelText="取消"
      >
        <Form form={issueForm} layout="vertical" initialValues={{ tenant_id: 'default', template: 'readonly', name: 'MCP 客户端' }}>
          <Form.Item name="user_id" label="用户 ID" rules={[{ required: true, message: '填写用户 ID' }]}>
            <Input placeholder="如 10000001" />
          </Form.Item>
          <Form.Item name="tenant_id" label="租户" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="name" label="名称" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="template" label="权限模板" rules={[{ required: true }]}>
            <Select
              options={Object.keys(config?.templates ?? {}).map((key) => ({
                value: key,
                label: key === 'readonly' ? '只读' : key === 'research' ? '研究' : '模拟交易',
              }))}
            />
          </Form.Item>
        </Form>
        <Alert type="info" showIcon message="Secret Key 只展示一次；请让用户立即保存。" />
      </Modal>

      <Modal
        open={!!issued}
        title="MCP 密钥已签发（仅展示一次）"
        onCancel={() => setIssued(null)}
        footer={<Button type="primary" onClick={() => setIssued(null)}>我已保存</Button>}
      >
        {issued && (
          <div className="space-y-3">
            <Alert type="warning" showIcon message="关闭后无法再次查看 Secret Key。" />
            <div>
              <div className="text-[11px] font-bold text-slate-500">Access Key</div>
              <div className="flex items-center gap-2">
                <code className="flex-1 break-all rounded bg-slate-50 p-2 text-xs">{issued.access_key}</code>
                <Button size="small" icon={<CopyOutlined />} onClick={() => void copy(issued.access_key)} />
              </div>
            </div>
            <div>
              <div className="text-[11px] font-bold text-slate-500">Secret Key</div>
              <div className="flex items-center gap-2">
                <code className="flex-1 break-all rounded bg-amber-50 p-2 text-xs">{issued.secret_key}</code>
                <Button size="small" icon={<CopyOutlined />} onClick={() => void copy(issued.secret_key)} />
              </div>
            </div>
          </div>
        )}
      </Modal>
    </div>
  );
};

export default AdminMcpPanel;
