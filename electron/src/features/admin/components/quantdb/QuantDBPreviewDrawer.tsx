import React, { useCallback, useEffect, useState } from 'react';
import {
    Alert, AutoComplete, Button, Empty, InputNumber, Modal, Progress, Space,
    Table, Tag, Tooltip, Typography, message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { CloudDownloadOutlined, CloudSyncOutlined, ReloadOutlined } from '@ant-design/icons';
import {
    dataPlatformService, QuantDBDataset, QuantDBPreview, QuantDBSyncJob,
} from '../../services/dataPlatformService';
import { describeError } from './utils';

const { Text } = Typography;

const DEFAULT_LIMIT = 50;
const MAX_LIMIT = 200;
const JOB_POLL_INTERVAL_MS = 3000;

interface QuantDBPreviewDrawerProps {
    dataset: QuantDBDataset | null;
    onClose: () => void;
    /** 该数据集同步完成后刷新目录统计（可选） */
    onSynced?: () => void;
}

export function QuantDBPreviewDrawer({ dataset, onClose, onSynced }: QuantDBPreviewDrawerProps) {
    const [preview, setPreview] = useState<QuantDBPreview | null>(null);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [symbol, setSymbol] = useState('');
    const [limit, setLimit] = useState(DEFAULT_LIMIT);
    const [job, setJob] = useState<QuantDBSyncJob | null>(null);
    const [startingSync, setStartingSync] = useState(false);

    const load = useCallback(async (opts: { remote?: boolean } = {}) => {
        if (!dataset) return;
        setLoading(true);
        setError(null);
        try {
            setPreview(await dataPlatformService.previewQuantDBDataset({
                dataset: dataset.dataset,
                symbol: symbol.trim() || undefined,
                limit,
                remote: opts.remote,
            }));
        } catch (err: unknown) {
            setError(describeError(err));
            setPreview(null);
        } finally {
            setLoading(false);
        }
    }, [dataset, symbol, limit]);

    // 切换数据集时重置查询条件并自动加载
    useEffect(() => {
        if (!dataset) {
            setPreview(null);
            return;
        }
        setSymbol('');
        setLimit(DEFAULT_LIMIT);
        setError(null);
        dataPlatformService
            .previewQuantDBDataset({ dataset: dataset.dataset, limit: DEFAULT_LIMIT })
            .then(setPreview)
            .catch((err: unknown) => {
                setError(describeError(err));
                setPreview(null);
            });
    }, [dataset]);

    const fetchRemote = async () => {
        await load({ remote: true });
        message.info('已通过 SDK 远端预览（不消耗下载流量）');
    };

    const loadLatestJob = useCallback(async (): Promise<QuantDBSyncJob | null> => {
        try {
            const resp = await dataPlatformService.listQuantDBSyncJobs();
            const latest = resp.jobs[0] ?? null;
            setJob(latest);
            return latest;
        } catch (err: unknown) {
            console.error('[QuantDBPreviewDrawer] loadLatestJob failed:', err);
            return null;
        }
    }, []);

    // 打开抽屉时感知正在运行的同步任务（如从目录页发起的）
    useEffect(() => {
        if (!dataset) {
            setJob(null);
            return;
        }
        loadLatestJob();
    }, [dataset, loadLatestJob]);

    // 该数据集存在进行中的任务 → 轮询；完成/失败/取消时给出反馈
    const isSyncingThis = Boolean(
        dataset
            && job
            && ['running', 'cancelling'].includes(job.status)
            && job.datasets.includes(dataset.dataset),
    );
    const syncingPercent = isSyncingThis && job && job.total > 0
        ? Math.round((job.done / job.total) * 100)
        : 0;

    useEffect(() => {
        if (!isSyncingThis || !dataset) return;
        const timer = setInterval(async () => {
            const latest = await loadLatestJob();
            if (!latest || ['running', 'cancelling'].includes(latest.status)) return;
            if (latest.status === 'completed') {
                message.success(`${dataset.name} 增量同步完成`);
                onSynced?.();
                load(); // 同步完成后重新加载本地预览
            } else if (latest.status === 'failed') {
                message.error(`同步失败: ${latest.error ?? '详见后端日志'}`);
            } else {
                message.warning('同步已取消');
            }
        }, JOB_POLL_INTERVAL_MS);
        return () => clearInterval(timer);
    }, [isSyncingThis, dataset, loadLatestJob, onSynced, load]);

    const handleSync = async () => {
        if (!dataset) return;
        setStartingSync(true);
        try {
            const resp = await dataPlatformService.syncQuantDBDatasets({
                datasets: [dataset.dataset],
            });
            setJob(resp.job);
            message.success(`已启动 ${dataset.name} 增量同步（后台执行）`);
        } catch (err: unknown) {
            message.error(`启动同步失败: ${describeError(err)}`);
        } finally {
            setStartingSync(false);
        }
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

    const supportsSymbol = dataset?.layout === 'symbol';

    return (
        <Modal
            open={dataset !== null}
            onCancel={onClose}
            width="88%"
            title={dataset ? `${dataset.name} · ${dataset.dataset}` : ''}
            footer={null}
            destroyOnHidden
        >
            <Space direction="vertical" className="w-full" size="middle">
                <Space wrap>
                    {supportsSymbol && (
                        <AutoComplete
                            value={symbol}
                            onChange={setSymbol}
                            options={(preview?.symbol_choices ?? []).map((s) => ({ value: s }))}
                            filterOption={(input, option) =>
                                String(option?.value ?? '').toUpperCase().includes(input.toUpperCase())
                            }
                            placeholder="标的代码，如 600519.SH"
                            style={{ width: 220 }}
                            onSelect={() => load()}
                        />
                    )}
                    <Space size="small">
                        <Text type="secondary" className="text-xs">行数</Text>
                        <InputNumber
                            min={1}
                            max={MAX_LIMIT}
                            value={limit}
                            onChange={(v) => setLimit(v ?? DEFAULT_LIMIT)}
                            style={{ width: 90 }}
                        />
                    </Space>
                    <Button type="primary" onClick={() => load()} loading={loading}>
                        查询
                    </Button>
                    <Button icon={<ReloadOutlined />} onClick={() => load()} loading={loading}>
                        刷新
                    </Button>
                    <Button icon={<CloudDownloadOutlined />} onClick={fetchRemote} loading={loading}>
                        远端预览
                    </Button>
                    <Tooltip title="比对远端 manifest：本地缺失或与远端不一致的数据自动从远端增量拉取">
                        <Button
                            type="primary"
                            icon={<CloudSyncOutlined />}
                            onClick={handleSync}
                            loading={startingSync}
                            disabled={isSyncingThis}
                        >
                            {isSyncingThis ? '同步中...' : '增量同步'}
                        </Button>
                    </Tooltip>
                </Space>

                {isSyncingThis && job && (
                    <Space size="small" align="center">
                        <Text type="secondary" className="text-xs">
                            {job.stage}
                            {job.current ? ` · ${job.current}` : ''}
                        </Text>
                        <Progress
                            percent={syncingPercent}
                            size="small"
                            status="active"
                            style={{ width: 160 }}
                        />
                    </Space>
                )}

                {preview && (
                    <Space wrap size="small">
                        <Tag color={preview.source === 'local' ? 'green' : 'blue'}>
                            {preview.source === 'local' ? '本地 parquet（零流量）' : 'SDK 远端预览'}
                        </Tag>
                        <Tag>{preview.rows_total.toLocaleString()} 行</Tag>
                        <Tag>{preview.column_count ?? preview.columns.length} 列</Tag>
                        {preview.symbol_total !== undefined && (
                            <Tag color="purple">{preview.symbol_total.toLocaleString()} 个标的</Tag>
                        )}
                        {preview.file && (
                            <Text type="secondary" className="text-xs">{preview.file}</Text>
                        )}
                    </Space>
                )}

                {error && (
                    <Alert
                        type="error"
                        showIcon
                        message="预览失败"
                        description={error}
                    />
                )}

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
                        <Empty description={
                            dataset?.synced
                                ? '该数据集本地无可预览样本，可尝试远端预览'
                                : '该数据集尚未同步到本地，可尝试远端预览'
                        } />
                    )
                )}
            </Space>
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

export default QuantDBPreviewDrawer;
