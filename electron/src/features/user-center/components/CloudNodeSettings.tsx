import React, { useState, useEffect, useCallback, useRef } from 'react';
import { Button, Spin, Modal, Form, Input, InputNumber, message, Select, Popconfirm, Empty } from 'antd';
import { Server, Plus, Trash2, Pencil, PlugZap, RefreshCw, Cpu, HardDrive, MemoryStick, CircuitBoard, Cloud } from 'lucide-react';
import { adminService } from '../../admin/services/adminService';
import { parseSshSnippet, suggestAutodlNodeId } from '../utils/parseSshSnippet';

interface CloudNodeInfo {
  id: string;
  name?: string;
  host?: string;
  port?: number;
  type?: 'local' | 'remote';
  description?: string;
  available?: boolean;
  status?: NodeStatusData;
}

interface CloudNodeDetail {
  id: string;
  name?: string;
  host?: string;
  port?: number;
  user?: string;
  work_dir?: string;
  docker_image?: string;
  gpus?: string;
  exec_mode?: string;
  quantdb_dir?: string;
  has_password?: boolean;
  has_key?: boolean;
}

interface NodeStatusData {
  online?: boolean;
  error?: string;
  cpu_cores?: number;
  cpu_load?: number;
  mem_total_mb?: number;
  mem_used_mb?: number;
  disk_total_kb?: number;
  disk_used_kb?: number;
  gpus?: { util: number; mem_used_mb: number; mem_total_mb: number; temp_c: number; name: string }[];
  containers?: { name: string; status: string }[];
  training_active?: boolean;
  gpu_error?: string;
  exec_mode?: string;
}

interface NodeFormValues {
  id?: string;
  name: string;
  host: string;
  port: number;
  user: string;
  ssh_password?: string;
  ssh_key?: string;
  work_dir: string;
  docker_image?: string;
  gpus: string;
  exec_mode: 'native_python' | 'ssh_docker';
  quantdb_dir: string;
}

const ENV = (import.meta as any).env || {};

const DEFAULT_FORM: NodeFormValues = {
  id: '',
  name: ENV.VITE_AUTODL_DEFAULT_NAME || '',
  host: ENV.VITE_AUTODL_DEFAULT_HOST || '',
  port: ENV.VITE_AUTODL_DEFAULT_PORT ? Number(ENV.VITE_AUTODL_DEFAULT_PORT) : 22,
  user: ENV.VITE_AUTODL_DEFAULT_USER || 'root',
  ssh_password: '',
  ssh_key: '',
  work_dir: ENV.VITE_AUTODL_DEFAULT_WORK_DIR || '/root/workspace',
  docker_image: '',
  gpus: 'all',
  exec_mode: 'native_python',
  quantdb_dir: '/root/autodl-fs/quantdb',
};

function formatGbFromMb(mb?: number): string {
  if (mb == null || !Number.isFinite(mb) || mb <= 0) return '—';
  const gb = mb / 1024;
  return gb >= 100 ? gb.toFixed(0) : gb.toFixed(1);
}

function formatGbFromKb(kb?: number): string {
  if (kb == null || !Number.isFinite(kb) || kb <= 0) return '—';
  const gb = kb / 1024 / 1024;
  return gb >= 100 ? gb.toFixed(0) : gb.toFixed(1);
}

function pct(used?: number, total?: number): number | undefined {
  if (!used || !total || total <= 0) return undefined;
  return Math.min(100, Math.max(0, (used / total) * 100));
}

const MetricTile: React.FC<{
  icon: React.ReactNode;
  label: string;
  value: string;
  hint?: string;
  bar?: number;
  accent?: string;
}> = ({ icon, label, value, hint, bar, accent = 'bg-indigo-500' }) => (
  <div className="rounded-xl border border-slate-100 bg-slate-50/70 px-3 py-2.5 min-w-0">
    <div className="flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wider text-slate-400">
      {icon}
      {label}
    </div>
    <div className="mt-1 text-[13px] font-semibold text-slate-900 tabular-nums truncate leading-tight">{value}</div>
    {hint ? <div className="mt-0.5 text-[11px] text-slate-500 truncate">{hint}</div> : null}
    {typeof bar === 'number' ? (
      <div className="mt-2 h-1 rounded-full bg-slate-200 overflow-hidden">
        <div className={`h-full rounded-full ${accent}`} style={{ width: `${bar}%` }} />
      </div>
    ) : null}
  </div>
);

