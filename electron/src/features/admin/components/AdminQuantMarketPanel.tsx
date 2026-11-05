import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
    Alert, AutoComplete, Button, Card, Checkbox, Collapse, Empty,
    InputNumber, Modal, Progress, Space, Table, Tag, Tooltip, Typography, message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import {
    CloudServerOutlined, CloudSyncOutlined, DatabaseOutlined, EyeOutlined, SettingOutlined,
    ReloadOutlined, StopOutlined, SyncOutlined,
} from '@ant-design/icons';
import {
    dataPlatformService, QuantDBDataset, QuantDBGroup, QuantDBSyncJob,
    QuantDBPreview,
} from '../services/dataPlatformService';
import { describeError, formatPartitionDate, formatSize } from './quantdb/utils';
import { SyncSchedulePanel } from './data-management/SyncSchedulePanel';
import { SectionCard, VerticalStep } from './data-management/SectionCard';

const { Text } = Typography;

const JOB_POLL_INTERVAL_MS = 3000;
const MAX_PREVIEW_LIMIT = 200;

type MarketKey = 'quantus' | 'quanthk' | 'quantbc' | 'quantfutures';

const MARKET_KEY_TO_SCHEDULE: Record<MarketKey, string> = {
    quantus: 'US',
    quanthk: 'HK',
    quantbc: 'BC',
    quantfutures: 'FUTURES',
};

const LAYOUT_LABELS: Record<QuantDBDataset['layout'], { text: string; color: string }> = {
    partition: { text: '按日分区', color: 'blue' },
    symbol: { text: '按标的', color: 'purple' },
    single: { text: '单文件', color: 'default' },
};

interface AdminQuantMarketPanelProps {
    market: MarketKey;
    marketLabel: string;
    color: string;
}

