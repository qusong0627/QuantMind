import React, { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
    Alert, Button, Card, Checkbox, Col, Descriptions, Input, Modal, Progress,
    Row, Space, Statistic, Table, Tag, Tooltip, Typography, message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import {
    ApiOutlined, CheckCircleFilled, CloseCircleFilled, CloudDownloadOutlined,
    DatabaseOutlined, FileSearchOutlined, KeyOutlined, ReloadOutlined,
    StopOutlined,
} from '@ant-design/icons';
import {
    dataPlatformService, QuantDBDataset, QuantDBLocalScanJob,
    QuantDBLocalScanPreflight, QuantDBModelScopeInitJob,
    QuantDBModelScopePreflight,
} from '../services/dataPlatformService';
import { QuantDBCatalogPanel } from './quantdb/QuantDBCatalogPanel';
import { QuantDBPreviewDrawer } from './quantdb/QuantDBPreviewDrawer';
import { describeError, httpStatusOf } from './quantdb/utils';
import { SyncSchedulePanel } from './data-management/SyncSchedulePanel';
import { TdxAiDataPanel } from './data-management/TdxAiDataPanel';

const { Text } = Typography;

const USAGE_WARN_PERCENT = 70;
const USAGE_DANGER_PERCENT = 90;
const LOW_QUOTA_GB = 5;

interface QuantDBInfo {
    installed: boolean;
    api_key_configured: boolean;
    connected: boolean;
    version?: string;
    account?: { username: string; email: string };
    usage?: {
        used_gb: number;
        limit_gb: number;
        remaining_gb: number;
        credit_gb?: number;
        subscription?: { status: string };
    };
    error?: string;
}

export const AdminQuantDBPanel: React.FC = () => {
    const navigate = useNavigate();
    const [loading, setLoading] = useState(false);
    const [info, setInfo] = useState<QuantDBInfo | null>(null);
    const [previewDataset, setPreviewDataset] = useState<QuantDBDataset | null>(null);
    const [catalogRefreshSignal, setCatalogRefreshSignal] = useState(0);
    const refreshCounter = useRef(0);
    const bumpCatalogRefresh = useCallback(() => {
        refreshCounter.current += 1;
        setCatalogRefreshSignal(refreshCounter.current);
    }, []);
    const [sources, setSources] = useState<Array<{ source: string; label: string; enabled: boolean }>>([]);
    const [sourcesLoading, setSourcesLoading] = useState(false);
    const [scanOpen, setScanOpen] = useState(false);
    const [initOpen, setInitOpen] = useState(false);

    const loadInfo = useCallback(async () => {
        setLoading(true);
        try {
            const resp = await dataPlatformService.getQuantDBInfo();
            setInfo(resp.quantdb);
        } catch (error: unknown) {
            message.error(`获取 QuantDB 状态失败: ${describeError(error)}`);
        } finally {
            setLoading(false);
        }
    }, []);

    const loadSources = useCallback(async () => {
        setSourcesLoading(true);
        try {
            const resp = await dataPlatformService.getMarketDataSources('quantdb');
            setSources(resp.sources);
        } catch (error: unknown) {
            message.error(`加载数据源配置失败: ${describeError(error)}`);
        } finally {
            setSourcesLoading(false);
        }
    }, []);

    const saveSources = useCallback(async (source: string, enabled: boolean) => {
        const next = sources.map((s) => (s.source === source ? { ...s, enabled } : s));
        setSources(next);
        try {
            const payload: Record<string, boolean> = {};
            next.forEach((s) => { payload[s.source] = s.enabled; });
            await dataPlatformService.saveMarketDataSources('quantdb', payload);
            message.success('A股数据源配置已保存');
        } catch (error: unknown) {
            message.error(`保存数据源配置失败: ${describeError(error)}`);
            loadSources();
        }
    }, [sources, loadSources]);

    useEffect(() => {
        loadSources();
    }, [loadSources]);

    useEffect(() => {
        loadInfo();
    }, [loadInfo]);

    const usagePercent = info?.usage && info.usage.limit_gb > 0
        ? Math.round((info.usage.used_gb / info.usage.limit_gb) * 100)
        : 0;

    return (
        <div className="space-y-4">
            {/* QuantDB SDK / 账号状态卡片 */}
            <Card
                size="small"
                title={
                    <Space>
                        <DatabaseOutlined />
                        <span>QuantDB 云端直供状态 (A股)</span>
                        <Tag color={info?.connected ? 'green' : 'red'}>
                            {info?.connected ? '已连接' : '未连接'}
                        </Tag>
                    </Space>
                }
                extra={
                    <Space size="middle">
                        <Tooltip title="从魔搭（ModelScope）公开数据集一键拉取 QuantDB 全量数据并覆盖本地数据目录，无需 QuantDB API Key / 流量；全量约 56GB，预计 1-3 小时，可稍后回来查看进度。仓库：https://www.modelscope.cn/datasets/qusong0627/LightGBM_Alpha300">
                            <Button
                                type="primary"
                                size="large"
                                icon={<CloudDownloadOutlined />}
                                style={{ height: 40, padding: '0 18px', fontWeight: 600 }}
                                onClick={() => setInitOpen(true)}
                            >
                                初始化数据
                            </Button>
                        </Tooltip>
                        <Tooltip title="扫描本地离线数据并建立 SQLite 同步状态库，配置 API 后首次同步即增量，避免全量重拉">
                            <Button
                                size="large"
                                icon={<FileSearchOutlined />}
                                style={{ height: 40, padding: '0 18px', fontWeight: 500 }}
                                onClick={() => setScanOpen(true)}
                            >
                                本地扫描
                            </Button>
                        </Tooltip>
                        <Button
                            size="large"
                            icon={<ReloadOutlined />}
                            style={{ height: 40, padding: '0 18px', fontWeight: 500 }}
                            onClick={() => { loadInfo(); loadSources(); }}
                            loading={loading}
                        >
                            刷新
                        </Button>
                    </Space>
                }
            >
                {info?.error && <Alert type="error" message={info.error} className="mb-4" showIcon />}

                <Row gutter={16}>
                    <Col span={6}>
                        <Statistic
                            title="SDK 状态"
                            value={info?.installed ? `已安装${info.version ? ` v${info.version}` : ''}` : '未安装'}
                            prefix={info?.installed
                                ? <CheckCircleFilled style={{ color: '#52c41a' }} />
                                : <CloseCircleFilled style={{ color: '#ff4d4f' }} />}
                            valueStyle={{ fontSize: 16 }}
                        />
                    </Col>
                    <Col span={6}>
                        <Statistic
                            title="API Key"
                            value={info?.api_key_configured ? '已配置' : '未配置'}
                            prefix={<ApiOutlined />}
                            valueStyle={{ fontSize: 16, color: info?.api_key_configured ? '#52c41a' : '#ff4d4f' }}
                        />
                    </Col>
                    <Col span={6}>
                        <Statistic
                            title="已用流量"
                            value={info?.usage?.used_gb?.toFixed(2) ?? '-'}
                            suffix="GB"
                            prefix={<DatabaseOutlined />}
                            valueStyle={{ fontSize: 16 }}
                        />
                    </Col>
                    <Col span={6}>
                        <Statistic
                            title="剩余流量"
                            value={info?.usage?.remaining_gb?.toFixed(2) ?? '-'}
                            suffix="GB"
                            valueStyle={{
                                fontSize: 16,
                                color: (info?.usage?.remaining_gb ?? 0) < LOW_QUOTA_GB ? '#ff4d4f' : '#52c41a',
                            }}
                        />
                    </Col>
                </Row>

                {info?.usage && (
                    <div className="mt-4">
                        <Progress
                            percent={usagePercent}
                            status={usagePercent > USAGE_DANGER_PERCENT
                                ? 'exception'
                                : usagePercent > USAGE_WARN_PERCENT ? 'active' : 'normal'}
                            format={() => `${info.usage!.used_gb.toFixed(1)} / ${info.usage!.limit_gb} GB`}
                        />
                        <div className="flex gap-4 mt-2">
                            {info.usage.subscription && (
                                <Tag color="blue">订阅: {info.usage.subscription.status}</Tag>
                            )}
                            {info.usage.credit_gb !== undefined && info.usage.credit_gb > 0 && (
                                <Tag color="green">赠送: {info.usage.credit_gb} GB</Tag>
                            )}
                        </div>
                    </div>
                )}

                {info?.account && (
                    <Descriptions size="small" column={2} className="mt-4">
                        <Descriptions.Item label="用户名">{info.account.username}</Descriptions.Item>
                        <Descriptions.Item label="邮箱">{info.account.email}</Descriptions.Item>
                    </Descriptions>
                )}
            </Card>

            {/* 数据源勾选配置 */}
            <div className="p-3 bg-gray-50 rounded">
                <Space direction="vertical" className="w-full" size="small">
                    <Space>
                        <DatabaseOutlined />
                        <Text strong>数据源</Text>
                        <Text type="secondary" className="text-xs">默认 QuantDB A股/akshare/北向/南向；雅虎默认关闭不勾选</Text>
                    </Space>
                    <Space wrap size="small">
                        {sources.map((s) => (
                            <Checkbox
                                key={s.source}
                                checked={s.enabled}
                                disabled={sourcesLoading}
                                onChange={(e) => saveSources(s.source, e.target.checked)}
                            >
                                <Text className="text-xs">{s.label}</Text>
                                <Text type="secondary" className="text-xs">({s.source})</Text>
                            </Checkbox>
                        ))}
                    </Space>
                </Space>
            </div>

            {/* API Key 状态与个人中心设置入口 */}
            <div className="flex items-center justify-between bg-white border border-slate-200 rounded-xl px-4 py-2.5 shadow-2xs">
                <Space size="middle">
                    <KeyOutlined className="text-blue-500" />
                    <Text className="text-xs font-semibold">API Key 授权状态:</Text>
                    <Tag
                        color={info?.api_key_configured ? 'green' : 'red'}
                        icon={<ApiOutlined />}
                        className="m-0"
                    >
                        {info?.api_key_configured ? '已授权配置' : '未配置密钥'}
                    </Tag>
                    {info?.account?.username && (
                        <Text type="secondary" className="text-xs">
                            账户: <Text code>{info.account.username}</Text>
                        </Text>
                    )}
                </Space>
                <Button
                    type="link"
                    size="small"
                    className="text-xs text-blue-600 hover:text-blue-700 p-0 font-medium"
                    onClick={() => navigate('/admin/profile?tab=data-platform')}
                >
                    前往「个人中心 - 数据平台」绑定或更新密钥 →
                </Button>
            </div>

            {/* 定时同步调度面板（建议次日 00:00 以后按需错峰，具体时间以前端设置为准） */}
            <SyncSchedulePanel market="A" defaultDays={5} />

            {/* TdxAiData 实时数据源（P6：订阅推送=盘中实时主源；请求接口=低频补充） */}
            <TdxAiDataPanel />

            {/* 自定义数据集重建调度（筛选保留 273 因子五库合并 → /data/quantcustom；
                依赖 A 股同步先把源数据落盘，建议排在 A 股之后如 03:00） */}
            <SyncSchedulePanel
                market="CUSTOM"
                title="自定义数据集重建（因子筛选保留集五库合并，建议 03:00、排在 A 股同步之后）"
                hint="重建 /data/quantcustom 合并数据集（模型直读训练的数据源）：增量模式只补缺失分区，筛选集或参数变化时自动全量重建。数据源为 A 股 QuantDB，须先等 A 股同步完成。"
                runLabel="立即重建一次"
                hideWindowFields
            />

            {/* QuantDB 数据集目录与详情 */}
            <QuantDBCatalogPanel
                connected={Boolean(info?.connected)}
                apiKeyConfigured={Boolean(info?.api_key_configured)}
                onPreview={setPreviewDataset}
                refreshSignal={catalogRefreshSignal}
            />

            {/* 数据集抽屉预览；抽屉内增量同步完成后刷新目录统计 */}
            <QuantDBPreviewDrawer
                dataset={previewDataset}
                onClose={() => setPreviewDataset(null)}
                onSynced={bumpCatalogRefresh}
            />

            {/* 本地扫描：离线数据 → SQLite 同步状态库 */}
            <LocalScanModal
                open={scanOpen}
                onClose={() => setScanOpen(false)}
                onCompleted={bumpCatalogRefresh}
            />

            {/* 初始化数据：魔搭 ModelScope → 覆盖本地数据目录 */}
            <ModelScopeInitModal
                open={initOpen}
                onClose={() => setInitOpen(false)}
                onCompleted={bumpCatalogRefresh}
            />
        </div>
    );
};

// ---------------------------------------------------------------------------
// 本地扫描弹窗：预检 → 选择数据集 → 后台扫描 → 进度/结果
// ---------------------------------------------------------------------------
const SCAN_JOB_POLL_INTERVAL_MS = 2000;

const formatBytes = (bytes: number): string => {
    if (!bytes || bytes <= 0) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let v = bytes;
    let i = 0;
    while (v >= 1024 && i < units.length - 1) {
        v /= 1024;
        i += 1;
    }
    return `${v.toFixed(v >= 100 || i === 0 ? 0 : 1)} ${units[i]}`;
};

interface LocalScanModalProps {
    open: boolean;
    onClose: () => void;
    onCompleted: () => void;
}

export const LocalScanModal: React.FC<LocalScanModalProps> = ({ open, onClose, onCompleted }) => {
    const [preflight, setPreflight] = useState<QuantDBLocalScanPreflight | null>(null);
    const [preflightLoading, setPreflightLoading] = useState(false);
    const [rootInput, setRootInput] = useState<string>('');
    const [selected, setSelected] = useState<string[]>([]);
    const [job, setJob] = useState<QuantDBLocalScanJob | null>(null);
    const [starting, setStarting] = useState(false);
    const [cancelling, setCancelling] = useState(false);

    const loadPreflight = useCallback(async (root?: string) => {
        setPreflightLoading(true);
        try {
            const data = await dataPlatformService.localScanPreflight(root || undefined);
            setPreflight(data);
            setRootInput(data.root);
            setSelected(data.datasets.map((d) => d.dataset));
        } catch (error: unknown) {
            message.error(`预检失败: ${describeError(error)}`);
        } finally {
            setPreflightLoading(false);
        }
    }, []);

    useEffect(() => {
        if (open) {
            setJob(null);
            loadPreflight();
        }
    }, [open, loadPreflight]);

    // 轮询扫描任务进度；完成/失败时提示并刷新目录统计
    useEffect(() => {
        if (!job || job.status !== 'running') return undefined;
        const timer = setInterval(async () => {
            try {
                const resp = await dataPlatformService.getQuantDBLocalScanJob(job.job_id);
                setJob(resp.job);
                if (resp.job.status === 'completed') {
                    message.success(`本地扫描完成：登记 ${resp.job.summary?.registered ?? 0} 个文件`);
                    onCompleted();
                } else if (resp.job.status === 'failed') {
                    message.error(`本地扫描失败: ${resp.job.error ?? '未知错误'}`);
                }
            } catch (error: unknown) {
                if (httpStatusOf(error) === 404) {
                    // 任务记录已不存在（服务重启/被清理）→ 停止轮询并回到预检
                    setJob(null);
                    loadPreflight();
                    message.warning('扫描任务记录已失效（服务可能重启过），已重新预检。');
                    return;
                }
                // 单次轮询失败忽略，下一轮重试
            }
        }, SCAN_JOB_POLL_INTERVAL_MS);
        return () => clearInterval(timer);
    }, [job, onCompleted]);

    const startScan = async () => {
        setStarting(true);
        try {
            const all = preflight?.datasets.map((d) => d.dataset) ?? [];
            const resp = await dataPlatformService.startQuantDBLocalScan({
                root: rootInput.trim() || undefined,
                datasets: selected.length === all.length ? undefined : selected,
            });
            setJob(resp.job);
            message.success('本地扫描已启动（后台执行）');
        } catch (error: unknown) {
            message.error(`启动扫描失败: ${describeError(error)}`);
        } finally {
            setStarting(false);
        }
    };

    const handleCancelJob = async () => {
        if (!job) return;
        setCancelling(true);
        try {
            await dataPlatformService.cancelQuantDBLocalScanJob(job.job_id);
        } catch (error: unknown) {
            message.error(`取消失败: ${describeError(error)}`);
        } finally {
            setCancelling(false);
        }
    };

    const isRunning = job?.status === 'running';
    const percent = job && job.total > 0 ? Math.round((job.done / job.total) * 100) : 0;

    const datasetColumns: ColumnsType<QuantDBLocalScanPreflight['datasets'][number]> = [
        { title: '数据集', dataIndex: 'name', width: 130 },
        {
            title: '标识',
            dataIndex: 'dataset',
            width: 160,
            render: (v: string) => <Text code className="text-xs">{v}</Text>,
        },
        {
            title: '落盘形态',
            dataIndex: 'layout',
            width: 90,
            render: (v: string) => <Tag>{v}</Tag>,
        },
        { title: '本地目录', dataIndex: 'rel_dir', ellipsis: true },
        { title: '文件数', dataIndex: 'files', width: 90, align: 'right', render: (v: number) => v.toLocaleString() },
        { title: '大小', dataIndex: 'bytes', width: 90, align: 'right', render: (v: number) => formatBytes(v) },
    ];

    return (
        <Modal
            title="本地扫描 — 建立离线数据同步状态库"
            open={open}
            onCancel={onClose}
            width={860}
            destroyOnHidden
            footer={
                <Space>
                    {isRunning && (
                        <Button danger icon={<StopOutlined />} loading={cancelling} onClick={handleCancelJob}>
                            取消扫描
                        </Button>
                    )}
                    <Button
                        type="primary"
                        icon={<FileSearchOutlined />}
                        loading={starting}
                        disabled={isRunning || selected.length === 0 || (Boolean(preflight) && !preflight?.exists)}
                        onClick={startScan}
                    >
                        开始扫描
                    </Button>
                    <Button onClick={onClose}>关闭</Button>
                </Space>
            }
        >
            <Space direction="vertical" className="w-full" size="middle">
                <Alert
                    type="info"
                    showIcon
                    message="扫描本地已有的 QuantDB 离线数据（网盘包 / 归档），把 md5/sha256 登记进 SQLite 同步状态库。之后配置 QuantDB API key 首次同步即走增量 fast-path，只下载缺失分区，避免全量重拉。"
                />

                {preflight?.warnings.map((w, i) => (
                    <Alert key={i} type="warning" showIcon message={w} />
                ))}

                {/* 数据目录 + 预检 */}
                <div className="flex gap-2 items-center">
                    <Input
                        value={rootInput}
                        onChange={(e) => setRootInput(e.target.value)}
                        placeholder="服务器上的数据根目录，如 /data/quantdb"
                    />
                    <Button
                        icon={<ReloadOutlined />}
                        loading={preflightLoading}
                        onClick={() => loadPreflight(rootInput.trim() || undefined)}
                    >
                        预检
                    </Button>
                </div>

                {preflight && (
                    <Row gutter={16}>
                        <Col span={8}>
                            <Statistic title="本地文件" value={preflight.total_files} />
                        </Col>
                        <Col span={8}>
                            <Statistic title="离线数据总量" value={formatBytes(preflight.total_bytes)} />
                        </Col>
                        <Col span={8}>
                            <Statistic
                                title="状态库已登记"
                                value={preflight.state.quantmind_objects}
                                valueStyle={{ color: preflight.state.quantmind_objects > 0 ? '#52c41a' : '#faad14' }}
                            />
                        </Col>
                    </Row>
                )}

                {preflight && (
                    <div className="text-xs text-slate-400 break-all">
                        状态库：<Text code>{preflight.state.quantmind_path}</Text>
                    </div>
                )}

                {/* 数据集选择 */}
                {!isRunning && preflight && (
                    <Table
                        size="small"
                        loading={preflightLoading}
                        rowKey="dataset"
                        dataSource={preflight.datasets}
                        columns={datasetColumns}
                        pagination={false}
                        scroll={{ y: 280 }}
                        rowSelection={{
                            selectedRowKeys: selected,
                            onChange: (keys) => setSelected(keys as string[]),
                        }}
                    />
                )}

                {/* 扫描进度 / 结果 */}
                {job && (
                    <div className="p-3 bg-gray-50 rounded space-y-2">
                        <Space wrap>
                            <Text strong>{job.job_id}</Text>
                            <Tag
                                color={
                                    job.status === 'completed' ? 'green'
                                        : job.status === 'failed' ? 'red'
                                            : job.status === 'cancelled' || job.status === 'cancelling' ? 'orange'
                                                : 'blue'
                                }
                            >
                                {job.status === 'running' ? '扫描中' : job.status === 'completed' ? '已完成'
                                    : job.status === 'failed' ? '失败' : '已取消'}
                            </Tag>
                            {job.current && <Text type="secondary" className="text-xs">{job.current}</Text>}
                        </Space>
                        <Progress percent={percent} status={job.status === 'failed' ? 'exception' : 'active'} />
                        {job.status === 'completed' && job.summary && (
                            <>
                                <Space wrap>
                                    <Tag color="green">登记 {job.summary.registered.toLocaleString()}</Tag>
                                    <Tag>复用 {job.summary.reused.toLocaleString()}</Tag>
                                    <Tag color={job.summary.invalid_files ? 'red' : 'default'}>
                                        无效 {job.summary.invalid_files}
                                    </Tag>
                                    <Tag>{formatBytes(job.summary.total_bytes)}</Tag>
                                    <Tag>耗时 {job.summary.elapsed_sec}s</Tag>
                                </Space>
                                {job.summary.warnings?.map((w, i) => (
                                    <Alert key={i} type="warning" showIcon message={w} className="mt-2" />
                                ))}
                                <div className="text-xs text-slate-400 mt-1 break-all">
                                    {Object.entries(job.summary.state_dbs).map(([k, v]) => (
                                        <div key={k}>{k}: <Text code>{v}</Text></div>
                                    ))}
                                </div>
                            </>
                        )}
                        {job.status === 'failed' && (
                            <Alert type="error" showIcon message={job.error ?? '未知错误'} />
                        )}
                    </div>
                )}
            </Space>
        </Modal>
    );
};

// ---------------------------------------------------------------------------
// 初始化数据弹窗：魔搭预检 → 选择数据集/模式 → 后台拉取 → 进度/结果
// ---------------------------------------------------------------------------
const INIT_JOB_POLL_INTERVAL_MS = 2000;

interface ModelScopeInitModalProps {
    open: boolean;
    onClose: () => void;
    onCompleted: () => void;
}

export const ModelScopeInitModal: React.FC<ModelScopeInitModalProps> = ({ open, onClose, onCompleted }) => {
    const [preflight, setPreflight] = useState<QuantDBModelScopePreflight | null>(null);
    const [preflightLoading, setPreflightLoading] = useState(false);
    const [selected, setSelected] = useState<string[]>([]);
    const [job, setJob] = useState<QuantDBModelScopeInitJob | null>(null);
    const [starting, setStarting] = useState(false);
    const [cancelling, setCancelling] = useState(false);

    const loadPreflight = useCallback(async () => {
        setPreflightLoading(true);
        try {
            const data = await dataPlatformService.modelscopePreflight();
            setPreflight(data);
            setSelected(data.datasets.map((d) => d.dataset));
        } catch (error: unknown) {
            message.error(`预检失败: ${describeError(error)}`);
        } finally {
            setPreflightLoading(false);
        }
    }, []);

    // 打开弹窗时：若后台已有运行中的任务，直接回到下载进度页；否则做预检
    useEffect(() => {
        if (!open) return undefined;
        let alive = true;
        (async () => {
            try {
                const resp = await dataPlatformService.listModelScopeInitJobs();
                const active = resp.jobs
                    .filter((j) => j.status === 'running')
                    .sort((a, b) => (a.started_at < b.started_at ? 1 : -1))[0];
                if (!alive) return;
                if (active) {
                    setJob(active);
                    return;
                }
            } catch {
                // 忽略：回退到正常预检
            }
            if (!alive) return;
            setJob(null);
            loadPreflight();
        })();
        return () => {
            alive = false;
        };
    }, [open, loadPreflight]);

    // 轮询初始化任务进度；结束后回到预检（可再次发起）并刷新目录统计
    useEffect(() => {
        if (!job || job.status !== 'running') return undefined;
        const timer = setInterval(async () => {
            try {
                const resp = await dataPlatformService.getModelScopeInitJob(job.job_id);
                setJob(resp.job);
                if (resp.job.status === 'completed') {
                    const s = resp.job.summary;
                    message.success(`初始化完成：下载 ${s?.downloaded ?? 0}，跳过 ${s?.skipped ?? 0}，失败 ${s?.errors ?? 0}`);
                    onCompleted();
                    loadPreflight();
                } else if (resp.job.status === 'failed') {
                    message.error(`初始化失败: ${resp.job.error ?? '未知错误'}`);
                    loadPreflight();
                } else if (resp.job.status === 'cancelled') {
                    message.warning('初始化已取消');
                    loadPreflight();
                }
            } catch (error: unknown) {
                if (httpStatusOf(error) === 404) {
                    // 任务记录已不存在（服务重启/被清理）→ 停止轮询，回到预检
                    setJob(null);
                    loadPreflight();
                    message.warning('后台任务记录已失效（服务可能重启过）。已下载的文件会保留，可再次发起续传。');
                    return;
                }
                // 单次轮询失败忽略，下一轮重试
            }
        }, INIT_JOB_POLL_INTERVAL_MS);
        return () => clearInterval(timer);
    }, [job, onCompleted, loadPreflight]);

    const doStart = async () => {
        setStarting(true);
        try {
            const all = preflight?.datasets.map((d) => d.dataset) ?? [];
            const resp = await dataPlatformService.startModelScopeInit({
                datasets: selected.length === all.length ? undefined : selected,
            });
            setJob(resp.job);
            message.success('初始化数据已启动（后台执行）');
        } catch (error: unknown) {
            message.error(`启动失败: ${describeError(error)}`);
        } finally {
            setStarting(false);
        }
    };

    const handleCancelJob = async () => {
        if (!job) return;
        setCancelling(true);
        try {
            await dataPlatformService.cancelModelScopeInitJob(job.job_id);
        } catch (error: unknown) {
            message.error(`取消失败: ${describeError(error)}`);
        } finally {
            setCancelling(false);
        }
    };

    const isRunning = job?.status === 'running';
    const percent = job && job.bytes_total > 0
        ? Math.min(100, Math.round((job.bytes_done / job.bytes_total) * 100))
        : job && job.total > 0 ? Math.round((job.done / job.total) * 100) : 0;
    const repoUrl = preflight?.repo_url
        ?? `https://www.modelscope.cn/datasets/${preflight?.repo_id ?? 'qusong0627/LightGBM_Alpha300'}`;

    const columns: ColumnsType<QuantDBModelScopePreflight['datasets'][number]> = [
        { title: '数据集', dataIndex: 'name', width: 130 },
        {
            title: '标识',
            dataIndex: 'dataset',
            width: 160,
            render: (v: string) => <Text code className="text-xs">{v}</Text>,
        },
        {
            title: '落盘形态',
            dataIndex: 'layout',
            width: 90,
            render: (v: string) => <Tag>{v}</Tag>,
        },
        { title: '远端目录', dataIndex: 'rel_dir', ellipsis: true },
        { title: '文件数', dataIndex: 'files', width: 90, align: 'right', render: (v: number) => v.toLocaleString() },
        { title: '大小', dataIndex: 'bytes', width: 90, align: 'right', render: (v: number) => formatBytes(v) },
    ];

    return (
        <Modal
            title="初始化数据 — 从魔搭覆盖本地 QuantDB 数据目录"
            open={open}
            onCancel={onClose}
            width={880}
            centered
            destroyOnHidden
            style={{ paddingBottom: 120 }}
            styles={{
                body: {
                    maxHeight: 'calc(var(--app-h) - 280px)',
                    overflowY: 'auto',
                    paddingBottom: 16,
                },
            }}
            footer={
                <Space>
                    {isRunning && (
                        <Button danger icon={<StopOutlined />} loading={cancelling} onClick={handleCancelJob}>
                            取消
                        </Button>
                    )}
                    <Button
                        type="primary"
                        icon={<CloudDownloadOutlined />}
                        loading={starting}
                        disabled={isRunning || selected.length === 0 || preflightLoading}
                        onClick={doStart}
                    >
                        开始拉取
                    </Button>
                    <Button onClick={onClose}>关闭</Button>
                </Space>
            }
        >
            <Space direction="vertical" className="w-full" size="middle">
                <Alert
                    type="info"
                    showIcon
                    message={
                        <span>
                            从魔搭公开数据集仓库拉取 QuantDB A股数据并覆盖本地数据目录（免 QuantDB API Key / 流量）。逐文件校验 sha256 后原地覆盖；已完整下载的文件自动跳过，支持断点续传。仓库：
                            <a href={repoUrl} target="_blank" rel="noreferrer">{repoUrl}</a>
                        </span>
                    }
                />

                <Alert
                    type="warning"
                    showIcon
                    message="首次全量同步约需 1-3 小时，请耐心等待，您可稍后回来查看"
                    description="数据总量约 56GB，下载在后台执行。启动后可以关闭本窗口或离开页面，稍后回来查看进度；已完整下载的文件会自动跳过，中断后重新发起可断点续传。"
                />

                {preflight?.warnings.map((w, i) => (
                    <Alert key={i} type="warning" showIcon message={w} />
                ))}

                {preflight && (
                    <Row gutter={12}>
                        <Col flex="1">
                            <Statistic title="远端文件" value={preflight.total_files} valueStyle={{ fontSize: 18 }} />
                        </Col>
                        <Col flex="1">
                            <Statistic title="远端总量" value={formatBytes(preflight.total_bytes)} valueStyle={{ fontSize: 18 }} />
                        </Col>
                        <Col flex="1">
                            <Statistic
                                title="已就绪(将跳过)"
                                value={formatBytes(preflight.skip_bytes)}
                                valueStyle={{ fontSize: 18, color: '#52c41a' }}
                            />
                        </Col>
                        <Col flex="1">
                            <Statistic
                                title="将原地覆盖"
                                value={formatBytes(preflight.changed_bytes)}
                                valueStyle={{ fontSize: 18, color: '#fa8c16' }}
                            />
                        </Col>
                        <Col flex="1">
                            <Statistic
                                title="需新增空间"
                                value={formatBytes(preflight.missing_bytes)}
                                valueStyle={{ fontSize: 18, color: preflight.missing_bytes > 0 ? '#1677ff' : '#52c41a' }}
                            />
                        </Col>
                        <Col flex="1">
                            <Statistic title="目录可用" value={formatBytes(preflight.disk.free)} valueStyle={{ fontSize: 18 }} />
                        </Col>
                    </Row>
                )}

                {preflight && (
                    <div className="text-[11px] text-slate-400 leading-5">
                        <Text type="secondary" className="text-[11px]">
                            已就绪 = 大小与 sha256 一致（跳过 {preflight.skip_files.toLocaleString()} 个文件）；
                            将原地覆盖 = 本地已有旧版本，替换不占净增空间；需新增空间 = 本地缺失。
                        </Text>
                    </div>
                )}

                {preflight && (
                    <div className="text-xs text-slate-400 break-all">
                        目标目录：<Text code>{preflight.root}</Text>（
                        <Text code>QM_QUANTDB_DATA_DIR</Text>）· 共 {preflight.datasets.length} 个数据集
                    </div>
                )}

                {/* 数据集选择 */}
                {!isRunning && (
                    <Table
                        size="small"
                        loading={preflightLoading}
                        rowKey="dataset"
                        dataSource={preflight?.datasets ?? []}
                        columns={columns}
                        pagination={false}
                        scroll={{ y: 260 }}
                        rowSelection={{
                            selectedRowKeys: selected,
                            onChange: (keys) => setSelected(keys as string[]),
                        }}
                    />
                )}

                {/* 拉取进度 / 结果 */}
                {job && (
                    <div className="p-3 bg-gray-50 rounded space-y-2">
                        <Space wrap>
                            <Text strong>{job.job_id}</Text>
                            <Tag
                                color={
                                    job.status === 'completed' ? 'green'
                                        : job.status === 'failed' ? 'red'
                                            : job.status === 'cancelled' || job.status === 'cancelling' ? 'orange'
                                                : 'blue'
                                }
                            >
                                {job.status === 'running' ? '进行中' : job.status === 'completed' ? '已完成'
                                    : job.status === 'failed' ? '失败' : '已取消'}
                            </Tag>
                            <Tag>{job.stage}</Tag>
                            {job.current && <Text type="secondary" className="text-xs">{job.current}</Text>}
                        </Space>
                        <Progress percent={percent} status={job.status === 'failed' ? 'exception' : 'active'} />
                        <Text type="secondary" className="text-xs">
                            {(job.bytes_done / 1024 / 1024).toFixed(1)} MB / {(job.bytes_total / 1024 / 1024).toFixed(1)} MB
                            （数据集 {job.done}/{job.total}）
                        </Text>
                        {job.status === 'running' && (
                            <Text type="secondary" className="text-xs">
                                首次全量约需 1-3 小时，可关闭本窗口或离开页面，稍后回来查看进度。
                            </Text>
                        )}
                        {job.status === 'completed' && job.summary && (
                            <>
                                <Space wrap>
                                    <Tag color="green">下载 {job.summary.downloaded.toLocaleString()}</Tag>
                                    <Tag>跳过 {job.summary.skipped.toLocaleString()}</Tag>
                                    <Tag color={job.summary.errors ? 'red' : 'default'}>
                                        失败 {job.summary.errors}
                                    </Tag>
                                    <Tag>{formatBytes(job.summary.downloaded_bytes)}</Tag>
                                    <Tag>耗时 {job.summary.elapsed_sec}s</Tag>
                                </Space>
                                {job.summary.state?.state_dbs && (
                                    <div className="text-xs text-slate-400 mt-1 break-all">
                                        {Object.entries(job.summary.state.state_dbs).map(([k, v]) => (
                                            <div key={k}>{k}: <Text code>{v}</Text></div>
                                        ))}
                                    </div>
                                )}
                                {(job.summary.error_samples?.length ?? 0) > 0 && (
                                    <Alert
                                        type="warning"
                                        showIcon
                                        message={`部分文件失败（示例）`}
                                        description={
                                            <div className="text-xs">
                                                {job.summary.error_samples.slice(0, 5).map((s, i) => (
                                                    <div key={i}>{s}</div>
                                                ))}
                                            </div>
                                        }
                                    />
                                )}
                            </>
                        )}
                        {job.status === 'failed' && (
                            <Alert type="error" showIcon message={job.error ?? '未知错误'} />
                        )}
                    </div>
                )}
            </Space>
        </Modal>
    );
};

export default AdminQuantDBPanel;
