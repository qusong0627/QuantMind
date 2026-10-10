import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
    Alert, Button, Card, Checkbox, Collapse, Progress, Space, Table, Tag, Tooltip,
    Typography, message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import {
    CloudSyncOutlined, DatabaseOutlined, EyeOutlined, ReloadOutlined,
    StopOutlined,
} from '@ant-design/icons';
import {
    dataPlatformService, QuantDBDataset, QuantDBGroup, QuantDBSyncJob,
    QuantDBDiffResult,
} from '../../services/dataPlatformService';
import { describeError, formatPartitionDate, formatSize, HIDDEN_DATASETS } from './utils';
import { QuantDBDiffSummary } from './QuantDBDiffSummary';

const { Text } = Typography;

const JOB_POLL_INTERVAL_MS = 3000;

// 前端隐藏的大类：后端 catalog 仍返回，但面板不展示、也不参与默认勾选/同步。
// bond_etf = 债券/ETF（category 4）。
const HIDDEN_GROUP_IDS = new Set(['bond_etf']);

const LAYOUT_LABELS: Record<QuantDBDataset['layout'], { text: string; color: string }> = {
    partition: { text: '按日分区', color: 'blue' },
    symbol: { text: '按标的', color: 'purple' },
    single: { text: '单文件', color: 'default' },
};

const DIFF_STATUS_TAG: Record<string, { color: string; label: string }> = {
    up_to_date: { color: 'green', label: '最新' },
    updates_available: { color: 'orange', label: '有更新' },
    not_synced: { color: 'default', label: '未同步' },
    unknown: { color: 'default', label: '未知' },
};

interface QuantDBCatalogPanelProps {
    connected: boolean;
    onPreview: (dataset: QuantDBDataset) => void;
    /** 外部（如预览抽屉）同步完成后递增，触发目录统计刷新 */
    refreshSignal?: number;
    embedded?: boolean;
}