/** 美股/港股/区块链本地数据管理面板 — 数据集目录 + 同步 + 详情预览。 */
export function AdminQuantMarketPanel({ market, marketLabel, color }: AdminQuantMarketPanelProps) {
    const [groups, setGroups] = useState<QuantDBGroup[]>([]);
    const [datasets, setDatasets] = useState<QuantDBDataset[]>([]);
    const [dataDir, setDataDir] = useState('');
    const [loading, setLoading] = useState(false);
    const [selected, setSelected] = useState<string[]>([]);
    const [days, setDays] = useState(market === 'quantbc' ? 365 : 5);
    const [submitting, setSubmitting] = useState(false);
    const [activeJob, setActiveJob] = useState<QuantDBSyncJob | null>(null);
    const [cancelling, setCancelling] = useState(false);
    const [detailDataset, setDetailDataset] = useState<QuantDBDataset | null>(null);
    const detailDatasetRef = React.useRef<string | null>(null);
    const [sources, setSources] = useState<Array<{ source: string; label: string; enabled: boolean }>>([]);
    const [sourcesLoading, setSourcesLoading] = useState(false);

    const loadSources = useCallback(async () => {
        setSourcesLoading(true);
        try {
            const resp = await dataPlatformService.getMarketDataSources(market);
            setSources(resp.sources);
        } catch (error: unknown) {
            message.error(`加载数据源配置失败: ${describeError(error)}`);
        } finally {
            setSourcesLoading(false);
        }
    }, [market]);

    const saveSources = useCallback(async (source: string, enabled: boolean) => {
        const next = sources.map((s) => (s.source === source ? { ...s, enabled } : s));
        setSources(next);
        try {
            const payload: Record<string, boolean> = {};
            next.forEach((s) => { payload[s.source] = s.enabled; });
            await dataPlatformService.saveMarketDataSources(market, payload);
            message.success(`${marketLabel} 数据源配置已保存`);
        } catch (error: unknown) {
            message.error(`保存数据源配置失败: ${describeError(error)}`);
            loadSources();
        }
    }, [sources, market, marketLabel, loadSources]);

    const loadCatalog = useCallback(async () => {
        setLoading(true);
        try {
            const resp = await dataPlatformService.getMarketCatalog(market);
            setGroups(resp.groups);
            setDatasets(resp.datasets);
            setDataDir(resp.data_dir);
            // 同步刷新详情弹窗中的数据集统计
            if (detailDatasetRef.current) {
                const fresh = resp.datasets.find((d) => d.dataset === detailDatasetRef.current);
                if (fresh) setDetailDataset(fresh);
            }
        } catch (error: unknown) {
            message.error(`加载 ${marketLabel} 数据集目录失败: ${describeError(error)}`);
        } finally {
            setLoading(false);
        }
    }, [market, marketLabel]);

    const loadLatestJob = useCallback(async () => {
        try {
            const resp = await dataPlatformService.listMarketSyncJobs(market);
            setActiveJob(resp.jobs[0] ?? null);
            return resp.jobs[0] ?? null;
        } catch (err: unknown) {
            console.error(`[AdminQuantMarketPanel] loadLatestJob failed for ${market}:`, err);
            return null;
        }
    }, [market]);

    useEffect(() => {
        loadCatalog();
        loadLatestJob();
        loadSources();
    }, [loadCatalog, loadLatestJob, loadSources]);

    // 跟踪当前打开的详情数据集，供 loadCatalog 刷新统计
    useEffect(() => {
        detailDatasetRef.current = detailDataset?.dataset ?? null;
    }, [detailDataset]);

    // 任务运行期间轮询进度；完成/取消时刷新目录统计
    useEffect(() => {
        if (activeJob?.status !== 'running' && activeJob?.status !== 'cancelling') return;
        const timer = setInterval(async () => {
            const job = await loadLatestJob();
            if (job && job.status !== 'running' && job.status !== 'cancelling') {
                clearInterval(timer);
                loadCatalog();
                if (job.status === 'completed') {
                    message.success(`${marketLabel} 同步完成：${job.datasets.length} 个数据集`);
                } else if (job.status === 'cancelled') {
                    message.warning(`同步已取消`);
                } else {
                    message.error(`同步失败: ${job.error ?? '详见后端日志'}`);
                }
            }
        }, JOB_POLL_INTERVAL_MS);
        return () => clearInterval(timer);
    }, [activeJob?.status, loadLatestJob, loadCatalog, marketLabel]);

    const triggerSync = async () => {
        if (selected.length === 0) {
            message.warning('请先勾选要同步的数据集');
            return;
        }
        setSubmitting(true);
        try {
            const resp = await dataPlatformService.syncMarketDatasets(market, {
                datasets: selected,
                days,
            });
            setActiveJob(resp.job);
            message.success(`已启动同步任务 ${resp.job.job_id}（后台执行）`);
        } catch (error: unknown) {
            message.error(`启动同步失败: ${describeError(error)}`);
        } finally {
            setSubmitting(false);
        }
    };

    const handleCancelSync = useCallback(async () => {
        if (!activeJob || activeJob.status !== 'running') return;
        setCancelling(true);
        try {
            await dataPlatformService.cancelMarketSyncJob(market, activeJob.job_id);
            message.info('取消信号已发送');
        } catch (error: unknown) {
            message.error(`取消失败: ${describeError(error)}`);
        } finally {
            setCancelling(false);
        }
    }, [activeJob, market]);

    const datasetsByGroup = useMemo(() => {
        const map = new Map<string, QuantDBDataset[]>();
        datasets.forEach((ds) => {
            const list = map.get(ds.group) ?? [];
            list.push(ds);
            map.set(ds.group, list);
        });
        return map;
    }, [datasets]);

    const toggleGroup = (groupId: string, checked: boolean) => {
        const names = (datasetsByGroup.get(groupId) ?? []).map((d) => d.dataset);
        setSelected(checked
            ? Array.from(new Set([...selected, ...names]))
            : selected.filter((n) => !names.includes(n)));
    };

    const columns: ColumnsType<QuantDBDataset> = [
        {
            title: '数据集',
            dataIndex: 'name',
            width: 160,
            render: (name: string, row) => (
                <Space direction="vertical" size={0}>
                    <Text strong>{name}</Text>
                    <Text type="secondary" className="text-xs">{row.dataset}</Text>
                </Space>
            ),
        },
        {
            title: '形态',
            dataIndex: 'layout',
            width: 90,
            render: (layout: QuantDBDataset['layout']) => (
                <Tag color={LAYOUT_LABELS[layout].color}>{LAYOUT_LABELS[layout].text}</Tag>
            ),
        },
        {
            title: '本地状态',
            dataIndex: 'synced',
            width: 90,
            render: (synced: boolean) => (
                <Tag color={synced ? 'green' : 'default'}>{synced ? '已同步' : '未同步'}</Tag>
            ),
        },
        {
            title: '数据量',
            key: 'volume',
            width: 130,
            render: (_, row) => row.files > 0
                ? <Text className="text-xs">{row.files.toLocaleString()} 文件 · {formatSize(row.size_mb)}</Text>
                : <Text type="secondary">—</Text>,
        },
        {
            title: '数据区间',
            key: 'range',
            width: 170,
            render: (_, row) => (row.start_date
                ? `${formatPartitionDate(row.start_date)} → ${formatPartitionDate(row.end_date)}`
                : '—'),
        },
        {
            title: '更新时间',
            key: 'updated',
            width: 140,
            render: (_, row) => (row.updated_at
                ? <Text type="secondary" className="text-xs">{new Date(row.updated_at).toLocaleString()}</Text>
                : '—'),
        },
        {
            title: '说明',
            dataIndex: 'note',
            ellipsis: true,
            render: (note: string) => (note
                ? <Tooltip title={note}><Text type="secondary" className="text-xs">{note}</Text></Tooltip>
                : <Text type="secondary">—</Text>),
        },
        {
            title: '操作',
            key: 'action',
            width: 90,
            render: (_, row) => row.synced ? (
                <Button
                    size="small"
                    type="link"
                    icon={<EyeOutlined />}
                    onClick={(e) => { e.stopPropagation(); setDetailDataset(row); }}
                >
                    查看
                </Button>
            ) : (
                <Button
                    size="small"
                    type="primary"
                    ghost
                    icon={<CloudSyncOutlined />}
                    disabled={isJobRunning}
                    onClick={(e) => {
                        e.stopPropagation();
                        setDetailDataset(row);
                        // 延迟到弹窗打开后触发同步
                        setTimeout(() => triggerSingleSync(row), 300);
                    }}
                >
                    同步
                </Button>
            ),
        },
    ];

    // 单个数据集同步：启动任务并返回 job
    const triggerSingleSync = async (ds: QuantDBDataset) => {
        try {
            const resp = await dataPlatformService.syncMarketDatasets(market, {
                datasets: [ds.dataset],
                days,
            });
            setActiveJob(resp.job);
            message.success(`已启动 ${ds.name} 同步（后台执行）`);
        } catch (error: unknown) {
            message.error(`启动同步失败: ${describeError(error)}`);
        }
    };

    const totalSizeMb = groups.reduce((sum, g) => sum + g.size_mb, 0);
    const isJobRunning = activeJob?.status === 'running' || activeJob?.status === 'cancelling';

    const hasSources = sources.length > 0;
    const enabledCount = sources.filter(x=>x.enabled).length;
    const syncReady = selected.length>0 && !isJobRunning;

    return (
        <div className="space-y-5">
            {/* ① 引导 — 纵向 */}
            <SectionCard
                index="01"
                title="引导初始化"
                desc={`${marketLabel} · 数据源与本地环境`}
                icon={<CloudServerOutlined style={{ color }} />}
                tone="indigo"
                extra={<Tag className="m-0 rounded-full bg-slate-50 border-slate-200 text-slate-500 text-[11px] font-bold">{hasSources ? `${enabledCount}/${sources.length} 源已启用` : '加载中'}</Tag>}
            >
                <div className="flex flex-col">
                    <VerticalStep
                        done={enabledCount>0}
                        title="数据源配置"
                        desc="按勾选的数据源分发同步任务，关闭不需要的源可减少无效请求"
                        extra={
                            <div className="rounded-2xl border border-slate-100 bg-slate-50/50 p-3">
                                <div className="flex flex-wrap gap-2">
                                    {sources.map((s) => (
                                        <Checkbox
                                            key={s.source}
                                            checked={s.enabled}
                                            disabled={sourcesLoading}
                                            onChange={(e) => saveSources(s.source, e.target.checked)}
                                            className="bg-white rounded-lg border border-slate-100 px-2.5 py-1.5 m-0"
                                        >
                                            <span className="text-xs font-medium">{s.label}</span>
                                            <span className="text-[11px] text-slate-400 ml-1">({s.source})</span>
                                        </Checkbox>
                                    ))}
                                    {sources.length===0 && <span className="text-xs text-slate-400">加载中...</span>}
                                </div>
                            </div>
                        }
                    />
                    <VerticalStep
                        done={datasets.some(d=>d.synced)}
                        active={enabledCount>0}
                        isLast
                        title="本地落盘"
                        desc={dataDir ? `本地目录 ${dataDir} · ${datasets.filter(d=>d.synced).length}/${datasets.length} 已同步` : '本地数据目录检测中'}
                        extra={
                            <div className="text-[11px] text-slate-500 bg-white rounded-xl border border-slate-100 px-3 py-2">
                                {dataDir ? <span>目录 <code className="text-xs bg-slate-50 px-1 py-0.5 rounded border">{dataDir}</code></span> : '—'} · 按需勾选下方数据集后在“数据同步”区发起同步
                            </div>
                        }
                    />
                </div>
            </SectionCard>

            {/* ② 数据同步 */}
            <SectionCard
                index="02"
                title="数据同步"
                desc="数据集目录 · 增量同步 · 后台任务"
                icon={<DatabaseOutlined />}
                tone="blue"
                extra={
                    <Space size="small">
                        <span className="text-xs text-slate-400 hidden sm:inline">{datasets.filter((d) => d.synced).length}/{datasets.length} 已同步 · {formatSize(totalSizeMb)}</span>
                        <Button size="small" icon={<ReloadOutlined />} onClick={loadCatalog} loading={loading} className="rounded-lg">刷新</Button>
                    </Space>
                }
            >
                <div className="space-y-4">
                    <Collapse
                        defaultActiveKey={groups.length > 0 ? [groups[0].id] : []}
                        items={groups.map((group) => {
                            const members = datasetsByGroup.get(group.id) ?? [];
                            const names = members.map((d) => d.dataset);
                            const checkedCount = names.filter((n) => selected.includes(n)).length;
                            return {
                                key: group.id,
                                label: (
                                    <Space onClick={(e) => e.stopPropagation()}>
                                        <Checkbox
                                            checked={checkedCount > 0 && checkedCount === names.length}
                                            indeterminate={checkedCount > 0 && checkedCount < names.length}
                                            onChange={(e) => toggleGroup(group.id, e.target.checked)}
                                        >
                                            <Text strong>{group.name}</Text>
                                        </Checkbox>
                                        <Tag>{group.synced_count}/{group.dataset_count} 已同步</Tag>
                                        <Text type="secondary" className="text-xs">{formatSize(group.size_mb)}</Text>
                                    </Space>
                                ),
                                children: (
                                    <Table
                                        dataSource={members}
                                        columns={columns}
                                        rowKey="dataset"
                                        size="small"
                                        pagination={false}
                                        scroll={{ x: 'max-content' }}
                                        onRow={(record) => ({
                                            onClick: () => setDetailDataset(record),
                                            style: { cursor: 'pointer' },
                                        })}
                                        rowSelection={{
                                            selectedRowKeys: selected.filter((n) => names.includes(n)),
                                            onChange: (keys) => {
                                                const picked = keys as string[];
                                                setSelected([
                                                    ...selected.filter((n) => !names.includes(n)),
                                                    ...picked,
                                                ]);
                                            },
                                        }}
                                    />
                                ),
                            };
                        })}
                    />

                    <div className="flex flex-wrap items-center gap-2 bg-slate-50 rounded-2xl border border-slate-100 p-3">
                        <span className="text-xs text-slate-500 font-medium">同步最近</span>
                        <InputNumber
                            min={1}
                            max={365}
                            value={days}
                            onChange={(v) => setDays(v ?? 5)}
                            style={{ width: 80 }}
                            size="small"
                        />
                        <span className="text-xs text-slate-500">{market === 'quantbc' ? '个自然日' : '个交易日'}</span>
                        <div className="flex-1" />
                        <Button
                            type="primary"
                            icon={<CloudSyncOutlined />}
                            onClick={triggerSync}
                            loading={submitting}
                            disabled={selected.length === 0 || isJobRunning}
                            className="rounded-xl font-bold"
                        >
                            {isJobRunning ? '已有同步任务进行中...' : `按数据源同步 ${selected.length} 个数据集`}
                        </Button>
                        {isJobRunning && activeJob?.status === 'running' && (
                            <Button danger icon={<StopOutlined />} onClick={handleCancelSync} loading={cancelling} className="rounded-xl">取消同步</Button>
                        )}
                    </div>
                    <div className="text-[11px] text-slate-400 bg-white rounded-xl border border-slate-100 px-3 py-2">
                        {market === 'quantbc'
                            ? '数据源为 Binance 公开 API，实时拉取后按 QuantDB 格式落盘本地 parquet（A股格式一致）。'
                            : '数据源按勾选分发：日线/财务/分析师走 Yahoo Finance，估值/财务指标/公司资料/指数走 akshare，落盘后按 QuantDB 格式存本地 parquet（A股格式一致）。'}
                    </div>
                    {activeJob && <MarketSyncJobProgress job={activeJob} />}
                </div>
            </SectionCard>

            {/* ③ 同步设置 */}
            <SectionCard
                index="03"
                title="同步设置"
                desc="定时同步 · 后台自动执行"
                icon={<SettingOutlined />}
                tone="amber"
            >
                <SyncSchedulePanel
                    market={MARKET_KEY_TO_SCHEDULE[market]}
                    selectedDatasets={selected}
                    defaultDays={market === 'quantbc' ? 365 : 5}
                />
            </SectionCard>



            <MarketDataModal
                market={market}
                dataset={detailDataset}
                activeJob={activeJob}
                onClose={() => setDetailDataset(null)}
                onRefreshCatalog={loadCatalog}
                onSync={(ds) => triggerSingleSync(ds)}
            />
        </div>
    );
}

