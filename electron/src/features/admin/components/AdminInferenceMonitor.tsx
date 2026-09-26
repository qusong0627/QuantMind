import React, { useCallback, useEffect, useState } from 'react';
import {
    Button,
    Card,
    Descriptions,
    Drawer,
    Empty,
    Pagination,
    Select,
    Space,
    Spin,
    Statistic,
    Tag,
    Typography,
    message,
} from 'antd';
import { ReloadOutlined, RightOutlined } from '@ant-design/icons';
import { adminService } from '../services/adminService';
import type { AdminInferenceDispatchItem, AdminInferenceMonitor as AdminInferenceMonitorData } from '../types';

const { Title, Text } = Typography;

const PAGE_SIZE = 20;

const STATUS_LABEL: Record<string, string> = {
    success: '成功',
    failed: '失败',
    skipped: '已跳过',
    running: '运行中',
};

const STATUS_COLOR: Record<string, string> = {
    success: 'green',
    failed: 'red',
    skipped: 'default',
    running: 'blue',
};

/** 调度记录表：固定列宽，表头与数据行共用同一模板，避免列位漂移 */
const DISPATCH_GRID =
    'grid w-full items-center gap-x-3 px-4 ' +
    'grid-cols-[72px_168px_minmax(140px,1.2fr)_88px_104px_minmax(160px,1.6fr)_24px]';

const emptyMonitor = (): AdminInferenceMonitorData => ({
    schedule: {
        enabled: false,
        cron: '工作日 06:30',
        timezone: 'Asia/Shanghai',
        next_run_at: null,
        task: 'engine.tasks.backfill_default_inference',
        description: '默认模型推理缺口补全（含历史空洞，同「一键补全至最新」）',
    },
    summary: {
        total: 0,
        success: 0,
        failed: 0,
        skipped: 0,
        running: 0,
        today_success: 0,
        today_failed: 0,
        today_skipped: 0,
        today_running: 0,
        latest_at: null,
    },
    settings: [],
    items: [],
});

function formatTime(value?: string | null): string {
    if (!value) return '-';
    const raw = value.trim().replace('T', ' ');
    const hasZone = /[zZ]|[+-]\d{2}:?\d{2}$/.test(raw);
    const parsed = new Date(hasZone ? raw : `${raw.replace(' ', 'T')}+08:00`);
    if (Number.isNaN(parsed.getTime())) return raw.slice(0, 19);
    const parts = new Intl.DateTimeFormat('sv-SE', {
        timeZone: 'Asia/Shanghai',
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
        hour12: false,
    }).formatToParts(parsed);
    const pick = (type: string) => parts.find((item) => item.type === type)?.value || '';
    return `${pick('year')}-${pick('month')}-${pick('day')} ${pick('hour')}:${pick('minute')}:${pick('second')}`;
}

function StatusTag({ status }: { status: string }) {
    return (
        <Tag className="!m-0 shrink-0" color={STATUS_COLOR[status] || 'default'}>
            {STATUS_LABEL[status] || status}
        </Tag>
    );
}

