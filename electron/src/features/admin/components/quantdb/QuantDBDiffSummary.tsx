import React from 'react';
import { Button, Card, Space, Statistic, Tag, Tooltip, Typography } from 'antd';
import {
    CheckCircleFilled,
    CloudSyncOutlined,
    ExclamationCircleFilled,
    QuestionCircleFilled,
    SyncOutlined,
} from '@ant-design/icons';
import { QuantDBDiffResult, QuantDBDatasetDiff } from '../../services/dataPlatformService';
import { HIDDEN_DATASETS } from './utils';

const { Text } = Typography;

interface QuantDBDiffSummaryProps {
    diff: QuantDBDiffResult | null;
    loading: boolean;
    onCheckUpdates: () => void;
    onSyncSelected: (datasets: string[]) => void;
}

const STATUS_CONFIG: Record<string, { color: string; label: string; icon: React.ReactNode }> = {
    up_to_date: { color: '#10b981', label: '已最新', icon: <CheckCircleFilled /> },
    updates_available: { color: '#f59e0b', label: '有更新', icon: <ExclamationCircleFilled /> },
    not_synced: { color: '#94a3b8', label: '未同步', icon: <QuestionCircleFilled /> },
    unknown: { color: '#cbd5e1', label: '未知', icon: <QuestionCircleFilled /> },
};

function formatRemoteDate(date: string | null | undefined): string {
    if (!date) return '—';
    const s = String(date);
    if (s.length === 8) return `${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6, 8)}`;
    return s;
}

export const QuantDBDiffSummary: React.FC<QuantDBDiffSummaryProps> = ({
    diff,
    loading,
    onCheckUpdates,
    onSyncSelected,
}) => {
    const summary = diff?.summary;

    const behindDatasets = diff
        ? diff.datasets.filter(d => d.status === 'updates_available' && !HIDDEN_DATASETS.has(d.dataset))
        : [];
    const notSyncedDatasets = diff
        ? diff.datasets.filter(d => d.status === 'not_synced' && !HIDDEN_DATASETS.has(d.dataset))
        : [];

    const syncableDatasets = [...behindDatasets, ...notSyncedDatasets];

    return (
        <Card
            size="small"
            title={
                <Space>
                    <CloudSyncOutlined />
                    <span>数据更新检查</span>
                    {summary && (
                        <Text type="secondary" className="text-xs">
                            （检查时间: {new Date(diff!.timestamp).toLocaleTimeString()}）
                        </Text>
                    )}
                </Space>
            }
            extra={
                <Space>
                    <Button
                        icon={loading ? <SyncOutlined spin /> : <CloudSyncOutlined />}
                        onClick={onCheckUpdates}
                        loading={loading}
                        size="small"
                    >
                        {diff ? '重新检查' : '检查更新'}
                    </Button>
                    {syncableDatasets.length > 0 && (
                        <Button
                            type="primary"
                            size="small"
                            onClick={() => onSyncSelected(syncableDatasets.map(d => d.dataset))}
                        >
                            同步 {syncableDatasets.length} 个数据集
                        </Button>
                    )}
                </Space>
            }
        >
            {diff ? (
                <div>
                    <Space size="large" className="mb-3">
                        <Statistic
                            title="已最新"
                            value={summary!.up_to_date}
                            valueStyle={{ color: '#10b981', fontSize: 20 }}
                            prefix={<CheckCircleFilled />}
                        />
                        <Statistic
                            title="有更新"
                            value={summary!.updates_available}
                            valueStyle={{ color: '#f59e0b', fontSize: 20 }}
                            prefix={<ExclamationCircleFilled />}
                        />
                        <Statistic
                            title="未同步"
                            value={summary!.not_synced}
                            valueStyle={{ color: '#94a3b8', fontSize: 20 }}
                            prefix={<QuestionCircleFilled />}
                        />
                        <Statistic
                            title="未知"
                            value={summary!.unknown}
                            valueStyle={{ color: '#cbd5e1', fontSize: 20 }}
                        />
                    </Space>

                    {syncableDatasets.length > 0 && (
                        <div className="mt-2">
                            <Text type="secondary" className="text-xs">可同步的数据集：</Text>
                            <div className="mt-1 flex flex-wrap gap-1">
                                {syncableDatasets.map(d => {
                                    const cfg = STATUS_CONFIG[d.status];
                                    return (
                                        <Tooltip
                                            key={d.dataset}
                                            title={
                                                <div className="text-xs">
                                                    <div>{d.name}</div>
                                                    {d.local.end_date && (
                                                        <div>本地: {formatRemoteDate(d.local.end_date)}</div>
                                                    )}
                                                    {d.remote?.end_date && (
                                                        <div>远端: {formatRemoteDate(d.remote.end_date)}</div>
                                                    )}
                                                    {d.new_files > 0 && (
                                                        <div>约 {d.new_files} 个新文件</div>
                                                    )}
                                                </div>
                                            }
                                        >
                                            <Tag color={cfg.color === '#10b981' ? 'green' : cfg.color === '#f59e0b' ? 'orange' : 'default'}>
                                                {d.name}
                                            </Tag>
                                        </Tooltip>
                                    );
                                })}
                            </div>
                        </div>
                    )}

                    {summary!.up_to_date === summary!.total_datasets && (
                        <Text type="success" className="text-sm">
                            所有数据集均为最新，无需同步
                        </Text>
                    )}
                </div>
            ) : (
                <Text type="secondary" className="text-sm">
                    点击「检查更新」对比远端与本地数据差异，仅查询元数据不消耗流量
                </Text>
            )}
        </Card>
    );
};

export default QuantDBDiffSummary;