function MarketSyncJobProgress({ job }: { job: QuantDBSyncJob }) {
    const percent = job.total > 0 ? Math.round((job.done / job.total) * 100) : 0;
    const statusLabel = job.status === 'running'
        ? `进行中 · ${job.stage}`
        : job.status === 'cancelling'
            ? '正在取消...'
            : job.status === 'cancelled'
                ? '已取消'
                : job.status;
    const statusColor = job.status === 'completed'
        ? 'green'
        : job.status === 'failed'
            ? 'red'
            : job.status === 'cancelled'
                ? 'orange'
                : job.status === 'cancelling'
                    ? 'orange'
                    : 'blue';

    return (
        <div className="mt-4 p-3 bg-gray-50 rounded">
            <Space direction="vertical" className="w-full" size="small">
                <Space wrap>
                    <Text strong>{job.job_id}</Text>
                    <Tag color={statusColor}>{statusLabel}</Tag>
                    {job.started_by && <Text type="secondary" className="text-xs">由 {job.started_by} 启动</Text>}
                </Space>
                <Progress
                    percent={percent}
                    status={job.status === 'failed' ? 'exception' : (job.status === 'running' || job.status === 'cancelling') ? 'active' : 'success'}
                    format={() => `${job.done}/${job.total}`}
                />
                {job.summary && typeof job.summary === 'object' && (
                    <Text type="secondary" className="text-xs">
                        {JSON.stringify(job.summary)}
                    </Text>
                )}
                {job.error && <Alert type="error" showIcon message={job.error} />}
            </Space>
        </div>
    );
}