export const AdminInferenceMonitor: React.FC = () => {
    const [data, setData] = useState<AdminInferenceMonitorData>(emptyMonitor);
    const [loading, setLoading] = useState(false);
    const [status, setStatus] = useState<string | undefined>();
    const [page, setPage] = useState(1);
    const [detail, setDetail] = useState<AdminInferenceDispatchItem | null>(null);

    const loadAll = useCallback(async () => {
        setLoading(true);
        try {
            const payload = await adminService.getInferenceMonitor({ status, limit: 200 });
            setData(payload || emptyMonitor());
            setPage(1);
        } catch (error: any) {
            message.error(error?.response?.data?.detail || error?.message || '加载推理监控失败');
        } finally {
            setLoading(false);
        }
    }, [status]);

    useEffect(() => {
        void loadAll();
    }, [loadAll]);

    const items = data.items || [];
    const paged = items.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);
    const summary = data.summary;
    const schedule = data.schedule;

    const statCardClass =
        'text-center [&_.ant-card-body]:flex [&_.ant-card-body]:flex-col [&_.ant-card-body]:items-center [&_.ant-statistic-title]:text-center [&_.ant-statistic-content]:justify-center';

    return (
        <div className="mx-auto flex h-full min-h-0 w-full max-w-[1400px] flex-col gap-4 pb-4">
            <div className="flex shrink-0 items-start justify-between gap-4">
                <div>
                    <Title level={4} className="!mb-1">
                        推理监控
                    </Title>
                    <Text type="secondary">
                        默认模型推理缺口补全（Celery Beat 工作日 06:30）的成功、失败与跳过记录。
                    </Text>
                </div>
                <Space wrap>
                    <Select
                        allowClear
                        placeholder="状态"
                        className="w-28"
                        value={status}
                        onChange={setStatus}
                        options={[
                            { label: '成功', value: 'success' },
                            { label: '失败', value: 'failed' },
                            { label: '已跳过', value: 'skipped' },
                        ]}
                    />
                    <Button icon={<ReloadOutlined />} onClick={() => void loadAll()}>
                        刷新
                    </Button>
                </Space>
            </div>

            <div className="grid shrink-0 grid-cols-2 gap-3 lg:grid-cols-5">
                <Card size="small" className={statCardClass}>
                    <Statistic
                        title="调度开关"
                        value={schedule.enabled ? '已开启' : '已关闭'}
                        valueStyle={{ color: schedule.enabled ? '#16a34a' : '#64748b', fontSize: 20 }}
                    />
                    <div className="mt-1 text-xs text-slate-500">
                        下次 {formatTime(schedule.next_run_at)}
                    </div>
                </Card>
                <Card size="small" className={statCardClass}>
                    <Statistic title="今日成功" value={summary.today_success} valueStyle={{ color: '#16a34a' }} />
                </Card>
                <Card size="small" className={statCardClass}>
                    <Statistic title="今日失败" value={summary.today_failed} valueStyle={{ color: '#dc2626' }} />
                </Card>
                <Card size="small" className={statCardClass}>
                    <Statistic title="今日跳过" value={summary.today_skipped} />
                </Card>
                <Card size="small" className={statCardClass}>
                    <Statistic title="累计成功 / 失败" value={`${summary.success} / ${summary.failed}`} />
                    <div className="mt-1 text-xs text-slate-500">
                        最近 {formatTime(summary.latest_at)}
                        {(summary.running ?? 0) > 0 && (
                            <span className="ml-2 font-semibold text-amber-600">
                                运行中 {summary.running}（未结束=任务被杀）
                            </span>
                        )}
                    </div>
                </Card>
            </div>

            {data.settings.length > 0 && (
                <Card
                    size="small"
                    className="shrink-0 [&_.ant-card-head-title]:w-full [&_.ant-card-head-title]:text-center"
                    title={`已开启自动推理（${data.settings.length}）`}
                >
                    <div className="max-h-32 overflow-y-auto divide-y divide-slate-100">
                        {data.settings.map((item) => (
                            <div
                                key={`${item.tenant_id}-${item.user_id}-${item.model_id}`}
                                className="flex items-center justify-center gap-3 overflow-hidden py-2 text-center text-sm"
                            >
                                <span className="w-40 shrink-0 truncate font-medium">{item.model_id}</span>
                                <span className="w-20 shrink-0 text-slate-500">用户 {item.user_id}</span>
                                <span className="min-w-0 max-w-md truncate text-xs text-slate-400">
                                    {item.schedule_time ? `计划 ${item.schedule_time}` : '跟随全局窗口'}
                                    {item.last_run_id ? ` · 上次 ${item.last_run_id}` : ''}
                                </span>
                            </div>
                        ))}
                    </div>
                </Card>
            )}

            <Card
                size="small"
                className="flex min-h-0 flex-1 flex-col overflow-hidden [&_.ant-card-head]:shrink-0 [&_.ant-card-head-title]:w-full [&_.ant-card-head-title]:text-center [&_.ant-card-body]:flex [&_.ant-card-body]:min-h-0 [&_.ant-card-body]:flex-1 [&_.ant-card-body]:flex-col [&_.ant-card-body]:overflow-hidden [&_.ant-card-body]:p-0"
                title={`调度任务记录（${items.length}）`}
            >
                <div className="flex min-h-0 flex-1 flex-col">
                    <div className="min-h-0 flex-1 overflow-auto">
                        <Spin spinning={loading}>
                            {items.length === 0 ? (
                                <Empty
                                    className="py-10"
                                    image={Empty.PRESENTED_IMAGE_SIMPLE}
                                    description="暂无自动推理调度记录"
                                />
                            ) : (
                                <div className="min-w-[760px]">
                                    <div
                                        className={`${DISPATCH_GRID} sticky top-0 z-10 border-b border-slate-100 bg-slate-50 py-2 text-xs font-medium text-slate-500`}
                                    >
                                        <span>状态</span>
                                        <span>时间</span>
                                        <span className="truncate">模型 / 策略</span>
                                        <span>用户</span>
                                        <span>预测日</span>
                                        <span className="truncate">原因</span>
                                        <span aria-hidden />
                                    </div>
                                    <div className="divide-y divide-slate-100">
                                        {paged.map((item) => (
                                            <button
                                                key={item.id}
                                                type="button"
                                                className={`${DISPATCH_GRID} py-2.5 text-left hover:bg-slate-50`}
                                                onClick={() => setDetail(item)}
                                            >
                                                <span className="flex items-center">
                                                    <StatusTag status={item.status} />
                                                </span>
                                                <span className="truncate font-mono text-xs text-slate-500">
                                                    {formatTime(item.created_at)}
                                                </span>
                                                <span
                                                    className="truncate text-sm font-medium text-slate-800"
                                                    title={
                                                        item.model_id ||
                                                        item.strategy_id ||
                                                        '全局默认'
                                                    }
                                                >
                                                    {item.model_id ||
                                                        item.strategy_id ||
                                                        '全局默认'}
                                                </span>
                                                <span className="truncate text-xs text-slate-500">
                                                    {item.user_id || '-'}
                                                </span>
                                                <span className="truncate font-mono text-xs text-slate-500">
                                                    {item.prediction_trade_date || '-'}
                                                </span>
                                                <span
                                                    className="truncate text-xs text-slate-400"
                                                    title={
                                                        item.reason_detail ||
                                                        item.reason_label ||
                                                        item.reason_code ||
                                                        undefined
                                                    }
                                                >
                                                    {item.reason_label ||
                                                        item.reason_code ||
                                                        item.reason_detail ||
                                                        '—'}
                                                </span>
                                                <RightOutlined className="justify-self-end text-slate-400" />
                                            </button>
                                        ))}
                                    </div>
                                </div>
                            )}
                        </Spin>
                    </div>
                    {items.length > 0 && (
                        <div className="flex shrink-0 items-center justify-center border-t border-slate-100 px-4 py-3">
                            <Pagination
                                current={page}
                                pageSize={PAGE_SIZE}
                                total={items.length}
                                showSizeChanger={false}
                                showTotal={(total) => `共 ${total} 条`}
                                onChange={setPage}
                                size="small"
                            />
                        </div>
                    )}
                </div>
            </Card>

            <Drawer
                title="调度任务详情"
                open={Boolean(detail)}
                width={480}
                onClose={() => setDetail(null)}
                destroyOnHidden
            >
                {detail && (
                    <Descriptions column={1} size="small" bordered>
                        <Descriptions.Item label="状态">
                            <StatusTag status={detail.status} />
                        </Descriptions.Item>
                        <Descriptions.Item label="时间">{formatTime(detail.created_at)}</Descriptions.Item>
                        <Descriptions.Item label="触发来源">{detail.trigger_source || '-'}</Descriptions.Item>
                        <Descriptions.Item label="用户">{detail.user_id || '-'}</Descriptions.Item>
                        <Descriptions.Item label="租户">{detail.tenant_id || '-'}</Descriptions.Item>
                        <Descriptions.Item label="策略">{detail.strategy_id || '-'}</Descriptions.Item>
                        <Descriptions.Item label="模型">{detail.model_id || '-'}</Descriptions.Item>
                        <Descriptions.Item label="特征日">{detail.data_trade_date || '-'}</Descriptions.Item>
                        <Descriptions.Item label="预测日">{detail.prediction_trade_date || '-'}</Descriptions.Item>
                        <Descriptions.Item label="原因">
                            {detail.reason_label || detail.reason_code || '-'}
                        </Descriptions.Item>
                        <Descriptions.Item label="详情">
                            <span className="whitespace-pre-wrap break-all">{detail.reason_detail || '-'}</span>
                        </Descriptions.Item>
                        <Descriptions.Item label="Run ID">{detail.run_id || '-'}</Descriptions.Item>
                        <Descriptions.Item label="记录 ID">{detail.id}</Descriptions.Item>
                    </Descriptions>
                )}
            </Drawer>
        </div>
    );
};
