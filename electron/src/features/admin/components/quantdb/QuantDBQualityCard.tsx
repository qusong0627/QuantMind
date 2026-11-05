/**
 * QuantDB 同步语义质量卡（A1）
 * =============================
 *
 * 数据管理页「数据集目录」下方展示最近一次同步的质量闸门结果：
 * 分区覆盖 / 行数断崖 / 列集漂移。默认读后端落盘报告（零开销），
 * 「现场检查」才实时跑一遍（只读，不写告警）。
 *
 * 设计取舍：这张卡只回答「数据能不能信」，不重复目录页的下载统计；
 * 因此只列异常项，全部通过时保持一行绿色结论，避免又变成一张噪声卡。
 */
import React, { useCallback, useEffect, useState } from 'react';
import { Alert, Button, Card, Space, Tag, Tooltip, Typography, message } from 'antd';
import { ReloadOutlined, SafetyCertificateOutlined } from '@ant-design/icons';
import {
    dataPlatformService,
    type QuantDBQualityIssue,
    type QuantDBQualityReport,
} from '../../services/dataPlatformService';
import { describeError } from './utils';

const { Text } = Typography;

const ISSUE_LABELS: Record<string, string> = {
    latest_lag: '最新分区缺失',
    missing_partition: '历史分区缺口',
    row_count_drop: '行数断崖',
    schema_drift: '列集漂移',
    check_failed: '检查失败',
};

const SEVERITY_COLORS: Record<string, string> = {
    error: 'red',
    warning: 'orange',
};

interface QuantDBQualityCardProps {
    /** 外部（同步完成 / 目录刷新）递增，触发重新读取报告 */
    refreshSignal?: number;
}

export function QuantDBQualityCard({ refreshSignal = 0 }: QuantDBQualityCardProps) {
    const [report, setReport] = useState<QuantDBQualityReport | null>(null);
    const [source, setSource] = useState<string>('none');
    const [loading, setLoading] = useState(false);

    const load = useCallback(async (refresh = false) => {
        setLoading(true);
        try {
            const resp = await dataPlatformService.getQuantDBQuality(refresh);
            setReport(resp.report);
            setSource(resp.source);
        } catch (error: unknown) {
            message.error(`加载同步质量报告失败: ${describeError(error)}`);
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => {
        void load(false);
    }, [load, refreshSignal]);

    const issues: QuantDBQualityIssue[] = report?.issues ?? [];
    const errorCount = issues.filter((i) => i.severity === 'error').length;
    const warningCount = issues.filter((i) => i.severity !== 'error').length;
    const unavailable = report?.available === false || !report;
    const sourceLabel = source === 'fresh' ? '现场检查' : source === 'cache' ? '同步后报告' : '暂无报告';

    return (
        <Card
            size="small"
            className="mb-3"
            title={
                <Space>
                    <SafetyCertificateOutlined />
                    同步质量（覆盖 / 行数 / 列集）
                </Space>
            }
            extra={
                <Space>
                    <Tag color={source === 'fresh' ? 'blue' : 'default'}>{sourceLabel}</Tag>
                    <Button
                        size="small"
                        icon={<ReloadOutlined />}
                        loading={loading}
                        onClick={() => void load(true)}
                    >
                        现场检查
                    </Button>
                </Space>
            }
        >
            {unavailable ? (
                <Text type="secondary" className="text-xs">
                    {report?.hint ?? report?.error ?? '尚无质量报告：执行一次 QuantDB 同步后自动生成，或点「现场检查」。'}
                </Text>
            ) : report?.ok ? (
                <div className="space-y-1">
                    <Alert
                        type="success"
                        showIcon
                        message="分区覆盖、行数、列集均未发现异常"
                    />
                    <Text type="secondary" className="text-xs">
                        检查于 {report.checked_at ?? '—'} · 预期最新 {report.expected_latest ?? '—'} · 日历{' '}
                        {report.calendar_source ?? '—'}
                        {report.relaxed_from_today ? `（今日 ${report.relaxed_from_today} 为交易日，按前一交易日判定）` : ''}
                    </Text>
                </div>
            ) : (
                <div className="space-y-2">
                    <Alert
                        type={errorCount > 0 ? 'error' : 'warning'}
                        showIcon
                        message={
                            errorCount > 0
                                ? `发现 ${errorCount} 个 error、${warningCount} 个 warning`
                                : `发现 ${warningCount} 个 warning`
                        }
                        description={
                            <Text type="secondary" className="text-xs">
                                检查于 {report.checked_at ?? '—'} · 预期最新 {report.expected_latest ?? '—'}
                                {report.blocked ? ' · 已按 block 模式跳过 PG/Qlib' : ''}
                            </Text>
                        }
                    />
                    <div className="space-y-1">
                        {issues.map((issue, idx) => (
                            <div
                                key={`${issue.dataset}-${issue.issue_type}-${idx}`}
                                className="flex items-start gap-2 rounded-md border border-slate-100 bg-slate-50/60 px-2.5 py-1.5"
                            >
                                <Tag color={SEVERITY_COLORS[issue.severity] ?? 'default'} className="m-0 shrink-0">
                                    {issue.severity}
                                </Tag>
                                <div className="min-w-0">
                                    <div className="flex flex-wrap items-center gap-1.5">
                                        <Text className="text-xs font-semibold">{issue.dataset}</Text>
                                        <Tag className="m-0" color="blue">
                                            {ISSUE_LABELS[issue.issue_type] ?? issue.issue_type}
                                        </Tag>
                                    </div>
                                    <Text type="secondary" className="text-[11px]">
                                        {issue.message}
                                    </Text>
                                </div>
                            </div>
                        ))}
                    </div>
                </div>
            )}

            {!unavailable && report?.datasets ? (
                <Tooltip title="每个数据集的本地最新分区与分区数">
                    <div className="mt-2 flex flex-wrap gap-1">
                        {Object.entries(report.datasets)
                            .filter(([, v]) => !v.skipped)
                            .slice(0, 12)
                            .map(([name, v]) => (
                                <Tag key={name} className="m-0 text-[11px]">
                                    {name} · {v.latest ?? '—'} · {v.partitions ?? 0} 分区
                                </Tag>
                            ))}
                    </div>
                </Tooltip>
            ) : null}
        </Card>
    );
}
