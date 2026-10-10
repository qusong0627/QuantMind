import React, { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
    Alert, Button, Checkbox, Col, Input, Modal, Progress,
    Row, Space, Statistic, Table, Tag, Tooltip, Typography, message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import {
    ApiOutlined, CheckCircleFilled, CloseCircleFilled, CloudDownloadOutlined,
    DatabaseOutlined, FileSearchOutlined, GlobalOutlined, ReloadOutlined,
    SettingOutlined, StopOutlined, CloudSyncOutlined,
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
import { SectionCard } from './data-management/SectionCard';

const { Text } = Typography;

const USAGE_WARN_PERCENT = 70;
const USAGE_DANGER_PERCENT = 90;
const LOW_QUOTA_GB = 5;
const QUANTDB_REGISTER_URL = 'https://www.quantdb.cn/index.html';

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
        // SDK 0.4.4+ 拆分：套餐额度（月度重置）/ 流量包额度（一年有效可叠加）
        total_gb?: number;
        plan_used_gb?: number;
        plan_total_gb?: number;
        plan_remaining_gb?: number;
        package_used_gb?: number;
        package_total_gb?: number;
        package_remaining_gb?: number;
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

    const usage = info?.usage;
    const hasSplit = usage !== undefined
        && usage.total_gb !== undefined && usage.total_gb > 0
        && usage.plan_total_gb !== undefined;
    const totalGb = hasSplit ? (usage!.total_gb as number) : (usage?.limit_gb ?? 0);
    const usagePercent = usage && totalGb > 0
        ? Math.round((usage.used_gb / totalGb) * 100)
        : 0;
    const planPercent = hasSplit && (usage!.plan_total_gb as number) > 0
        ? Math.round(((usage!.plan_used_gb ?? 0) / (usage!.plan_total_gb as number)) * 100)
        : 0;
    const packagePercent = hasSplit && (usage!.package_total_gb ?? 0) > 0
        ? Math.round(((usage!.package_used_gb ?? 0) / (usage!.package_total_gb as number)) * 100)
        : 0;

    return (
        <div className="space-y-5">
            {/* ① 数据初始化 — 首次全量落盘的两个动作 */}
            <SectionCard
                index="01"
                title="数据初始化"
                desc="首次全量落盘 · 后台执行可断点续传"
                icon={<CloudDownloadOutlined />}
                tone="indigo"
            >
                <div className="flex flex-wrap items-center gap-3 bg-white rounded-2xl border border-slate-100 p-3">
                    <Button
                        type="primary"
                        size="large"
                        icon={<CloudDownloadOutlined />}
                        onClick={() => setInitOpen(true)}
                        className="rounded-xl font-black shadow-sm"
                        style={{ height: 44, padding: '0 22px', fontSize: 14 }}
                    >
                        初始化数据
                    </Button>
                    <Button
                        size="large"
                        icon={<FileSearchOutlined />}
                        onClick={() => setScanOpen(true)}
                        className="rounded-xl font-bold border-slate-200 bg-white"
                        style={{ height: 44, padding: '0 22px', fontSize: 14 }}
                    >
                        本地扫描
                    </Button>
                    <span className="text-xs text-slate-400 leading-relaxed">
                        免流量拉取 56GB 全量（ModelScope 断点续传）或扫描已有离线包建增量
                    </span>
                </div>
                <div className="text-[11px] text-slate-400 mt-2 px-1">
                    已有网盘/归档包建议先 <b className="text-slate-600">本地扫描</b> 再走增量；无本地数据直接 <b className="text-slate-600">初始化数据</b>。完成后到「数据更新」检查差异并同步增量。
                </div>
            </SectionCard>

            {/* ② 数据更新 — QuantDB 状态/流量 + 注册入口 + 检查更新与增量同步 */}
            <SectionCard
                index="02"
                title="数据更新"
                desc="QuantDB 状态与流量 · 数据集检查更新与增量同步"
                icon={<CloudSyncOutlined />}
                tone="blue"
                extra={
                    <Space size="small">
                        <Tag color={info?.connected ? 'green' : 'red'} className="m-0 rounded-full px-2.5 font-bold border-none">
                            {info?.connected ? '已连接' : '未连接'}
                        </Tag>
                        <Button size="small" icon={<ReloadOutlined />} loading={loading} onClick={() => { loadInfo(); loadSources(); }} className="rounded-lg">
                            刷新
                        </Button>
                    </Space>
                }
            >
                {info?.error && <Alert type="error" message={info.error} className="mb-4 rounded-xl" showIcon />}

                {/* 状态行 */}
                <div className="flex flex-wrap items-center gap-2 mb-3">
                    <Tag color={info?.installed ? 'green' : 'red'} icon={info?.installed ? <CheckCircleFilled /> : <CloseCircleFilled />} className="rounded-full font-bold m-0">
                        {info?.installed ? `已安装${info.version ? ` v${info.version}` : ''}` : '未安装 SDK'}
                    </Tag>
                    <Tag color={info?.api_key_configured ? 'green' : 'red'} icon={<ApiOutlined />} className="rounded-full font-bold m-0">
                        {info?.api_key_configured ? '已授权' : '未配置密钥'}
                    </Tag>
                    {info?.account?.username && (
                        <span className="text-xs text-slate-500">
                            账户 <Text code className="text-xs">{info.account.username}</Text>
                            <Text type="secondary" className="text-xs ml-1">{info.account.email}</Text>
                        </span>
                    )}
                    <Button
                        size="small"
                        icon={<GlobalOutlined />}
                        onClick={() => window.open(QUANTDB_REGISTER_URL, '_blank')}
                        className="rounded-lg font-bold text-xs ml-auto"
                    >
                        注册 / 官网
                    </Button>
                    {info?.api_key_configured && (
                        <Button type="link" size="small" className="p-0 text-xs font-bold" onClick={() => navigate('/user-center?tab=data-platform')}>
                            去个人中心 →
                        </Button>
                    )}
                </div>

                {/* 流量条（有数据时才显示）：SDK 0.4.4+ 拆套餐/流量包两行，老版本回退单行 */}
                {info?.usage && (
                    <div className="bg-slate-50 rounded-xl border border-slate-100 px-3 py-2.5 mb-4">
                        <div className="flex items-center justify-between mb-1.5">
                            <Text type="secondary" className="text-xs">流量使用</Text>
                            <Text type="secondary" className="text-xs font-mono">{info.usage.used_gb.toFixed(1)} / {totalGb.toFixed(1)} GB · 剩余 {info.usage.remaining_gb.toFixed(1)} GB</Text>
                        </div>
                        <Progress
                            percent={usagePercent}
                            showInfo={false}
                            size="small"
                            status={usagePercent > USAGE_DANGER_PERCENT ? 'exception' : usagePercent > USAGE_WARN_PERCENT ? 'active' : 'normal'}
                        />
                        {hasSplit && (
                            <div className="mt-2 space-y-1.5">
                                <div className="flex items-center gap-2">
                                    <Text type="secondary" className="text-xs w-10 shrink-0">套餐</Text>
                                    <Progress
                                        percent={planPercent}
                                        showInfo={false}
                                        size="small"
                                        className="flex-1"
                                        status={planPercent > USAGE_DANGER_PERCENT ? 'exception' : 'normal'}
                                    />
                                    <Text type="secondary" className="text-xs font-mono shrink-0">
                                        {(usage!.plan_used_gb ?? 0).toFixed(1)} / {(usage!.plan_total_gb ?? 0).toFixed(1)} GB · 剩 {(usage!.plan_remaining_gb ?? 0).toFixed(1)}
                                    </Text>
                                </div>
                                {(usage!.package_total_gb ?? 0) > 0 && (
                                    <div className="flex items-center gap-2">
                                        <Text type="secondary" className="text-xs w-10 shrink-0">流量包</Text>
                                        <Progress
                                            percent={packagePercent}
                                            showInfo={false}
                                            size="small"
                                            className="flex-1"
                                            status={packagePercent > USAGE_DANGER_PERCENT ? 'exception' : 'normal'}
                                        />
                                        <Text type="secondary" className="text-xs font-mono shrink-0">
                                            {(usage!.package_used_gb ?? 0).toFixed(1)} / {(usage!.package_total_gb ?? 0).toFixed(1)} GB · 剩 {(usage!.package_remaining_gb ?? 0).toFixed(1)}
                                        </Text>
                                    </div>
                                )}
                            </div>
                        )}
                        <div className="flex gap-2 mt-2 flex-wrap">
                            {info.usage.subscription && <Tag color="blue" className="rounded-full text-[11px] m-0">订阅: {info.usage.subscription.status}</Tag>}
                            {info.usage.credit_gb !== undefined && info.usage.credit_gb > 0 && <Tag color="green" className="rounded-full text-[11px] m-0">赠送 {info.usage.credit_gb} GB</Tag>}
                            {(info.usage.remaining_gb ?? 0) < LOW_QUOTA_GB && <Tag color="red" className="rounded-full text-[11px] m-0">余量偏低</Tag>}
                            <span className="text-[11px] text-slate-400 ml-auto">{usagePercent}% 已用</span>
                        </div>
                    </div>
                )}

                <QuantDBCatalogPanel
                    connected={Boolean(info?.connected)}
                    onPreview={setPreviewDataset}
                    refreshSignal={catalogRefreshSignal}
                    embedded
                />
            </SectionCard>

            {/* ③ 同步设置 */}
            <SectionCard
                index="03"
                title="同步设置"
                desc="数据源策略与定时同步"
                icon={<SettingOutlined />}
                tone="amber"
                extra={<Tag className="m-0 rounded-full bg-slate-50 border-slate-200 text-slate-500 text-[11px] font-bold">A 股专用</Tag>}
            >
                <div className="space-y-4">
                    <div className="rounded-2xl border border-slate-100 bg-slate-50/50 p-4">
                        <div className="flex items-center gap-2 mb-3">
                            <DatabaseOutlined className="text-slate-600" />
                            <Text strong className="text-sm">数据源策略</Text>
                            <Text type="secondary" className="text-xs">默认 QuantDB / akshare / 北向 / 南向；雅虎默认关闭</Text>
                        </div>
                        <div className="flex flex-wrap gap-2">
                            {sources.map((s) => (
                                <Checkbox
                                    key={s.source}
                                    checked={s.enabled}
                                    disabled={sourcesLoading}
                                    onChange={(e) => saveSources(s.source, e.target.checked)}
                                    className="bg-white rounded-lg border border-slate-100 px-2.5 py-1.5 m-0"
                                >
                                    <Text className="text-xs font-medium">{s.label}</Text>
                                    <Text type="secondary" className="text-[11px] ml-1">({s.source})</Text>
                                </Checkbox>
                            ))}
                            {sources.length === 0 && <Text type="secondary" className="text-xs">加载中...</Text>}
                        </div>
                    </div>

                    <SyncSchedulePanel market="A" defaultDays={5} />
                </div>
            </SectionCard>

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
