/**
 * QuantDB 下载用量卡（B1）
 * =======================
 *
 * 按数据集展示近 N 天实际走 CDN 的下载字节/文件数/失败数/耗时。
 * 数据来自同步进程写的 data/quantdb/.usage/YYYYMMDD.json（不依赖 Redis），
 * 与质量卡同一刷新信号：同步完成 / 目录刷新后重新读取。
 */
import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { Button, Card, Space, Tag, Typography, message } from 'antd';
import { CloudDownloadOutlined, ReloadOutlined } from '@ant-design/icons';
import {
    dataPlatformService,
    type QuantDBUsageDataset,
    type QuantDBUsageStats,
} from '../../services/dataPlatformService';
import { describeError } from './utils';

const { Text } = Typography;

function formatBytes(n: number): string {
    if (!Number.isFinite(n) || n <= 0) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let value = n;
    let idx = 0;
    while (value >= 1024 && idx < units.length - 1) {
        value /= 1024;
        idx += 1;
    }
    return `${value.toFixed(idx >= 2 ? 2 : 1)} ${units[idx]}`;
}

function formatSeconds(seconds: number): string {
    if (!Number.isFinite(seconds) || seconds <= 0) return '—';
    if (seconds >= 60) return `${(seconds / 60).toFixed(1)} 分钟`;
    return `${seconds.toFixed(1)} 秒`;
}

interface QuantDBUsageCardProps {
    refreshSignal?: number;
    days?: number;
}

export function QuantDBUsageCard({ refreshSignal = 0, days = 7 }: QuantDBUsageCardProps) {
    const [report, setReport] = useState<QuantDBUsageStats | null>(null);
    const [loading, setLoading] = useState(false);

    const load = useCallback(async () => {
        setLoading(true);
        try {
            const resp = await dataPlatformService.getQuantDBUsageStats(days);
            setReport(resp);
        } catch (error: unknown) {
            message.error(`加载下载用量失败: ${describeError(error)}`);
        } finally {
            setLoading(false);
        }
    }, [days]);

    useEffect(() => {
        void load();
    }, [load, refreshSignal]);

    const topDatasets = useMemo(() => {
        const entries = Object.entries(report?.totals?.datasets ?? {});
        return entries
            .map(([name, stats]) => ({ name, stats: stats as QuantDBUsageDataset }))
            .sort((a, b) => b.stats.bytes - a.stats.bytes)
            .slice(0, 6);
    }, [report]);

    const totals = report?.totals;
    const maxBytes = topDatasets[0]?.stats.bytes ?? 0;
    const hasData = Boolean(report?.days?.length && totals && totals.files > 0);

    return (
        <Card
            size="small"
            title={
                <Space>
                    <CloudDownloadOutlined />
                    下载用量（近 {days} 天）
                </Space>
            }
            extra={
                <Button size="small" icon={<ReloadOutlined />} loading={loading} onClick={() => void load()}>
                    刷新
                </Button>
            }
        >
            {!hasData ? (
                <Text type="secondary" className="text-xs">
                    近 {days} 天暂无实际下载记录。执行一次 QuantDB 同步后这里会按数据集列出流量与失败数。
                </Text>
            ) : (
                <div className="space-y-2">
                    <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
                        <Text className="text-xs">
                            合计 <Text strong>{formatBytes(totals?.bytes ?? 0)}</Text>
                        </Text>
                        <Text type="secondary" className="text-xs">
                            {totals?.files ?? 0} 个文件
                        </Text>
                        <Text type="secondary" className="text-xs">
                            耗时 {formatSeconds(totals?.seconds ?? 0)}
                        </Text>
                        <Text type={totals && totals.errors > 0 ? 'danger' : 'secondary'} className="text-xs">
                            失败 {totals?.errors ?? 0}
                        </Text>
                    </div>

                    <div className="space-y-1.5">
                        {topDatasets.map(({ name, stats }) => {
                            const width = maxBytes > 0 ? Math.max(4, Math.round((stats.bytes / maxBytes) * 100)) : 0;
                            return (
                                <div key={name} className="flex items-center gap-2">
                                    <Text className="w-36 shrink-0 truncate text-[11px]">{name}</Text>
                                    <div className="h-1.5 min-w-0 flex-1 overflow-hidden rounded-full bg-slate-100">
                                        <div
                                            className={`h-full rounded-full ${stats.errors > 0 ? 'bg-amber-400' : 'bg-sky-400'}`}
                                            style={{ width: `${width}%` }}
                                        />
                                    </div>
                                    <Text type="secondary" className="w-20 shrink-0 text-right text-[11px]">
                                        {formatBytes(stats.bytes)}
                                    </Text>
                                    <Text type="secondary" className="w-14 shrink-0 text-right text-[11px]">
                                        {stats.files} 文件
                                    </Text>
                                    {stats.errors > 0 ? (
                                        <Tag color="orange" className="m-0 shrink-0 text-[10px]">
                                            {stats.errors} 失败
                                        </Tag>
                                    ) : null}
                                </div>
                            );
                        })}
                    </div>
                </div>
            )}
        </Card>
    );
}
