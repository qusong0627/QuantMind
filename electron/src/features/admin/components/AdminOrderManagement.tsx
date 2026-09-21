import React, { useCallback, useEffect, useState } from 'react';
import { Button, Card, Descriptions, Drawer, Empty, Pagination, Select, Space, Spin, Tag, Typography, message } from 'antd';
import { ReloadOutlined, RightOutlined } from '@ant-design/icons';
import { adminService } from '../services/adminService';
import type { AdminOrderHistoryItem, AdminPlannedOrderItem } from '../types';
import { isLiveTradingEnabled } from '../../../config/tradingFlags';

const { Title, Text } = Typography;

const SOURCE_LABEL: Record<string, string> = {
    hosted: '策略托管',
    risk: '风控触发',
};

const KIND_LABEL: Record<string, string> = {
    rebalance_job: '调仓任务',
    hosted_task: '托管执行',
    schedule: '下次窗口',
};

const MODE_LABEL: Record<string, string> = {
    SIMULATION: '模拟盘',
    REAL: '实盘',
    BOTH: '模拟+实盘',
};

const STATUS_LABEL: Record<string, string> = {
    filled: '已成交',
    submitted: '已报',
    pending: '待执行',
    ready: '就绪',
    running: '执行中',
    queued: '排队中',
    scheduled: '已排期',
    rejected: '已拒绝',
    cancelled: '已取消',
    expired: '已过期',
    failed: '失败',
    skipped: '已跳过',
};

const STATUS_COLOR: Record<string, string> = {
    filled: 'green',
    submitted: 'blue',
    pending: 'processing',
    ready: 'blue',
    running: 'processing',
    queued: 'processing',
    scheduled: 'cyan',
    rejected: 'red',
    cancelled: 'default',
    expired: 'orange',
    failed: 'red',
    skipped: 'default',
};

const REMARK_FALLBACK: Record<string, string> = {
    hosted: '策略托管自动调仓',
    risk: '风控规则触发平仓',
};

const PAGE_SIZE = 20;

type DetailTarget =
    | { kind: 'history'; item: AdminOrderHistoryItem }
    | { kind: 'planned'; item: AdminPlannedOrderItem };

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

function formatQty(value?: number | null): string {
    if (value == null || Number.isNaN(Number(value))) return '-';
    return Number(value).toLocaleString('zh-CN');
}

function formatPrice(value?: number | null): string {
    if (value == null || Number.isNaN(Number(value))) return '-';
    return Number(value).toFixed(2);
}

function historyRemarks(item: AdminOrderHistoryItem): string {
    const text = String(item.remarks || '').trim();
    return text || REMARK_FALLBACK[item.source] || '自动交易';
}

function StatusTag({ status }: { status: string }) {
    return (
        <Tag className="!m-0 shrink-0" color={STATUS_COLOR[status] || 'default'}>
            {STATUS_LABEL[status] || status}
        </Tag>
    );
}