export const CloudNodeSettings: React.FC = () => {
  const [nodes, setNodes] = useState<CloudNodeInfo[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [isModalOpen, setIsModalOpen] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [statusMap, setStatusMap] = useState<Record<string, NodeStatusData>>({});
  const [testingId, setTestingId] = useState<string | null>(null);
  const [statusLoadingId, setStatusLoadingId] = useState<string | null>(null);
  const [sshPaste, setSshPaste] = useState('');
  const [idTouched, setIdTouched] = useState(false);
  const [form] = Form.useForm<NodeFormValues>();
  const execMode = Form.useWatch('exec_mode', form);
  const nodesRef = useRef<CloudNodeInfo[]>([]);
  const statusMapRef = useRef<Record<string, NodeStatusData>>({});
  const pollingRef = useRef(false);
  nodesRef.current = nodes;
  statusMapRef.current = statusMap;

  const collectStatuses = useCallback(async (nodeList?: CloudNodeInfo[]) => {
    const list = nodeList ?? nodesRef.current;
    if (!list.length || pollingRef.current) return;
    pollingRef.current = true;
    try {
      const nextStatus: Record<string, NodeStatusData> = { ...statusMapRef.current };
      await Promise.all(
        list.map(async (n) => {
          try {
            nextStatus[n.id] = await adminService.getTrainingNodeStatus(n.id) as NodeStatusData;
          } catch {
            /* 自动采集失败时保留上次结果 */
          }
        }),
      );
      statusMapRef.current = nextStatus;
      setStatusMap(nextStatus);
    } finally {
      pollingRef.current = false;
    }
  }, []);

  const loadNodes = useCallback(async (opts?: { spin?: boolean }) => {
    if (opts?.spin !== false) setIsLoading(true);
    try {
      const resp = await adminService.listTrainingNodes(false);
      const remoteNodes = (resp?.nodes || []).filter((n: CloudNodeInfo) => n.type === 'remote');
      nodesRef.current = remoteNodes;
      setNodes(remoteNodes);
      setIsLoading(false);
      await collectStatuses(remoteNodes);
    } catch (error: any) {
      message.error(error.message || '加载节点列表失败');
    } finally {
      setIsLoading(false);
    }
  }, [collectStatuses]);

  useEffect(() => {
    void loadNodes();
  }, [loadNodes]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      if (document.hidden) return;
      void collectStatuses();
    }, 10_000);
    return () => window.clearInterval(timer);
  }, [collectStatuses]);

  const applySshPaste = (raw: string, opts?: { notify?: boolean }) => {
    setSshPaste(raw);
    const parsed = parseSshSnippet(raw);
    if (!parsed.host && !parsed.port && !parsed.user && !parsed.ssh_password && !parsed.ssh_key) {
      return;
    }
    const patch: Partial<NodeFormValues> = {};
    if (parsed.host) patch.host = parsed.host;
    if (parsed.port) patch.port = parsed.port;
    if (parsed.user) patch.user = parsed.user;
    if (parsed.ssh_password) patch.ssh_password = parsed.ssh_password;
    if (parsed.ssh_key) patch.ssh_key = parsed.ssh_key;
    const name = form.getFieldValue('name');
    if (!name && parsed.host) {
      patch.name = parsed.host.split('.')[0] || parsed.host;
    }
    if (!editingId && !idTouched) {
      patch.id = suggestAutodlNodeId(String(name || patch.name || parsed.host || ''));
    }
    form.setFieldsValue(patch);
    if (opts?.notify) {
      message.success('已从粘贴内容解析 SSH 连接信息');
    }
  };

  const openCreate = () => {
    setEditingId(null);
    setIdTouched(false);
    setSshPaste('');
    form.setFieldsValue(DEFAULT_FORM);
    setIsModalOpen(true);
  };

  const openEdit = async (node: CloudNodeInfo) => {
    setEditingId(node.id);
    setIdTouched(true);
    setSshPaste('');
    try {
      const resp = await adminService.getTrainingNodeDetail(node.id);
      if (resp?.success && resp.node) {
        const d: CloudNodeDetail = resp.node;
        form.setFieldsValue({
          id: d.id,
          name: d.name || '',
          host: d.host || '',
          port: d.port || 22,
          user: d.user || 'root',
          ssh_password: '',
          ssh_key: '',
          work_dir: d.work_dir || '/root/workspace',
          docker_image: d.docker_image || '',
          gpus: d.gpus || 'all',
          exec_mode: d.exec_mode === 'ssh_docker' ? 'ssh_docker' : 'native_python',
          quantdb_dir: d.quantdb_dir || '/root/autodl-fs/quantdb',
        });
      } else {
        form.setFieldsValue({ ...DEFAULT_FORM, name: node.name || '', host: node.host || '', id: node.id });
      }
      setIsModalOpen(true);
    } catch (error: any) {
      message.error(error.message || '加载节点详情失败');
    }
  };

  const handleSave = async () => {
    try {
      const values = await form.validateFields();
      const nodeId = editingId || suggestAutodlNodeId(values.id || values.name || values.host);
      if (!values.ssh_password && !values.ssh_key && !editingId) {
        message.error('请填写 SSH 密码或密钥路径');
        return;
      }
      const payload = {
        id: nodeId,
        name: values.name || nodeId,
        host: values.host,
        port: values.port,
        user: values.user,
        ssh_password: values.ssh_password || undefined,
        ssh_key: values.ssh_key || undefined,
        work_dir: values.work_dir,
        docker_image: values.exec_mode === 'native_python' ? '' : (values.docker_image || ''),
        gpus: values.gpus,
        exec_mode: values.exec_mode,
        quantdb_dir: values.quantdb_dir,
      };
      const resp = await adminService.saveTrainingNode(payload);
      if (resp?.success) {
        message.success(editingId ? '节点已更新' : '节点已创建');
        setIsModalOpen(false);
        await loadNodes();
      } else {
        message.error(resp?.error || '保存失败');
      }
    } catch (error: any) {
      if (error?.errorFields) return;
      message.error(error.message || '保存失败');
    }
  };

  const handleDelete = async (nodeId: string) => {
    try {
      const resp = await adminService.deleteTrainingNode(nodeId);
      if (resp?.success) {
        message.success('节点已删除');
        await loadNodes();
      } else {
        message.warning(resp?.success === false ? '节点不存在或删除失败' : '删除失败');
      }
    } catch (error: any) {
      message.error(error.message || '删除失败');
    }
  };

  const handleTest = async (nodeId: string) => {
    setTestingId(nodeId);
    try {
      const resp = await adminService.testTrainingNode(nodeId);
      if (resp?.success && resp.ssh) {
        if (resp.exec_mode === 'native_python' || resp.native_python) {
          message.success(`节点 ${nodeId} SSH 可用（免 Docker）`);
        } else if (resp.docker) {
          message.success(`节点 ${nodeId} SSH 与 Docker 均可用`);
        } else {
          message.warning(`节点 ${nodeId} SSH 可用，但 Docker 不可用（免 Docker 节点可忽略）`);
        }
      } else {
        message.error(resp?.error || '测试连接失败');
      }
    } catch (error: any) {
      message.error(error.message || '测试连接失败');
    } finally {
      setTestingId(null);
    }
  };

  const handleFetchStatus = async (nodeId: string) => {
    setStatusLoadingId(nodeId);
    try {
      const st = await adminService.getTrainingNodeStatus(nodeId);
      const next = { ...statusMapRef.current, [nodeId]: st as NodeStatusData };
      statusMapRef.current = next;
      setStatusMap(next);
    } catch (error: any) {
      message.error(error.message || '获取状态失败');
    } finally {
      setStatusLoadingId(null);
    }
  };

  const renderMetrics = (node: CloudNodeInfo) => {
    const st = statusMap[node.id];
    if (statusLoadingId === node.id && !st) {
      return (
        <div className="h-[76px] rounded-xl border border-slate-100 bg-slate-50/70 flex items-center justify-center">
          <Spin size="small" />
        </div>
      );
    }
    if (!st) {
      return (
        <div className="rounded-xl border border-dashed border-slate-200 bg-slate-50/40 px-4 py-3 text-[12px] text-slate-400">
          尚未采集实时状态，点击右上角刷新即可
        </div>
      );
    }
    if (!st.online) {
      return (
        <div className="rounded-xl border border-rose-100 bg-rose-50/70 px-4 py-3 text-[12px] text-rose-700">
          {st.error || '节点离线，无法采集硬件状态'}
        </div>
      );
    }

    const memPct = pct(st.mem_used_mb, st.mem_total_mb);
    const diskPct = pct(st.disk_used_kb, st.disk_total_kb);
    const gpu = st.gpus?.[0];
    const gpuHint = gpu
      ? `${gpu.name.replace(/^NVIDIA GeForce /, '')} · ${gpu.temp_c}°C`
      : (st.gpu_error || '未检测到 GPU');

    return (
      <div className="grid grid-cols-2 xl:grid-cols-4 gap-2.5">
        <MetricTile
          icon={<Cpu className="w-3 h-3" />}
          label="CPU"
          value={st.cpu_cores ? `${st.cpu_cores} 核` : '—'}
          hint={st.cpu_load != null ? `负载 ${Number(st.cpu_load).toFixed(2)}` : '实时核数'}
        />
        <MetricTile
          icon={<MemoryStick className="w-3 h-3" />}
          label="内存"
          value={`${formatGbFromMb(st.mem_used_mb)} / ${formatGbFromMb(st.mem_total_mb)} GB`}
          hint={memPct != null ? `已用 ${memPct.toFixed(0)}%` : undefined}
          bar={memPct}
          accent="bg-sky-500"
        />
        <MetricTile
          icon={<HardDrive className="w-3 h-3" />}
          label="系统盘"
          value={`${formatGbFromKb(st.disk_used_kb)} / ${formatGbFromKb(st.disk_total_kb)} GB`}
          hint={diskPct != null ? `已用 ${diskPct.toFixed(0)}%` : undefined}
          bar={diskPct}
          accent="bg-amber-500"
        />
        <MetricTile
          icon={<CircuitBoard className="w-3 h-3" />}
          label="GPU"
          value={gpu ? `${gpu.util}%` : '—'}
          hint={gpuHint}
          bar={gpu ? gpu.util : undefined}
          accent="bg-violet-500"
        />
      </div>
    );
  };

  if (isLoading) {
    return (
      <div className="w-full space-y-3">
        <div className="bg-white rounded-2xl border border-slate-200/80 p-10 flex items-center justify-center min-h-[220px]">
          <Spin />
        </div>
      </div>
    );
  }

  return (
    <div className="w-full space-y-3">
      <div className="bg-white rounded-2xl border border-slate-200/80 px-5 py-3.5 shadow-xs flex items-center justify-between gap-3">
        <div className="flex items-center gap-3 min-w-0">
          <div className="w-9 h-9 rounded-xl bg-gradient-to-br from-indigo-500 to-violet-600 flex items-center justify-center text-white shadow-sm shrink-0">
            <Cloud className="w-4 h-4" />
          </div>
          <div className="min-w-0">
            <h3 className="text-sm font-black text-slate-800 m-0">AutoDL 训练节点</h3>
            <p className="text-[11px] text-slate-400 m-0 leading-tight truncate">
              远程 GPU 实例，默认免 Docker · 状态每 10 秒自动采集
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <Button
            size="small"
            icon={<RefreshCw className={`w-3 h-3 ${isLoading ? 'animate-spin' : ''}`} />}
            onClick={() => void loadNodes({ spin: false })}
            className="rounded-lg font-bold text-xs h-7 px-3"
          >
            刷新状态
          </Button>
          <Button
            type="primary"
            size="small"
            icon={<Plus className="w-3.5 h-3.5" />}
            onClick={openCreate}
            className="rounded-lg font-bold text-xs h-7 px-3"
          >
            新建节点
          </Button>
        </div>
      </div>

      {nodes.length === 0 ? (
        <div className="bg-white rounded-2xl border border-slate-200/80 px-6 py-14 text-center">
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description={<span className="text-slate-500">还没有云端节点，粘贴 AutoDL 的 SSH 命令即可添加</span>}
          >
            <Button type="primary" icon={<Plus className="w-3.5 h-3.5" />} onClick={openCreate} className="rounded-lg">
              新建节点
            </Button>
          </Empty>
        </div>
      ) : (
        nodes.map((node) => {
          const st = statusMap[node.id];
          const online = Boolean(st?.online);
          const gpuName = st?.gpus?.[0]?.name?.replace(/^NVIDIA GeForce /, '') || '';
          return (
            <div key={node.id} className="bg-white rounded-2xl border border-slate-200/80 shadow-xs overflow-hidden">
              <div className="px-5 py-4 flex items-start justify-between gap-4">
                <div className="flex items-start gap-3 min-w-0">
                  <div className="w-10 h-10 rounded-xl bg-slate-50 border border-slate-100 flex items-center justify-center shrink-0">
                    <Server className="w-5 h-5 text-indigo-500" />
                  </div>
                  <div className="min-w-0">
                    <div className="flex items-center gap-2 flex-wrap">
                      <h4 className="text-sm font-black text-slate-800 m-0 truncate">{node.name || node.id}</h4>
                      <span
                        className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-bold ${
                          online ? 'bg-emerald-50 text-emerald-700' : 'bg-slate-100 text-slate-500'
                        }`}
                      >
                        <span className={`w-1.5 h-1.5 rounded-full ${online ? 'bg-emerald-500' : 'bg-slate-400'}`} />
                        {online ? '在线' : st ? '离线' : '待检测'}
                      </span>
                      {st?.training_active ? (
                        <span className="inline-flex rounded-full bg-blue-50 text-blue-700 px-2 py-0.5 text-[10px] font-bold">
                          训练中
                        </span>
                      ) : null}
                    </div>
                    <div className="mt-1 flex items-center gap-2 text-[11px] text-slate-500 flex-wrap">
                      <code className="rounded-md bg-slate-50 border border-slate-100 px-1.5 py-0.5 text-[11px] text-slate-600">
                        {node.id}
                      </code>
                      <span className="truncate">{node.host}{node.port ? `:${node.port}` : ''}</span>
                      {gpuName ? <span className="text-slate-400">· {gpuName}</span> : null}
                    </div>
                  </div>
                </div>
                <div className="flex items-center gap-1 shrink-0">
                  <Button
                    size="small"
                    type="text"
                    className="!text-slate-500 !text-xs !h-7 !px-2"
                    icon={<PlugZap className="w-3.5 h-3.5" />}
                    loading={testingId === node.id}
                    onClick={() => void handleTest(node.id)}
                  >
                    连接
                  </Button>
                  <Button
                    size="small"
                    type="text"
                    className="!text-slate-500 !text-xs !h-7 !px-2"
                    icon={<RefreshCw className="w-3.5 h-3.5" />}
                    loading={statusLoadingId === node.id}
                    onClick={() => void handleFetchStatus(node.id)}
                  >
                    刷新
                  </Button>
                  <Button
                    size="small"
                    type="text"
                    className="!text-slate-500 !text-xs !h-7 !px-2"
                    icon={<Pencil className="w-3.5 h-3.5" />}
                    onClick={() => void openEdit(node)}
                  >
                    编辑
                  </Button>
                  <Popconfirm
                    title="确认删除此节点？"
                    description="将从配置中移除该 AutoDL 节点"
                    okText="删除"
                    cancelText="取消"
                    onConfirm={() => void handleDelete(node.id)}
                  >
                    <Button size="small" type="text" danger className="!text-xs !h-7 !px-2" icon={<Trash2 className="w-3.5 h-3.5" />} />
                  </Popconfirm>
                </div>
              </div>
              <div className="px-5 pb-4">{renderMetrics(node)}</div>
            </div>
          );
        })
      )}

      <Modal
        title={editingId ? '编辑云端节点' : '新建云端节点'}
        open={isModalOpen}
        onOk={() => void handleSave()}
        onCancel={() => setIsModalOpen(false)}
        okText="保存"
        cancelText="取消"
        width={600}
        destroyOnHidden
      >
        <Form form={form} layout="vertical" initialValues={DEFAULT_FORM} className="!pt-1">
          <div className="mb-4 rounded-xl border border-indigo-100 bg-indigo-50/40 p-3">
            <div className="text-[11px] font-semibold text-indigo-700 mb-1.5">粘贴 AutoDL SSH 命令</div>
            <Input.TextArea
              value={sshPaste}
              rows={3}
              placeholder="ssh -p 27045 root@connect.bjb2.seetacloud.com"
              className="!rounded-lg"
              onChange={(e) => applySshPaste(e.target.value)}
              onPaste={(e) => {
                const text = e.clipboardData.getData('text');
                if (text) {
                  window.setTimeout(() => applySshPaste(text, { notify: true }), 0);
                }
              }}
            />
            <p className="mt-1.5 mb-0 text-[11px] text-slate-500">自动解析地址、端口、用户；密码只保存在服务端，编辑时留空表示不改。</p>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <Form.Item name="name" label="显示名称" rules={[{ required: true, message: '请输入名称' }]}>
              <Input
                placeholder="如 AutoDL 4090"
                className="!h-8 !rounded-lg"
                onChange={(e) => {
                  if (!editingId && !idTouched) {
                    form.setFieldValue('id', suggestAutodlNodeId(e.target.value || form.getFieldValue('host') || ''));
                  }
                }}
              />
            </Form.Item>
            <Form.Item
              name="id"
              label="调度 ID"
              extra="须以 autodl 开头"
              rules={[{ required: !editingId, message: '请填写节点 ID' }]}
            >
              <Input
                disabled={!!editingId}
                placeholder="autodl-rtx4090"
                className="!h-8 !rounded-lg"
                onChange={() => setIdTouched(true)}
              />
            </Form.Item>
          </div>
          <div className="grid grid-cols-3 gap-3">
            <Form.Item name="host" label="主机" className="col-span-2" rules={[{ required: true, message: '请输入主机' }]}>
              <Input placeholder="connect.xxx.seetacloud.com" className="!h-8 !rounded-lg" />
            </Form.Item>
            <Form.Item name="port" label="端口" rules={[{ required: true, message: '请输入端口' }]}>
              <InputNumber min={1} max={65535} className="!w-full !h-8 !rounded-lg" placeholder="22" />
            </Form.Item>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <Form.Item name="user" label="用户" rules={[{ required: true, message: '请输入用户' }]}>
              <Input placeholder="root" className="!h-8 !rounded-lg" />
            </Form.Item>
            <Form.Item name="exec_mode" label="执行模式">
              <Select
                className="[&_.ant-select-selector]:!h-8 [&_.ant-select-selector]:!rounded-lg [&_.ant-select-selector]:!items-center"
                options={[
                  { value: 'native_python', label: '免 Docker（推荐）' },
                  { value: 'ssh_docker', label: '远端 Docker' },
                ]}
              />
            </Form.Item>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <Form.Item name="ssh_password" label="SSH 密码" extra={editingId ? '留空保持原值' : undefined}>
              <Input.Password placeholder="密码或留空" className="!h-8 !rounded-lg" />
            </Form.Item>
            <Form.Item name="ssh_key" label="密钥路径" extra="一般留空，用密码即可">
              <Input placeholder="可选" className="!h-8 !rounded-lg" />
            </Form.Item>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <Form.Item name="work_dir" label="工作目录">
              <Input placeholder="/root/workspace" className="!h-8 !rounded-lg" />
            </Form.Item>
            <Form.Item name="gpus" label="GPU">
              <Select
                className="[&_.ant-select-selector]:!h-8 [&_.ant-select-selector]:!rounded-lg [&_.ant-select-selector]:!items-center"
                options={[
                  { value: 'all', label: '全部 GPU' },
                  { value: '0', label: '仅 CPU' },
                  { value: '1', label: '1 块 GPU' },
                  { value: '2', label: '2 块 GPU' },
                ]}
              />
            </Form.Item>
          </div>
          <Form.Item name="quantdb_dir" label="QuantDB 数据目录" extra="写数据盘，实例重启不丢">
            <Input placeholder="/root/autodl-fs/quantdb" className="!h-8 !rounded-lg" />
          </Form.Item>
          {execMode === 'ssh_docker' && (
            <Form.Item name="docker_image" label="训练镜像">
              <Input placeholder="quantmind-train:latest" className="!h-8 !rounded-lg" />
            </Form.Item>
          )}
        </Form>
      </Modal>
    </div>
  );
};

export default CloudNodeSettings;
