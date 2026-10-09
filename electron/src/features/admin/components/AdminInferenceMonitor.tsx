/**
 * 推理监控（2026-10-09 机构版改版）。
 *
 * 旧版是五张居中统计卡 + 无表头的居中流水行 —— 数据在但读不出「哪里坏了」。
 * 改版按值班台重排：
 * - 运行概况条：调度状态（含下次运行）、今日成败跳、累计失败率（>=30% 标红）；
 * - 14 天堆叠小柱：失败占比一眼可见（桶按上海日历日，缺日补零）；
 * - 调度记录表：真表头、左对齐、等宽时间/ID/模型，失败行整行左缘标红；
 * - 详情抽屉重排为「任务 / 数据窗口 / 结果」三段，Run ID 可一键复制。
 *
 * 状态筛选改为**客户端**切（服务端仍支持 status 参数，这里刻意不用）：
 * 筛掉失败后趋势图会失真，监控页的趋势必须永远基于全量已加载记录。
 */
import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { Button, Drawer, Empty, Pagination, Switch, Tooltip, message } from 'antd';
import { CopyOutlined, ReloadOutlined, RightOutlined } from '@ant-design/icons';
import { adminService } from '../services/adminService';
import type { AdminInferenceDispatchItem, AdminInferenceMonitor as AdminInferenceMonitorData } from '../types';
import { KpiCell, Panel, StatusDot, type DotTone } from './ui/AdminPrimitives';
import { buildDailyTrend } from './inferenceTrend';

const RECORD_LIMIT = 200;
const AUTO_REFRESH_MS = 30_000;
const TREND_DAYS = 14;

/** 表头与数据行共用同一列模板，避免两处列宽漂移。 */
const GRID = 'grid grid-cols-[76px_148px_minmax(160px,1.1fr)_92px_170px_minmax(150px,1.4fr)_16px] items-center gap-x-4 pl-3 pr-4';

const STATUS_META: Record<string, { label: string; tone: DotTone; text: string }> = {
    success: { label: '成功', tone: 'ok', text: 'text-emerald-700' },
    failed: { label: '失败', tone: 'bad', text: 'text-rose-700' },
    skipped: { label: '跳过', tone: 'idle', text: 'text-slate-500' },
};

const statusMeta = (status: string) =>
    STATUS_META[status] || { label: status, tone: 'info' as DotTone, text: 'text-slate-600' };

const emptyMonitor = (): AdminInferenceMonitorData => ({
    schedule: {
        enabled: false,
        cron: '工作日 00:00',
        timezone: 'Asia/Shanghai',
        next_run_at: null,
        task: 'engine.tasks.auto_inference_if_needed',
    },
    summary: {
        total: 0,
        success: 0,
        failed: 0,
        skipped: 0,
        today_success: 0,
        today_failed: 0,
        today_skipped: 0,
        latest_at: null,
    },
    settings: [],
    items: [],
});