// 数据集详情大框：统计 + 检索 + 刷新/更新 + 表格
interface MarketDataModalProps {
    market: MarketKey;
    dataset: QuantDBDataset | null;
    activeJob: QuantDBSyncJob | null;
    onClose: () => void;
    onRefreshCatalog: () => void;
    onSync: (ds: QuantDBDataset) => void;
}

function MarketDataModal({ market, dataset, activeJob, onClose, onRefreshCatalog, onSync }: MarketDataModalProps) {
    const [preview, setPreview] = useState<QuantDBPreview | null>(null);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [symbol, setSymbol] = useState('');
    const [limit, setLimit] = useState(50);
    const [syncing, setSyncing] = useState(false);

    const isSyncingThis = activeJob?.status === 'running' && dataset
        ? activeJob.datasets.includes(dataset.dataset)
        : false;
    const syncingPercent = activeJob && activeJob.total > 0
        ? Math.round((activeJob.done / activeJob.total) * 100)
        : 0;

    const load = useCallback(async () => {
        if (!dataset) return;
        setLoading(true);
        setError(null);
        try {
            setPreview(await dataPlatformService.previewMarketDataset(market, {
                dataset: dataset.dataset,
                symbol: symbol.trim() || undefined,
                limit,
            }));
        } catch (err: unknown) {
            setError(describeError(err));
            setPreview(null);
        } finally {
            setLoading(false);
        }
    }, [dataset, symbol, limit, market]);

    useEffect(() => {
        if (!dataset) {
            setPreview(null);
            return;
        }
        setSymbol('');
        setLimit(50);
        setError(null);
        dataPlatformService
            .previewMarketDataset(market, { dataset: dataset.dataset, limit: 50 })
            .then(setPreview)
            .catch((err: unknown) => {
                setError(describeError(err));
                setPreview(null);
            });
    }, [dataset, market]);

    // 同步进行中时轮询任务，完成后刷新
    useEffect(() => {
        if (!isSyncingThis) return;
        const timer = setInterval(async () => {
            const job = await dataPlatformService.listMarketSyncJobs(market);
            const latest = job.jobs[0];
            if (latest && latest.status === 'completed' && latest.datasets.includes(dataset!.dataset)) {
                clearInterval(timer);
                setSyncing(false);
                message.success(`${dataset!.name} 同步完成`);
                onRefreshCatalog();
                load();
            } else if (latest && latest.status === 'failed') {
                clearInterval(timer);
                setSyncing(false);
                message.error(`同步失败: ${latest.error ?? '未知错误'}`);
            }
        }, JOB_POLL_INTERVAL_MS);
        return () => clearInterval(timer);
    }, [isSyncingThis, dataset, market, onRefreshCatalog, load]);

    const handleUpdate = () => {
        if (!dataset) return;
        setSyncing(true);
        onSync(dataset);
    };

    const columns: ColumnsType<Record<string, unknown>> = (preview?.columns ?? []).map((col) => ({
        title: (
            <Space direction="vertical" size={0}>
                <Text strong className="text-xs">{col.name}</Text>
                <Text type="secondary" style={{ fontSize: 10 }}>{col.dtype}</Text>
            </Space>
        ),
        dataIndex: col.name,
        key: col.name,
        width: 150,
        ellipsis: true,
        render: (value: unknown) => formatCell(value),
    }));

    const supportsSymbol = dataset?.layout === 'symbol' || dataset?.layout === 'partition';

    return (
        <Modal
            open={dataset !== null}
            onCancel={onClose}
            width="88%"
            title={dataset ? `${dataset.name} · ${dataset.dataset}` : ''}
            footer={null}
            destroyOnHidden
        >
            {dataset && (
                <Space direction="vertical" className="w-full" size="middle">
                    {/* 数据统计 + 操作 */}
                    <div className="p-3 bg-gray-50 rounded flex flex-wrap items-center gap-3">
                        <Tag color={dataset.synced ? 'green' : 'orange'}>
                            {dataset.synced ? '已同步' : '未同步'}
                        </Tag>
                        <Text type="secondary" className="text-xs">
                            数据量: {dataset.files > 0
                                ? `${dataset.files.toLocaleString()} 文件 · ${formatSize(dataset.size_mb)}`
                                : '暂无'}
                        </Text>
                        {dataset.start_date && (
                            <Text type="secondary" className="text-xs">
                                区间: {formatPartitionDate(dataset.start_date)} → {formatPartitionDate(dataset.end_date)}
                            </Text>
                        )}
                        {dataset.updated_at && (
                            <Text type="secondary" className="text-xs">
                                更新: {new Date(dataset.updated_at).toLocaleString()}
                            </Text>
                        )}
                        <div className="flex-1" />
                        {isSyncingThis && (
                            <Space size="small">
                                <Progress percent={syncingPercent} size="small" status="active" style={{ width: 120 }} />
                                <Text type="secondary" className="text-xs">同步中...</Text>
                            </Space>
                        )}
                        <Button
                            icon={<ReloadOutlined />}
                            onClick={load}
                            loading={loading}
                            disabled={isSyncingThis}
                        >
                            刷新
                        </Button>
                        <Button
                            type="primary"
                            icon={<CloudSyncOutlined />}
                            onClick={handleUpdate}
                            loading={syncing}
                            disabled={isSyncingThis || (activeJob?.status === 'running' && !isSyncingThis)}
                        >
                            {isSyncingThis ? '同步中...' : (dataset.synced ? '更新' : '同步')}
                        </Button>
                    </div>

                    {/* 检索区 */}
                    <Space wrap>
                        {supportsSymbol && (
                            <AutoComplete
                                value={symbol}
                                onChange={setSymbol}
                                options={(preview?.symbol_choices ?? []).map((s) => ({
                                    value: s,
                                    label: preview?.symbol_names?.[s]
                                        ? `${s} · ${preview.symbol_names[s]}`
                                        : s,
                                }))}
                                filterOption={(input, option) =>
                                    String(option?.value ?? '').toUpperCase().includes(input.toUpperCase())
                                    || String(option?.label ?? '').includes(input)
                                }
                                placeholder="检索标的代码"
                                style={{ width: 260 }}
                                onSelect={() => load()}
                            />
                        )}
                        <Space size="small">
                            <Text type="secondary" className="text-xs">行数</Text>
                            <InputNumber
                                min={1}
                                max={MAX_PREVIEW_LIMIT}
                                value={limit}
                                onChange={(v) => setLimit(v ?? 50)}
                                style={{ width: 80 }}
                            />
                        </Space>
                        <Button type="primary" onClick={load} loading={loading}>
                            查询
                        </Button>
                        {preview && (
                            <Space wrap size="small">
                                <Tag>{preview.rows_total.toLocaleString()} 行</Tag>
                                <Tag>{preview.column_count ?? preview.columns.length} 列</Tag>
                                {preview.symbol_total !== undefined && (
                                    <Tag color="purple">{preview.symbol_total.toLocaleString()} 个标的</Tag>
                                )}
                            </Space>
                        )}
                    </Space>

                    {error && <Alert type="error" showIcon message="加载失败" description={error} />}

                    {preview && preview.data.length > 0 ? (
                        <Table
                            dataSource={preview.data.map((r, i) => ({ ...r, _key: String(i) }))}
                            columns={columns}
                            rowKey="_key"
                            size="small"
                            loading={loading}
                            pagination={{ pageSize: 20, size: 'small', showSizeChanger: true }}
                            scroll={{ x: 'max-content', y: 480 }}
                            bordered
                        />
                    ) : (
                        !error && !loading && (
                            <Empty
                                description={dataset?.synced
                                    ? '该数据集本地无可预览样本'
                                    : '该数据集尚未同步，点击右上角「同步」开始拉取数据'}
                            />
                        )
                    )}
                </Space>
            )}
        </Modal>
    );
}

function formatCell(value: unknown): React.ReactNode {
    if (value === null || value === undefined) {
        return <Text type="secondary">null</Text>;
    }
    if (typeof value === 'number') {
        return Number.isInteger(value) ? value.toLocaleString() : value.toFixed(4);
    }
    if (typeof value === 'boolean') {
        return String(value);
    }
    return String(value);
}

export default AdminQuantMarketPanel;
