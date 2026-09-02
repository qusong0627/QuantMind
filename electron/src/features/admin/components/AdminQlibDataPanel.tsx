/** Qlib 数据管理（CN / HK / US / CRYPTO / FUTURES）：查看 Qlib 数据集状态，从本地 parquet 更新 / 重建 */

const QLIB_MARKETS = [
  { key: 'CN', label: 'A股' },
  { key: 'HK', label: '港股' },
  { key: 'US', label: '美股' },
  { key: 'CRYPTO', label: '区块链' },
  { key: 'FUTURES', label: '期货' },
];
import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Alert, Button, Card, Descriptions, Modal, Progress, Space, Table, Tag, Typography, message,
} from 'antd';
import {
  DatabaseOutlined, ReloadOutlined, SyncOutlined, StopOutlined,
} from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import { dataPlatformService } from '../services/dataPlatformService';
import type { QlibJob, QlibStatus } from '../services/dataPlatformService';

const { Title, Text } = Typography;

const RUNNING_STATUS = ['running', 'cancelling'];

const JOB_COLUMNS: ColumnsType<QlibJob> = [
  {
    title: '市场', key: 'market', width: 90,
    render: (_, r) => {
      const m = /qlib-\w+-(\w+)-20/.exec(r.job_id || '');
      return m ? <Tag color="blue">{m[1]}</Tag> : '—';
    },
  },
  { title: '任务', dataIndex: 'kind', width: 110, render: () => '本地重建 Qlib' },
  {
    title: '状态', dataIndex: 'status', width: 110,
    render: (v) => {
      const color = v === 'completed' ? 'green' : v === 'failed' ? 'red' : v === 'cancelling' ? 'orange' : v === 'running' ? 'processing' : 'default';
      const labels: Record<string, string> = { running: '执行中', completed: '完成', failed: '失败', cancelled: '已取消', cancelling: '取消中' };
      return <Tag color={color}>{labels[v] || v}</Tag>;
    },
  },
  {
    title: '进度', dataIndex: 'progress', width: 160,
    render: (v: number) => <Progress percent={v || 0} size="small" status={v >= 100 ? 'success' : undefined} />,
  },
  { title: '当前阶段', dataIndex: 'current', render: (v) => v || '—' },
  {
    title: '时间', key: 'time', width: 200,
    render: (_, r) => `${(r.started_at || '').replace('T', ' ').slice(0, 19)}${r.finished_at ? ` → ${r.finished_at.replace('T', ' ').slice(5, 19)}` : ''}`,
  },
];