export function QuantDBCatalogPanel({ connected, onPreview, refreshSignal = 0, embedded = false }: QuantDBCatalogPanelProps) {
    const [groups, setGroups] = useState<QuantDBGroup[]>([]);
    const [datasets, setDatasets] = useState<QuantDBDataset[]>([]);
    const [dataDir, setDataDir] = useState('');
    const [loading, setLoading] = useState(false);
    const [selected, setSelected] = useState<string[]>([]);
    const [submitting, setSubmitting] = useState(false);
    const [activeJob, setActiveJob] = useState<QuantDBSyncJob | null>(null);
    const [cancelling, setCancelling] = useState(false);
    const hasAppliedDefaultSelection = useRef(false);

    // Diff state
    const [diff, setDiff] = useState<QuantDBDiffResult | null>(null);
    const [diffLoading, setDiffLoading] = useState(false);

    const loadCatalog = useCallback(async () => {
        setLoading(true);
        try {
            const resp = await dataPlatformService.getQuantDBCatalog();
            // 过滤掉前端隐藏的大类（如债券/ETF），使它们不展示、不参与勾选
            const visibleGroups = (resp.groups ?? []).filter(
                (g) => !HIDDEN_GROUP_IDS.has(g.id),
            );
            const visibleDatasets = (resp.datasets ?? []).filter(
                (d) => !HIDDEN_GROUP_IDS.has(d.group) && !HIDDEN_DATASETS.has(d.dataset),
            );
            setGroups(visibleGroups);
            setDatasets(visibleDatasets);
            setDataDir(resp.data_dir ?? '');
            if (!hasAppliedDefaultSelection.current) {
                setSelected(visibleDatasets.map((dataset) => dataset.dataset));
                hasAppliedDefaultSelection.current = true;
            }
        } catch (error: unknown) {
            message.error(`加载数据集目录失败: ${describeError(error)}`);
        } finally {
            setLoading(false);
        }
    }, []);

    const loadLatestJob = useCallback(async () => {
        try {
            const resp = await dataPlatformService.listQuantDBSyncJobs();
            setActiveJob(resp.jobs[0] ?? null);
            return resp.jobs[0] ?? null;
        } catch (err: unknown) {
            console.error('[QuantDBCatalogPanel] loadLatestJob failed:', err);
            return null;
        }
    }, []);

    useEffect(() => {
        loadCatalog();
        loadLatestJob();
    }, [loadCatalog, loadLatestJob]);

    // 外部同步（预览抽屉等）完成后刷新目录统计
    useEffect(() => {
        if (refreshSignal > 0) {
            loadCatalog();
        }
    }, [refreshSignal, loadCatalog]);

    // 任务运行期间轮询进度；完成/取消时刷新目录统计
    useEffect(() => {
        if (activeJob?.status !== 'running' && activeJob?.status !== 'cancelling') return;
        const timer = setInterval(async () => {
            const job = await loadLatestJob();
            if (job && job.status !== 'running' && job.status !== 'cancelling') {
                clearInterval(timer);
                loadCatalog();
                if (job.status === 'completed') {
                    message.success(`同步完成：${job.datasets.length} 个数据集`);
                } else if (job.status === 'cancelled') {
                    message.warning(`同步已取消：${job.done}/${job.total} 个数据集已完成`);
                } else {
                    message.error(`同步失败: ${job.error ?? '详见后端日志'}`);
                }
                // 同步结束后刷新 diff
                if (diff) {
                    handleCheckUpdates();
                }
            }
        }, JOB_POLL_INTERVAL_MS);
        return () => clearInterval(timer);
    }, [activeJob?.status, loadLatestJob, loadCatalog]);

    const handleCheckUpdates = useCallback(async () => {
        setDiffLoading(true);
        try {
            const result = await dataPlatformService.checkQuantDBDiff();
            setDiff(result);
        } catch (error: unknown) {
            message.error(`检查更新失败: ${describeError(error)}`);
        } finally {
            setDiffLoading(false);
        }
    }, []);

    const handleSyncFromDiff = useCallback(async (datasets: string[]) => {
        // diff 结果可能含前端隐藏的数据集（如 1分/5分/Tick），一律过滤
        const visible = datasets.filter((n) => !HIDDEN_DATASETS.has(n));
        setSelected(visible);
        if (visible.length === 0) return;
        setSubmitting(true);
        try {
            const resp = await dataPlatformService.syncQuantDBDatasets({
                datasets: visible,
            });
            setActiveJob(resp.job);
            message.success(`已启动同步任务 ${resp.job.job_id}（后台执行）`);
        } catch (error: unknown) {
            message.error(`启动同步失败: ${describeError(error)}`);
        } finally {
            setSubmitting(false);
        }
    }, []);

    const handleCancelSync = useCallback(async () => {
        if (!activeJob || activeJob.status !== 'running') return;
        setCancelling(true);
        try {
            await dataPlatformService.cancelQuantDBSyncJob(activeJob.job_id);
            message.info('取消信号已发送，当前数据集完成后将停止');
        } catch (error: unknown) {
            message.error(`取消失败: ${describeError(error)}`);
        } finally {
            setCancelling(false);
        }
    }, [activeJob]);

    const datasetsByGroup = useMemo(() => {
        const map = new Map<string, QuantDBDataset[]>();
        datasets.forEach((ds) => {
            const list = map.get(ds.group) ?? [];
            list.push(ds);
            map.set(ds.group, list);
        });
        return map;
    }, [datasets]);

    // Build a diff lookup for table column
    const diffByDataset = useMemo(() => {
        const map = new Map<string, string>();
        if (diff) {
            diff.datasets.forEach(d => map.set(d.dataset, d.status));
        }
        return map;
    }, [diff]);

    const toggleGroup = (groupId: string, checked: boolean) => {
        const names = (datasetsByGroup.get(groupId) ?? []).map((d) => d.dataset);
        setSelected(checked
            ? Array.from(new Set([...selected, ...names]))
            : selected.filter((n) => !names.includes(n)));
    };

    const triggerSync = async () => {
        if (selected.length === 0) {
            message.warning('请先勾选要同步的数据集');
            return;
        }
        setSubmitting(true);
        try {
            const resp = await dataPlatformService.syncQuantDBDatasets({
                datasets: selected,
            });
            setActiveJob(resp.job);
            message.success(`已启动同步任务 ${resp.job.job_id}（后台执行）`);
        } catch (error: unknown) {
            message.error(`启动同步失败: ${describeError(error)}`);
        } finally {
            setSubmitting(false);
        }
    };

    const columns: ColumnsType<QuantDBDataset> = [
        {
            title: '数据集',
            dataIndex: 'name',
            width: 190,
            align: 'center',
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
            width: 100,
            align: 'center',
            render: (layout: QuantDBDataset['layout']) => {
                const cfg = LAYOUT_LABELS[layout] || { text: '未知', color: 'default' };
                return <Tag color={cfg.color}>{cfg.text}</Tag>;
            },
        },
        {
            title: '本地状态',
            dataIndex: 'synced',
            width: 100,
            align: 'center',
            render: (synced: boolean, row) => {
                const diffStatus = diffByDataset.get(row.dataset);
                if (diffStatus) {
                    const cfg = DIFF_STATUS_TAG[diffStatus] || DIFF_STATUS_TAG.unknown;
                    return <Tag color={cfg.color}>{cfg.label}</Tag>;
                }
                return <Tag color={synced ? 'green' : 'default'}>{synced ? '已同步' : '未同步'}</Tag>;
            },
        },
        {
            title: '文件数',
            dataIndex: 'files',
            width: 90,
            align: 'center',
            render: (files: number) => (files || 0).toLocaleString(),
        },
        {
            title: '大小',
            dataIndex: 'size_mb',
            width: 100,
            align: 'center',
            render: (sizeMb: number) => formatSize(sizeMb),
        },
        {
            title: '数据区间',
            key: 'range',
            width: 200,
            align: 'center',
            render: (_, row) => (row.start_date
                ? `${formatPartitionDate(row.start_date)} → ${formatPartitionDate(row.end_date)}`
                : '—'),
        },
        {
            title: '说明',
            dataIndex: 'note',
            align: 'center',
            ellipsis: true,
            render: (note: string) => (note
                ? <Tooltip title={note}><Text type="secondary" className="text-xs">{note}</Text></Tooltip>
                : <Text type="secondary">—</Text>),
        },
        {
            title: '操作',
            key: 'action',
            width: 90,
            render: (_, row) => (
                <Button
                    size="small"
                    icon={<EyeOutlined />}
                    onClick={(e) => { e.stopPropagation(); onPreview(row); }}
                >
                    预览
                </Button>
            ),
        },
    ];

    const totalSizeMb = groups.reduce((sum, g) => sum + g.size_mb, 0);
    const isJobRunning = activeJob?.status === 'running' || activeJob?.status === 'cancelling';

    const headerExtra = (
        <Space>
            <Text type="secondary" className="text-xs">
                {datasets.filter((d) => d.synced).length}/{datasets.length} 已同步 · {formatSize(totalSizeMb)}
            </Text>
            <Button size="small" icon={<ReloadOutlined />} onClick={loadCatalog} loading={loading}>
                刷新
            </Button>
        </Space>
    );

    const inner = (
        <>
            {dataDir && (
                <Text type="secondary" className="text-xs block mb-3">
                    本地目录 <Text code>{dataDir}</Text>
                </Text>
            )}
            {!embedded && <div className="mb-3 flex justify-end">{headerExtra}</div>}

            {/* Diff summary card */}
            <div className="mb-3">
                <QuantDBDiffSummary
                    diff={diff}
                    loading={diffLoading}
                    onCheckUpdates={handleCheckUpdates}
                    onSyncSelected={handleSyncFromDiff}
                />
            </div>

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
                                <Tag>{members.filter((d) => d.synced).length}/{members.length} 已同步</Tag>
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
                                    onClick: () => onPreview(record),
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

            <div className="mt-4">
                <Space direction="vertical" className="w-full" size="small">
                    <Space className="w-full">
                        <Button
                            type="primary"
                            icon={<CloudSyncOutlined />}
                            onClick={triggerSync}
                            loading={submitting}
                            disabled={!connected || selected.length === 0 || isJobRunning}
                            className="flex-1"
                        >
                            {isJobRunning
                                ? '已有同步任务进行中...'
                                : `同步选中的 ${selected.length} 个数据集`}
                        </Button>
                        {isJobRunning && activeJob?.status === 'running' && (
                            <Button
                                danger
                                icon={<StopOutlined />}
                                onClick={handleCancelSync}
                                loading={cancelling}
                            >
                                取消同步
                            </Button>
                        )}
                    </Space>
                    {!connected && (
                        <Alert
                            type="warning"
                            showIcon
                            message="SDK 未连接，请先在上方配置有效的 API Key"
                        />
                    )}
                </Space>
            </div>

            {activeJob && <SyncJobProgress job={activeJob} />}
        </>
    );

    if (embedded) {
        return <div className="space-y-3">{inner}</div>;
    }

    return (
        <Card
            size="small"
            title={<Space><DatabaseOutlined />数据集目录与分板块同步</Space>}
            extra={headerExtra}
        >
            {inner}
        </Card>
    );
}