export const AdminOrderManagement: React.FC = () => {
    const [history, setHistory] = useState<AdminOrderHistoryItem[]>([]);
    const [planned, setPlanned] = useState<AdminPlannedOrderItem[]>([]);
    const [loading, setLoading] = useState(false);
    const [mode, setMode] = useState<string | undefined>();
    const [source, setSource] = useState<string | undefined>();
    const [detail, setDetail] = useState<DetailTarget | null>(null);
    const [page, setPage] = useState(1);

    const loadAll = useCallback(async () => {
        setLoading(true);
        try {
            const [historyRows, plannedRows] = await Promise.all([
                adminService.listAutoOrderHistory({ mode, source, limit: 80 }),
                adminService.listPlannedOrders(),
            ]);
            setHistory(historyRows || []);
            setPlanned(plannedRows || []);
            setPage(1);
        } catch (error: any) {
            message.error(error?.response?.data?.detail || error?.message || '加载订单失败');
        } finally {
            setLoading(false);
        }
    }, [mode, source]);

    useEffect(() => {
        void loadAll();
    }, [loadAll]);

    const pagedHistory = history.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);

    return (
        <div className="flex h-full min-h-0 flex-col gap-4 pb-4">
            <div className="flex shrink-0 items-start justify-between gap-4">
                <div>
                    <Title level={4} className="!mb-1">
                        订单管理
                    </Title>
                    <Text type="secondary">策略托管与风控自动单；点击列表项查看完整备注与字段。</Text>
                </div>
                <Space wrap>
                    <Select
                        allowClear
                        placeholder="模式"
                        className="w-28"
                        value={mode}
                        onChange={setMode}
                        options={[
                            { label: '模拟盘', value: 'SIMULATION' },
                            // 实盘关闭时不提供该筛选项；MODE_LABEL 里的 'REAL' 保留，
                            // 存量实盘单仍要显示成「实盘」而不是错标成别的
                            ...(isLiveTradingEnabled() ? [{ label: '实盘', value: 'REAL' }] : []),
                        ]}
                    />
                    <Select
                        allowClear
                        placeholder="来源"
                        className="w-28"
                        value={source}
                        onChange={setSource}
                        options={[
                            { label: '策略托管', value: 'hosted' },
                            { label: '风控触发', value: 'risk' },
                        ]}
                    />
                    <Button icon={<ReloadOutlined />} onClick={() => void loadAll()}>
                        刷新
                    </Button>
                </Space>
            </div>

            <Card size="small" className="shrink-0" title={`未来计划交易（${planned.length}）`}>
                {planned.length === 0 ? (
                    <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无待执行计划或下次托管窗口" />
                ) : (
                    <div className="max-h-56 overflow-x-hidden overflow-y-auto divide-y divide-slate-100">
                        {planned.map((item) => (
                            <button
                                key={item.id}
                                type="button"
                                className="flex w-full items-center gap-3 overflow-hidden px-1 py-2.5 text-left hover:bg-slate-50"
                                onClick={() => setDetail({ kind: 'planned', item })}
                            >
                                <span className="w-40 shrink-0 text-xs text-slate-500">{formatTime(item.planned_at)}</span>
                                <Tag className="!m-0 shrink-0">{KIND_LABEL[item.kind] || item.kind}</Tag>
                                <span className="w-16 shrink-0 text-xs text-slate-500">
                                    {MODE_LABEL[item.mode] || item.mode}
                                </span>
                                <StatusTag status={item.status} />
                                <span className="min-w-0 flex-1 truncate text-sm text-slate-800">
                                    {item.title}
                                    {item.strategy_id ? ` · ${item.strategy_id}` : ''}
                                    {` · 用户 ${item.user_id}`}
                                </span>
                                <RightOutlined className="shrink-0 text-slate-400" />
                            </button>
                        ))}
                    </div>
                )}
            </Card>

            <Card
                size="small"
                className="flex min-h-0 flex-1 flex-col overflow-hidden [&_.ant-card-head]:shrink-0 [&_.ant-card-body]:flex [&_.ant-card-body]:min-h-0 [&_.ant-card-body]:flex-1 [&_.ant-card-body]:flex-col [&_.ant-card-body]:overflow-hidden [&_.ant-card-body]:p-0"
                title={`历史自动交易（${history.length}）`}
            >
                <div className="flex min-h-0 flex-1 flex-col">
                    <div className="min-h-0 flex-1 overflow-y-auto">
                        <Spin spinning={loading}>
                            {history.length === 0 ? (
                                <Empty className="py-10" image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无自动交易记录" />
                            ) : (
                                <div className="divide-y divide-slate-100">
                                    {pagedHistory.map((item) => {
                                        const isSell = item.side === 'SELL';
                                        const remarks = historyRemarks(item);
                                        return (
                                            <button
                                                key={item.id}
                                                type="button"
                                                className="flex w-full items-center gap-3 overflow-hidden px-4 py-2.5 text-left hover:bg-slate-50"
                                                onClick={() => setDetail({ kind: 'history', item })}
                                            >
                                                <span
                                                    className={`w-8 shrink-0 text-sm font-semibold ${
                                                        isSell ? 'text-emerald-600' : 'text-rose-600'
                                                    }`}
                                                >
                                                    {isSell ? '卖' : '买'}
                                                </span>
                                                <span className="w-28 shrink-0 font-medium text-slate-800">
                                                    {item.symbol}
                                                </span>
                                                <span className="w-24 shrink-0 tabular-nums text-slate-600">
                                                    {formatQty(item.quantity)}股
                                                </span>
                                                <span className="w-16 shrink-0 tabular-nums text-slate-600">
                                                    {formatPrice(item.average_price ?? item.price)}
                                                </span>
                                                <Tag
                                                    className="!m-0 shrink-0"
                                                    color={item.source === 'risk' ? 'red' : 'blue'}
                                                >
                                                    {SOURCE_LABEL[item.source] || item.source}
                                                </Tag>
                                                <span className="w-14 shrink-0 text-xs text-slate-500">
                                                    {MODE_LABEL[item.mode] || item.mode}
                                                </span>
                                                <StatusTag status={item.status} />
                                                <span className="w-40 shrink-0 text-xs text-slate-500">
                                                    {formatTime(item.created_at)}
                                                </span>
                                                <span className="min-w-0 flex-1 truncate text-xs text-slate-400">
                                                    用户 {item.user_id}
                                                    {item.strategy_id ? ` · ${item.strategy_id}` : ''} · {remarks}
                                                </span>
                                                <RightOutlined className="shrink-0 text-slate-400" />
                                            </button>
                                        );
                                    })}
                                </div>
                            )}
                        </Spin>
                    </div>
                    {history.length > 0 && (
                        <div className="flex shrink-0 items-center justify-end border-t border-slate-100 px-4 py-3">
                            <Pagination
                                current={page}
                                pageSize={PAGE_SIZE}
                                total={history.length}
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
                title={detail?.kind === 'planned' ? '计划交易详情' : '历史成交详情'}
                open={Boolean(detail)}
                width={480}
                onClose={() => setDetail(null)}
                destroyOnHidden
            >
                {detail?.kind === 'history' && (
                    <Descriptions column={1} size="small" bordered>
                        <Descriptions.Item label="时间">{formatTime(detail.item.created_at)}</Descriptions.Item>
                        <Descriptions.Item label="成交时间">{formatTime(detail.item.filled_at)}</Descriptions.Item>
                        <Descriptions.Item label="来源">
                            <Tag color={detail.item.source === 'risk' ? 'red' : 'blue'}>
                                {SOURCE_LABEL[detail.item.source] || detail.item.source}
                            </Tag>
                        </Descriptions.Item>
                        <Descriptions.Item label="模式">
                            {MODE_LABEL[detail.item.mode] || detail.item.mode}
                        </Descriptions.Item>
                        <Descriptions.Item label="状态">
                            <StatusTag status={detail.item.status} />
                        </Descriptions.Item>
                        <Descriptions.Item label="用户">{detail.item.user_id}</Descriptions.Item>
                        <Descriptions.Item label="租户">{detail.item.tenant_id || '-'}</Descriptions.Item>
                        <Descriptions.Item label="策略">{detail.item.strategy_id || '-'}</Descriptions.Item>
                        <Descriptions.Item label="标的">{detail.item.symbol}</Descriptions.Item>
                        <Descriptions.Item label="方向">{detail.item.side === 'SELL' ? '卖出' : '买入'}</Descriptions.Item>
                        <Descriptions.Item label="数量">{formatQty(detail.item.quantity)}</Descriptions.Item>
                        <Descriptions.Item label="委托价">{formatPrice(detail.item.price)}</Descriptions.Item>
                        <Descriptions.Item label="成交价">
                            {formatPrice(detail.item.average_price ?? detail.item.price)}
                        </Descriptions.Item>
                        <Descriptions.Item label="备注">
                            <span className="whitespace-pre-wrap break-all">{historyRemarks(detail.item)}</span>
                        </Descriptions.Item>
                        <Descriptions.Item label="单号">{detail.item.id}</Descriptions.Item>
                    </Descriptions>
                )}
                {detail?.kind === 'planned' && (
                    <Descriptions column={1} size="small" bordered>
                        <Descriptions.Item label="计划时间">{formatTime(detail.item.planned_at)}</Descriptions.Item>
                        <Descriptions.Item label="窗口结束">{formatTime(detail.item.window_end_at)}</Descriptions.Item>
                        <Descriptions.Item label="类型">
                            <Tag>{KIND_LABEL[detail.item.kind] || detail.item.kind}</Tag>
                        </Descriptions.Item>
                        <Descriptions.Item label="模式">
                            {MODE_LABEL[detail.item.mode] || detail.item.mode}
                        </Descriptions.Item>
                        <Descriptions.Item label="状态">
                            <StatusTag status={detail.item.status} />
                        </Descriptions.Item>
                        <Descriptions.Item label="用户">{detail.item.user_id}</Descriptions.Item>
                        <Descriptions.Item label="租户">{detail.item.tenant_id || '-'}</Descriptions.Item>
                        <Descriptions.Item label="策略">{detail.item.strategy_id || '-'}</Descriptions.Item>
                        <Descriptions.Item label="阶段">{detail.item.phase || '-'}</Descriptions.Item>
                        <Descriptions.Item label="交易日">{detail.item.trade_date || '-'}</Descriptions.Item>
                        <Descriptions.Item label="说明">{detail.item.title}</Descriptions.Item>
                        <Descriptions.Item label="详情">
                            <span className="whitespace-pre-wrap break-all">{detail.item.detail || '-'}</span>
                        </Descriptions.Item>
                        <Descriptions.Item label="单号">{detail.item.id}</Descriptions.Item>
                    </Descriptions>
                )}
            </Drawer>
        </div>
    );
};