export const AdminQlibDataPanel: React.FC = () => {
  const [market, setMarket] = useState('CN');
  const [status, setStatus] = useState<QlibStatus | null>(null);
  const [jobs, setJobs] = useState<QlibJob[]>([]);
  const [loading, setLoading] = useState(false);
  const [acting, setActing] = useState(false);

  const hasRunning = useMemo(() => jobs.some((j) => RUNNING_STATUS.includes(j.status)), [jobs]);
  const activeJobId = useMemo(() => jobs.find((j) => RUNNING_STATUS.includes(j.status))?.job_id, [jobs]);

  const loadStatus = useCallback(async (mk: string) => {
    try {
      setStatus(await dataPlatformService.getQlibStatus(mk));
    } catch (e: any) {
      const code = e?.code || e?.response?.status;
      // 网络瞬断（容器重启）静默退避，不刷 error toast
      if (code === 'ERR_NETWORK' || !e?.response) return;
      message.error(e?.response?.data?.detail || '加载 Qlib 状态失败');
    }
  }, []);

  const loadJobs = useCallback(async () => {
    try {
      const res = await dataPlatformService.listQlibJobs();
      setJobs(res.jobs || []);
    } catch (e: any) {
      const code = e?.code || e?.response?.status;
      if (code === 'ERR_NETWORK' || !e?.response) return;
      // 非网络错误才静默，避免 500 刷屏也忽略
    }
  }, []);

  const refresh = useCallback((mk: string) => {
    setLoading(true);
    Promise.all([loadStatus(mk), loadJobs()]).finally(() => setLoading(false));
  }, [loadStatus, loadJobs]);

  useEffect(() => {
    refresh(market);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [market]);

  // 有运行中任务时轮询进度（指数退避 + 页面不可见暂停）
  useEffect(() => {
    if (!hasRunning) return;
    let attempt = 0;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const poll = async () => {
      if (typeof document !== 'undefined' && document.hidden) {
        timer = setTimeout(poll, 5000);
        return;
      }
      try {
        await Promise.all([loadStatus(market), loadJobs()]);
        attempt = 0;
      } catch {
        attempt += 1;
      }
      const delay = Math.min(3000 * Math.pow(1.5, attempt), 15000) + Math.random() * 500;
      timer = setTimeout(poll, delay);
    };
    poll();
    return () => { if (timer) clearTimeout(timer); };
  }, [hasRunning, loadStatus, loadJobs]);

  const doUpdate = () => {
    if (acting) return;
    Modal.confirm({
      title: '更新 Qlib',
      content: '将从本地 parquet 增量重建 Qlib 缓存（本地 quantdb 已有独立同步流程，无需额外下载）。确定继续？',
      okText: '开始更新',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: async () => {
        setActing(true);
        try {
          await dataPlatformService.updateQlibFromSdk(market);
          message.success(`已提交 [${market}] Qlib 增量重建任务`);
          loadJobs();
        } catch (e: any) {
          message.error(e?.response?.data?.detail || '提交更新失败');
        } finally { setActing(false); }
      },
    });
  };

  const doCancel = (jobId: string) => {
    Modal.confirm({
      title: '取消任务',
      content: '确定取消该任务吗？已下载分区的成果会保留。',
      okText: '取消任务',
      okButtonProps: { danger: true },
      onOk: async () => {
        try {
          await dataPlatformService.cancelQlibJob(jobId);
          message.info('已提交取消请求');
          loadJobs();
        } catch (e: any) {
          message.error(e?.response?.data?.detail || '取消失败');
        }
      },
    });
  };

  const qlib = status?.qlib_data;
  const ins = qlib?.instruments;
  const ready = status?.ready;

  return (
    <div className="space-y-6">
      <div className="flex flex-col md:flex-row md:items-center justify-between gap-4 bg-white p-6 rounded-3xl border border-slate-100 shadow-sm">
        <div className="flex items-center gap-4">
          <div className="w-12 h-12 rounded-2xl bg-indigo-50 border border-indigo-100 flex items-center justify-center text-indigo-600 text-2xl shrink-0">
            <DatabaseOutlined />
          </div>
          <div>
            <div className="flex items-center gap-3">
              <Title level={4} className="!m-0 !font-black !text-slate-800 tracking-tight">Qlib 数据管理</Title>
              {status?.enabled === false && <Tag color="default" className="!m-0 rounded-full font-black">市场未启用</Tag>}
              {status?.enabled !== false && ready !== undefined && <Tag color={ready ? 'green' : 'red'} className="!m-0 rounded-full font-black">{ready ? 'Qlib 就绪' : '未就绪'}</Tag>}
              <div className="flex items-center gap-1 rounded-full bg-slate-100 p-0.5">
                {QLIB_MARKETS.map((m) => (
                  <button
                    key={m.key}
                    onClick={() => setMarket(m.key)}
                    className={`px-3 py-1 rounded-full text-[11px] font-extrabold transition-all ${
                      market === m.key ? 'bg-white text-indigo-700 shadow-2xs border border-indigo-200' : 'text-slate-500 hover:text-slate-800'
                    }`}
                  >
                    {m.label}
                  </button>
                ))}
              </div>
            </div>
            <Text className="text-slate-400 text-xs mt-1 block">
              查看当前系统 Qlib 数据集状态，从本地 parquet 增量重建缓存（支持五市场切换）。
            </Text>
          </div>
        </div>
        <Space>
          <Button icon={<ReloadOutlined />} loading={loading} onClick={() => refresh(market)}>刷新</Button>
        </Space>
      </div>

      <Card title="Qlib 数据集状态" className="rounded-3xl border-none shadow-xl shadow-slate-200/40 bg-white">
        {qlib ? (
          <Descriptions column={{ xs: 1, sm: 2, md: 3 }} size="small">
            <Descriptions.Item label="缓存目录"><Text code className="text-xs">{status?.qlib_dir}</Text></Descriptions.Item>
            <Descriptions.Item label="日历交易日数">{qlib.calendar_total_days ?? 0} 天</Descriptions.Item>
            <Descriptions.Item label="日历范围">
              {qlib.calendar_start_date || '--'} ～ {qlib.calendar_last_date || '--'}
            </Descriptions.Item>
            <Descriptions.Item label="标的数(Instruments)">{ins?.total ?? 0}</Descriptions.Item>
            <Descriptions.Item label="标的分布">
              <Space split={<Text type="secondary">/</Text>}>
                <span>SH {ins?.sh ?? 0}</span>
                <span>SZ {ins?.sz ?? 0}</span>
                <span>BJ {ins?.bj ?? 0}</span>
                <span>其他 {ins?.other ?? 0}</span>
              </Space>
            </Descriptions.Item>
            <Descriptions.Item label="特征标的数">{qlib.feature_dirs_total ?? 0} 个</Descriptions.Item>
            <Descriptions.Item label="上游 parquet 最新">{status?.parquet_latest_date ? `${status.parquet_latest_date.slice(0,4)}-${status.parquet_latest_date.slice(4,6)}-${status.parquet_latest_date.slice(6)}` : '—'}</Descriptions.Item>
            <Descriptions.Item label="对齐情况" span={2}>
              {status?.lag_hint ? (
                status.lag_days && status.lag_days > 0
                  ? <Tag color="orange">{status.lag_hint}</Tag>
                  : <Tag color="green">{status.lag_hint}</Tag>
              ) : <Text type="secondary">—</Text>}
            </Descriptions.Item>
          </Descriptions>
        ) : (
          <div className="py-8 text-center text-slate-400">暂无 Qlib 状态信息，请刷新</div>
        )}
      </Card>

      <Card title="操作" className="rounded-3xl border-none shadow-xl shadow-slate-200/40 bg-white">
        <Space size="large" wrap>
          <Button icon={<SyncOutlined />} danger loading={acting} disabled={!!activeJobId} onClick={doUpdate}>
            更新 Qlib（本地 parquet 重建）
          </Button>
          <Text type="secondary" className="text-xs">从本地 quantdb parquet 增量重建，无需额外 SDK 下载。</Text>
        </Space>
        {activeJobId && <Alert className="mt-4" type="info" showIcon message="有任务正在执行，请勿重复提交（预估耗时 10-20分钟）" description="任务完成后自动刷新状态。" />}
      </Card>

      <Card
        title="任务记录"
        extra={activeJobId && <Button size="small" icon={<StopOutlined />} onClick={() => doCancel(activeJobId)}>取消当前任务</Button>}
        className="rounded-3xl border-none shadow-xl shadow-slate-200/40 bg-white"
      >
        <Table rowKey="job_id" size="small" columns={JOB_COLUMNS} dataSource={jobs} pagination={{ pageSize: 10, hideOnSinglePage: true }} />
      </Card>
    </div>
  );
};

export default AdminQlibDataPanel;