function SyncJobProgress({ job }: { job: QuantDBSyncJob }) {
    const results = job.results ?? [];
    const percent = job.total > 0 ? Math.round((job.done / job.total) * 100) : 0;
    const failed = results.filter((r) => r.status === 'failed');
    const downloaded = results.reduce((sum, r) => sum + (r.downloaded || 0), 0);

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
                    {job.current && <Text type="secondary" className="text-xs">正在同步 {job.current}</Text>}
                    {job.started_by && <Text type="secondary" className="text-xs">由 {job.started_by} 启动</Text>}
                </Space>
                <Progress
                    percent={percent}
                    status={job.status === 'failed' ? 'exception' : (job.status === 'running' || job.status === 'cancelling') ? 'active' : 'success'}
                    format={() => `${job.done}/${job.total}`}
                />
                <Space wrap size="small">
                    <Tag color="blue">下载 {downloaded} 个文件</Tag>
                    {failed.length > 0 && <Tag color="red">{failed.length} 个数据集失败</Tag>}
                    {job.pg_fill && (
                        <Tag color={job.pg_fill.status === 'ok' ? 'green' : 'orange'}>
                            PG: {job.pg_fill.status}
                            {job.pg_fill.rows !== undefined && ` (${job.pg_fill.rows.toLocaleString()} 行)`}
                        </Tag>
                    )}
                    {job.qlib_cache && (
                        <Tag color={job.qlib_cache.status === 'ok' ? 'green' : 'orange'}>
                            Qlib: {job.qlib_cache.status}
                        </Tag>
                    )}
                </Space>
                {/* Per-dataset results */}
                {results.length > 0 && (
                    <div className="mt-1">
                        <Text type="secondary" className="text-xs">数据集进度：</Text>
                        <div className="flex flex-wrap gap-1 mt-1">
                            {results.map((r) => (
                                <Tag
                                    key={r.dataset}
                                    color={r.status === 'synced' ? 'green' : r.status === 'up_to_date' ? 'blue' : 'red'}
                                    className="text-xs"
                                >
                                    {r.dataset}
                                    {r.downloaded > 0 && ` (+${r.downloaded})`}
                                </Tag>
                            ))}
                        </div>
                    </div>
                )}
                {job.error && <Alert type="error" showIcon message={job.error} />}
                {failed.length > 0 && (
                    <Space direction="vertical" size={0}>
                        {failed.map((r) => (
                            <Text key={r.dataset} type="danger" className="text-xs">
                                {r.dataset}: {r.error}
                            </Text>
                        ))}
                    </Space>
                )}
            </Space>
        </div>
    );
}

export default QuantDBCatalogPanel;