/** 历史记录混着带时区（+08:00 / Z）与不带时区两种串；不带时区按上海墙钟解释。 */
function formatTime(value?: string | null): string {
    if (!value) return '—';
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

const SectionLabel: React.FC<{ children: React.ReactNode }> = ({ children }) => (
    <div className="mb-1.5 text-[10px] font-semibold uppercase tracking-[0.14em] text-slate-400">{children}</div>
);

const Kv: React.FC<{ k: string; v?: React.ReactNode; mono?: boolean; copyable?: boolean }> = ({
    k,
    v,
    mono = false,
    copyable = false,
}) => {
    const empty = v === undefined || v === null || v === '';
    const text = empty ? '—' : v;
    const onCopy = () => {
        if (typeof text !== 'string') return;
        if (!navigator.clipboard) {
            message.warning('当前环境不支持剪贴板复制');
            return;
        }
        void navigator.clipboard.writeText(text).then(() => message.success('已复制'));
    };
    return (
        <div className="grid grid-cols-[84px_minmax(0,1fr)] gap-x-3 py-1.5 text-[12px]">
            <span className="text-slate-400">{k}</span>
            <span className={`min-w-0 break-all text-slate-700 ${mono && !empty ? 'admin-num text-[11px]' : ''} ${empty ? 'text-slate-300' : ''}`}>
                {text}
                {copyable && !empty && (
                    <button
                        type="button"
                        onClick={onCopy}
                        className="ml-2 align-middle text-slate-300 transition-colors hover:text-slate-600"
                        title="复制"
                    >
                        <CopyOutlined className="text-[11px]" />
                    </button>
                )}
            </span>
        </div>
    );
};

export const AdminInferenceMonitor: React.FC = () => {
    const [data, setData] = useState<AdminInferenceMonitorData>(emptyMonitor);
    const [loading, setLoading] = useState(true);
    const [filter, setFilter] = useState<'all' | 'success' | 'failed' | 'skipped'>('all');
    const [page, setPage] = useState(1);
    const [pageSize, setPageSize] = useState(20);
    const [detail, setDetail] = useState<AdminInferenceDispatchItem | null>(null);
    const [autoRefresh, setAutoRefresh] = useState(false);

    const loadAll = useCallback(async (silent = false) => {
        if (!silent) setLoading(true);
        try {
            // 始终拉全量：趋势图与筛选计数都基于这一份，筛选只发生在前端
            const payload = await adminService.getInferenceMonitor({ limit: RECORD_LIMIT });
            setData(payload || emptyMonitor());
        } catch (error: any) {
            message.error(error?.response?.data?.detail || error?.message || '加载推理监控失败');
        } finally {
            if (!silent) setLoading(false);
        }
    }, []);

    useEffect(() => {
        void loadAll();
    }, [loadAll]);

    useEffect(() => {
        if (!autoRefresh) return;
        const timer = setInterval(() => {
            void loadAll(true);
        }, AUTO_REFRESH_MS);
        return () => clearInterval(timer);
    }, [autoRefresh, loadAll]);

    const items = data.items || [];
    const summary = data.summary;
    const schedule = data.schedule;

    const counts = useMemo(
        () => ({
            all: items.length,
            success: items.filter((i) => i.status === 'success').length,
            failed: items.filter((i) => i.status === 'failed').length,
            skipped: items.filter((i) => i.status === 'skipped').length,
        }),
        [items],
    );
    const filtered = useMemo(
        () => (filter === 'all' ? items : items.filter((i) => i.status === filter)),
        [items, filter],
    );
    const paged = filtered.slice((page - 1) * pageSize, page * pageSize);
    const trend = useMemo(() => buildDailyTrend(items, TREND_DAYS), [items]);
    const maxTrend = Math.max(1, ...trend.map((d) => d.total));

    const decided = summary.success + summary.failed;
    const failRate = decided > 0 ? summary.failed / decided : 0;
    const failTone = failRate >= 0.3 ? 'bad' : failRate >= 0.1 ? 'warn' : 'ok';

    const switchFilter = (next: typeof filter) => {
        setFilter(next);
        setPage(1);
    };

    return (
        <div className="mx-auto flex h-full min-h-0 w-full max-w-[1400px] flex-col gap-3 pb-2">
            <div className="flex shrink-0 items-start justify-between gap-4">
                <div>
                    <h2 className="text-[16px] font-semibold text-slate-800">推理监控</h2>
                    <p className="mt-0.5 text-[12px] text-slate-400">
                        自动推理调度（Celery Beat）的派发台账：成功、失败与跳过。
                    </p>
                </div>
                <div className="flex items-center gap-3 pt-1">
                    <label className="flex cursor-pointer items-center gap-1.5 text-[11px] text-slate-400">
                        <Switch size="small" checked={autoRefresh} onChange={setAutoRefresh} />
                        自动刷新 30s
                    </label>
                    <Button size="small" icon={<ReloadOutlined />} loading={loading} onClick={() => void loadAll()}>
                        刷新
                    </Button>
                </div>
            </div>

            {/* 运行概况条 */}
            <Panel className="shrink-0" bodyClassName="grid grid-cols-2 divide-y divide-slate-100 lg:grid-cols-12 lg:divide-x lg:divide-y-0">
                <div className="col-span-2 flex min-w-0 flex-col justify-center gap-1 px-4 py-3 lg:col-span-4">
                    <span className="flex items-center gap-1.5 text-[11px] font-medium text-slate-400">
                        <StatusDot tone={schedule.enabled ? 'ok' : 'idle'} pulse={schedule.enabled} />
                        自动推理调度 · {schedule.enabled ? '已开启' : '已关闭'}
                    </span>
                    <span className="admin-num text-[20px] font-semibold leading-none tracking-tight text-slate-800">
                        {schedule.next_run_at ? formatTime(schedule.next_run_at).slice(0, 16) : '—'}
                    </span>
                    <span className="truncate text-[11px] text-slate-400">
                        {schedule.cron} · {schedule.timezone} · 下次运行
                    </span>
                </div>
                <KpiCell className="lg:col-span-2" label="今日成功" value={summary.today_success} tone="ok" sub={`今日跳过 ${summary.today_skipped}`} />
                <KpiCell className="lg:col-span-2" label="今日失败" value={summary.today_failed} tone={summary.today_failed > 0 ? 'bad' : 'default'} sub="今日派发终态" />
                <KpiCell className="lg:col-span-2" label="累计成功" value={summary.success} sub={`失败 ${summary.failed} · 跳过 ${summary.skipped}`} />
                <KpiCell
                    className="lg:col-span-2"
                    label="累计失败率"
                    value={`${(failRate * 100).toFixed(1)}%`}
                    tone={failTone}
                    sub={`共 ${summary.total} 次派发`}
                />
            </Panel>

            {/* 趋势 + 订阅 */}
            <div className="grid shrink-0 grid-cols-1 gap-3 xl:grid-cols-[420px_minmax(0,1fr)]">
                <Panel
                    title="派发趋势"
                    sub={`最近 ${TREND_DAYS} 天`}
                    right={
                        <div className="flex items-center gap-3 text-[10px] text-slate-400">
                            <span className="flex items-center gap-1">
                                <StatusDot tone="ok" />
                                成功
                            </span>
                            <span className="flex items-center gap-1">
                                <StatusDot tone="bad" />
                                失败
                            </span>
                            <span className="flex items-center gap-1">
                                <StatusDot tone="idle" />
                                跳过
                            </span>
                        </div>
                    }
                >
                    <div className="flex h-[72px] items-end gap-[3px] px-4 pt-2">
                        {trend.map((d) => {
                            const seg = (n: number) => (n > 0 ? Math.max(2, Math.round((n / maxTrend) * 64)) : 0);
                            return (
                                <Tooltip
                                    key={d.date}
                                    title={
                                        <span className="admin-num text-[11px]">
                                            {d.date}
                                            <br />
                                            成功 {d.success} · 失败 {d.failed} · 跳过 {d.skipped}
                                        </span>
                                    }
                                >
                                    <div className="flex min-w-0 flex-1 cursor-default flex-col justify-end gap-[1px]">
                                        {d.failed > 0 && <div className="rounded-[2px] bg-rose-400" style={{ height: seg(d.failed) }} />}
                                        {d.skipped > 0 && <div className="rounded-[2px] bg-slate-300" style={{ height: seg(d.skipped) }} />}
                                        {d.success > 0 && <div className="rounded-[2px] bg-emerald-400" style={{ height: seg(d.success) }} />}
                                        {d.total === 0 && <div className="h-[2px] rounded bg-slate-100" />}
                                    </div>
                                </Tooltip>
                            );
                        })}
                    </div>
                    <div className="flex gap-[3px] px-4 pb-2 pt-1">
                        {trend.map((d, i) => (
                            <span key={d.date} className="admin-num min-w-0 flex-1 text-center text-[9px] text-slate-400">
                                {i % 2 === 1 && i !== trend.length - 1 ? '' : d.date.slice(5)}
                            </span>
                        ))}
                    </div>
                </Panel>

                <Panel title="自动推理订阅" sub={`已开启 ${data.settings.length}`}>
                    {data.settings.length === 0 ? (
                        <div className="px-4 py-4 text-[12px] text-slate-400">暂无模型开启自动推理</div>
                    ) : (
                        <div className="admin-dark-scrollbar max-h-[124px] overflow-y-auto">
                            <div className="grid grid-cols-[minmax(200px,1.6fr)_84px_96px_150px_minmax(130px,1fr)] items-center gap-x-3 border-b border-slate-100 bg-slate-50/70 px-4 py-1.5 text-[10px] font-medium text-slate-400">
                                <span>模型</span>
                                <span>用户</span>
                                <span>计划</span>
                                <span>下次运行</span>
                                <span>上次运行</span>
                            </div>
                            <div className="divide-y divide-slate-50">
                                {data.settings.map((item) => (
                                    <div
                                        key={`${item.tenant_id}-${item.user_id}-${item.model_id}`}
                                        className="grid grid-cols-[minmax(200px,1.6fr)_84px_96px_150px_minmax(130px,1fr)] items-center gap-x-3 px-4 py-1.5 text-[12px]"
                                    >
                                        <span className="admin-num truncate text-[11px] text-slate-700" title={item.model_id}>
                                            {item.model_id}
                                        </span>
                                        <span className="admin-num text-[11px] text-slate-500">{item.user_id}</span>
                                        <span className="admin-num text-[11px] text-slate-500">
                                            {item.schedule_time || '跟随全局'}
                                        </span>
                                        <span className="admin-num text-[11px] text-slate-500">
                                            {item.next_run_at ? formatTime(item.next_run_at).slice(0, 16) : '—'}
                                        </span>
                                        <span className="admin-num truncate text-[10px] text-slate-400" title={item.last_run_id || ''}>
                                            {item.last_run_id || '—'}
                                        </span>
                                    </div>
                                ))}
                            </div>
                        </div>
                    )}
                </Panel>
            </div>

            {/* 调度记录表 */}
            <Panel
                className="min-h-0 flex-1"
                title="调度记录"
                sub={`最近 ${items.length} 条 · 最近派发 ${formatTime(summary.latest_at)}`}
                right={
                    <div className="flex items-center gap-2">
                        <div className="flex items-center rounded-md border border-slate-200 bg-slate-50 p-0.5">
                            {(
                                [
                                    { key: 'all', label: '全部', n: counts.all },
                                    { key: 'success', label: '成功', n: counts.success },
                                    { key: 'failed', label: '失败', n: counts.failed },
                                    { key: 'skipped', label: '跳过', n: counts.skipped },
                                ] as const
                            ).map((tab) => (
                                <button
                                    key={tab.key}
                                    type="button"
                                    onClick={() => switchFilter(tab.key)}
                                    className={`h-6 rounded-[5px] px-2.5 text-[11px] font-medium transition-colors ${
                                        filter === tab.key
                                            ? 'bg-white text-slate-800 shadow-sm'
                                            : 'text-slate-500 hover:text-slate-700'
                                    }`}
                                >
                                    {tab.label}
                                    <span className="admin-num ml-1 text-[10px] text-slate-400">{tab.n}</span>
                                </button>
                            ))}
                        </div>
                    </div>
                }
                bodyClassName="flex min-h-0 flex-1 flex-col"
            >
                <div className={`${GRID} h-8 shrink-0 border-b border-slate-200 bg-slate-50/70 text-[10px] font-medium text-slate-400`}>
                    <span>状态</span>
                    <span>时间</span>
                    <span>模型</span>
                    <span>用户</span>
                    <span>特征日 → 预测日</span>
                    <span>原因</span>
                    <span />
                </div>

                <div className="min-h-0 flex-1 overflow-y-auto">
                    {loading && items.length === 0 ? (
                        <div>
                            {Array.from({ length: 8 }).map((_, i) => (
                                <div key={i} className="flex h-9 items-center border-b border-slate-50 px-4">
                                    <div className="h-2.5 w-full animate-pulse rounded bg-slate-100" />
                                </div>
                            ))}
                        </div>
                    ) : filtered.length === 0 ? (
                        <Empty className="py-10" image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无自动推理调度记录" />
                    ) : (
                        <div>
                            {paged.map((item) => {
                                const meta = statusMeta(item.status);
                                const model = item.model_id || item.strategy_id || '全局默认';
                                const failed = item.status === 'failed';
                                const reason = item.reason_label || item.reason_code || item.reason_detail || '—';
                                const window =
                                    item.data_trade_date || item.prediction_trade_date
                                        ? `${item.data_trade_date || '—'} → ${item.prediction_trade_date || '—'}`
                                        : '—';
                                return (
                                    <button
                                        key={item.id}
                                        type="button"
                                        onClick={() => setDetail(item)}
                                        className={`${GRID} h-9 w-full border-b border-b-slate-50 border-l-2 text-left transition-colors ${
                                            failed
                                                ? 'border-l-rose-400 bg-rose-50/40 hover:bg-rose-50/80'
                                                : 'border-l-transparent hover:bg-slate-50'
                                        }`}
                                    >
                                        <span className="flex items-center gap-1.5">
                                            <StatusDot tone={meta.tone} />
                                            <span className={`text-[12px] ${meta.text}`}>{meta.label}</span>
                                        </span>
                                        <span className="admin-num text-[11px] text-slate-500">{formatTime(item.created_at)}</span>
                                        <span
                                            className={`admin-num truncate text-[11px] ${item.model_id || item.strategy_id ? 'text-slate-700' : 'text-slate-400'}`}
                                            title={model}
                                        >
                                            {model}
                                        </span>
                                        <span className="admin-num text-[11px] text-slate-500">{item.user_id || '—'}</span>
                                        <span className="admin-num truncate text-[11px] text-slate-500" title={window}>
                                            {window}
                                        </span>
                                        <span className="truncate text-[11px] text-slate-400" title={reason}>
                                            {reason}
                                        </span>
                                        <RightOutlined className="text-[10px] text-slate-300" />
                                    </button>
                                );
                            })}
                        </div>
                    )}
                </div>

                {filtered.length > 0 && (
                    <div className="flex shrink-0 items-center justify-between border-t border-slate-100 px-4 py-2.5">
                        <span className="admin-num text-[11px] text-slate-400">共 {filtered.length} 条</span>
                        <Pagination
                            current={page}
                            pageSize={pageSize}
                            total={filtered.length}
                            size="small"
                            showSizeChanger
                            pageSizeOptions={[20, 50, 100]}
                            onChange={setPage}
                            onShowSizeChange={(_, size) => {
                                setPageSize(size);
                                setPage(1);
                            }}
                        />
                    </div>
                )}
            </Panel>

            <Drawer
                title="派发详情"
                open={Boolean(detail)}
                width={520}
                onClose={() => setDetail(null)}
                destroyOnHidden
                styles={{ body: { padding: 0 } }}
            >
                {detail &&
                    (() => {
                        const meta = statusMeta(detail.status);
                        return (
                            <div className="flex h-full flex-col">
                                <div className="flex items-center justify-between border-b border-slate-100 px-5 py-3.5">
                                    <span className="flex items-center gap-2">
                                        <StatusDot tone={meta.tone} pulse={detail.status === 'failed'} />
                                        <span className={`text-[13px] font-semibold ${meta.text}`}>{meta.label}</span>
                                    </span>
                                    <span className="admin-num text-[11px] text-slate-400">{formatTime(detail.created_at)}</span>
                                </div>
                                <div className="min-h-0 flex-1 space-y-5 overflow-y-auto px-5 py-4">
                                    <section>
                                        <SectionLabel>任务</SectionLabel>
                                        <Kv k="触发来源" v={detail.trigger_source} mono />
                                        <Kv k="模型" v={detail.model_id} mono />
                                        <Kv k="策略" v={detail.strategy_id} mono />
                                        <Kv k="用户" v={detail.user_id} mono />
                                        <Kv k="租户" v={detail.tenant_id} mono />
                                    </section>
                                    <section>
                                        <SectionLabel>数据窗口</SectionLabel>
                                        <Kv k="特征日" v={detail.data_trade_date} mono />
                                        <Kv k="预测日" v={detail.prediction_trade_date} mono />
                                    </section>
                                    <section>
                                        <SectionLabel>结果</SectionLabel>
                                        <Kv k="原因" v={detail.reason_label || detail.reason_code} />
                                        {detail.reason_detail && (
                                            <div className="mt-1 whitespace-pre-wrap break-all rounded-md border border-slate-100 bg-slate-50 px-3 py-2 text-[12px] leading-5 text-slate-600">
                                                {detail.reason_detail}
                                            </div>
                                        )}
                                        <Kv k="Run ID" v={detail.run_id} mono copyable />
                                        <Kv k="记录 ID" v={detail.id} mono />
                                    </section>
                                </div>
                            </div>
                        );
                    })()}
            </Drawer>
        </div>
    );
};
