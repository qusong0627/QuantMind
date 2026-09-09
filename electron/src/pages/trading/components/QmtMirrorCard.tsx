/** 大 QMT 真单镜像控制卡：热开关 / 急停 / 限额 / 白黑名单 / 队列 + 虚拟 vs 真单对账。 */
import React, { useCallback, useEffect, useState } from 'react';
import {
    Alert,
    Button,
    DatePicker,
    InputNumber,
    Popconfirm,
    Select,
    Table,
    Tag,
    message,
} from 'antd';
import { AlertTriangle, Power, RefreshCw, Send, ShieldAlert } from 'lucide-react';
import dayjs from 'dayjs';
import { authService } from '../../../features/auth/services/authService';
import { SERVICE_URLS } from '../../../config/services';

const apiBase = `${SERVICE_URLS.API_GATEWAY}/api/v1`;
const POLL_MS = 10000;

interface MirrorConfigView {
    max_order_value: number;
    max_daily_value: number;
    max_daily_symbols: number;
    max_daily_orders: number;
    max_slippage_pct: number;
    max_consecutive_rejects: number;
    queue_outside_hours: boolean;
    markets: string[];
}

interface MirrorStatus {
    enabled: boolean;
    env_enabled: boolean;
    kill_switch: boolean;
    whitelist: string[];
    blacklist: string[];
    config: MirrorConfigView;
    quota: { date: string; daily_value: number; daily_orders: number; daily_symbols: number };
    queue_length: number;
    consecutive_rejects: number;
    trading_time: boolean;
    broker_selected: string;
    real_trading_ready: boolean;
    blocked_reason: string;
}

interface ReconcileItem {
    client_order_id: string;
    symbol: string;
    side: string;
    quantity: number;
    virtual_price: number;
    virtual_fee: number;
    mirrored: boolean;
    note?: string;
    real_order_id?: string;
    real_price?: number;
    real_fee?: number;
    real_status?: string;
    real_filled_quantity?: number;
    slippage?: number;
    slippage_pct?: number;
    fee_diff?: number;
    real_message?: string;
}

interface ReconcileData {
    date: string;
    summary: {
        virtual_orders: number;
        mirrored: number;
        filled: number;
        rejected_or_cancelled: number;
        avg_abs_slippage: number;
        fee_diff_total: number;
    };
    items: ReconcileItem[];
}

const BLOCKED_REASON_LABEL: Record<string, string> = {
    kill_switch: '急停已置位',
    disabled: '镜像开关未开启',
    no_whitelist: '白名单为空（未点名任何策略）',
    not_ready: '实盘通道未就绪（券商未选 qmt_exec 或 ENABLE_REAL_TRADING=false）',
    outside_hours: '非交易时段',
};

const fmtMoney = (value: number | undefined | null) => `¥${Number(value ?? 0).toFixed(2)}`;

const QmtMirrorCard: React.FC = () => {
    const [status, setStatus] = useState<MirrorStatus | null>(null);
    const [loading, setLoading] = useState(false);
    const [saving, setSaving] = useState(false);
    const [limits, setLimits] = useState<Partial<MirrorConfigView>>({});
    const [whitelist, setWhitelist] = useState<string[]>([]);
    const [blacklist, setBlacklist] = useState<string[]>([]);
    const [reconDate, setReconDate] = useState<string>(dayjs().format('YYYY-MM-DD'));
    const [recon, setRecon] = useState<ReconcileData | null>(null);
    const [reconLoading, setReconLoading] = useState(false);

    const authHeaders = () => {
        const token = authService.getAccessToken();
        return {
            'Content-Type': 'application/json',
            ...(token ? { Authorization: `Bearer ${token}` } : {}),
        };
    };

    const applyStatus = (data: MirrorStatus) => {
        setStatus(data);
        setLimits(data.config ?? {});
        setWhitelist(data.whitelist ?? []);
        setBlacklist(data.blacklist ?? []);
    };

    const load = useCallback(async (silent = false) => {
        if (!silent) setLoading(true);
        try {
            const resp = await fetch(`${apiBase}/qmt-mirror/status`, { headers: authHeaders() });
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            applyStatus(await resp.json());
        } catch (e: unknown) {
            if (!silent) message.error(e instanceof Error ? e.message : '读取镜像状态失败');
        } finally {
            if (!silent) setLoading(false);
        }
    }, []);

    useEffect(() => {
        void load();
        const timer = setInterval(() => void load(true), POLL_MS);
        return () => clearInterval(timer);
    }, [load]);

    const loadReconcile = useCallback(async (date: string) => {
        setReconLoading(true);
        try {
            const resp = await fetch(
                `${apiBase}/qmt-mirror/reconcile?date=${date}&limit=200`,
                { headers: authHeaders() }
            );
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            setRecon(await resp.json());
        } catch (e: unknown) {
            message.error(e instanceof Error ? e.message : '读取对账数据失败');
            setRecon(null);
        } finally {
            setReconLoading(false);
        }
    }, []);

    useEffect(() => {
        void loadReconcile(reconDate);
    }, [loadReconcile, reconDate]);

    const putJson = async (path: string, body: unknown, okText: string) => {
        setSaving(true);
        try {
            const resp = await fetch(`${apiBase}${path}`, {
                method: 'PUT',
                headers: authHeaders(),
                body: JSON.stringify(body),
            });
            const data = await resp.json().catch(() => null);
            if (!resp.ok) throw new Error(data?.detail || `HTTP ${resp.status}`);
            if (data && typeof data.enabled === 'boolean') applyStatus(data as MirrorStatus);
            else await load(true);
            message.success(okText);
        } catch (e: unknown) {
            message.error(e instanceof Error ? e.message : '操作失败');
        } finally {
            setSaving(false);
        }
    };

    const drainQueue = async () => {
        setSaving(true);
        try {
            const resp = await fetch(`${apiBase}/qmt-mirror/drain?limit=20`, {
                method: 'POST',
                headers: authHeaders(),
            });
            const data = await resp.json().catch(() => null);
            if (!resp.ok) throw new Error(data?.detail || `HTTP ${resp.status}`);
            const submitted = Number(data?.submitted ?? 0);
            const failed = Number(data?.failed ?? 0);
            const dropped = Number(data?.dropped ?? 0);
            message.success(
                `已补交 ${submitted} 笔` +
                    (failed ? `，失败 ${failed} 笔` : '') +
                    (dropped ? `，复核不过丢弃 ${dropped} 笔` : '')
            );
            await load(true);
        } catch (e: unknown) {
            message.error(e instanceof Error ? e.message : '补交失败');
        } finally {
            setSaving(false);
        }
    };

    const blockedLabel = status?.blocked_reason
        ? BLOCKED_REASON_LABEL[status.blocked_reason] || status.blocked_reason
        : '';

    const quota = status?.quota;
    const cfg = status?.config;

    const columns = [
        { title: '标的', dataIndex: 'symbol', width: 100 },
        {
            title: '方向',
            dataIndex: 'side',
            width: 70,
            render: (side: string) =>
                side === 'BUY' ? (
                    <Tag color="red" className="!mr-0">买</Tag>
                ) : (
                    <Tag color="green" className="!mr-0">卖</Tag>
                ),
        },
        { title: '数量', dataIndex: 'quantity', width: 80 },
        {
            title: '虚拟成交价',
            dataIndex: 'virtual_price',
            width: 110,
            render: (v: number) => Number(v ?? 0).toFixed(3),
        },
        {
            title: '真实成交价',
            dataIndex: 'real_price',
            width: 110,
            render: (v: number | undefined, row: ReconcileItem) =>
                row.mirrored ? Number(v ?? 0).toFixed(3) : <span className="text-slate-400">—</span>,
        },
        {
            title: '滑点',
            dataIndex: 'slippage',
            width: 110,
            render: (v: number | undefined, row: ReconcileItem) => {
                if (!row.mirrored || v === undefined) return <span className="text-slate-400">—</span>;
                const cls = v > 0 ? 'text-red-600' : v < 0 ? 'text-green-600' : 'text-slate-500';
                return (
                    <span className={cls}>
                        {v > 0 ? '+' : ''}
                        {v.toFixed(3)}
                        {row.slippage_pct !== undefined ? ` (${(row.slippage_pct * 100).toFixed(2)}%)` : ''}
                    </span>
                );
            },
        },
        {
            title: '费用差',
            dataIndex: 'fee_diff',
            width: 90,
            render: (v: number | undefined, row: ReconcileItem) =>
                row.mirrored ? (
                    <span className={Number(v) > 0 ? 'text-red-600' : 'text-green-600'}>
                        {Number(v ?? 0).toFixed(2)}
                    </span>
                ) : (
                    <span className="text-slate-400">—</span>
                ),
        },
        {
            title: '真单状态',
            dataIndex: 'real_status',
            width: 130,
            render: (v: string | undefined, row: ReconcileItem) => {
                if (!row.mirrored) {
                    return <span className="text-[11px] text-slate-400">{row.note}</span>;
                }
                const color =
                    v === 'FILLED' ? 'green' : v === 'PARTIALLY_FILLED' ? 'orange' : v === 'REJECTED' ? 'red' : 'default';
                return (
                    <Tag color={color} className="!mr-0">
                        {v}
                        {row.real_message ? ` · ${row.real_message}` : ''}
                    </Tag>
                );
            },
        },
    ];

    return (
        <div className="space-y-4">
            <div className="flex items-start justify-between gap-3">
                <div>
                    <div className="text-sm font-bold text-gray-900 flex items-center gap-1.5">
                        <ShieldAlert className="text-rose-500" size={16} /> 大 QMT 真单镜像
                    </div>
                    <p className="text-xs text-gray-500 mt-1">
                        模拟盘虚拟成交后，按下列限额把同向同量委托同步到大 QMT 真实账户（双轨并行，虚拟账本不受影响）。
                        仅白名单点名的策略会被镜像。
                    </p>
                </div>
                <Button size="small" icon={<RefreshCw />} loading={loading} onClick={() => void load()}>
                    刷新
                </Button>
            </div>

            {status && (
                <div className="flex flex-wrap items-center gap-2 text-[11px]">
                    <Tag color={status.enabled ? 'green' : 'default'} className="!mr-0">
                        {status.enabled ? '镜像已开启' : '镜像未开启'}
                    </Tag>
                    {status.kill_switch && (
                        <Tag color="red" className="!mr-0">
                            急停中
                        </Tag>
                    )}
                    <Tag color={status.real_trading_ready ? 'blue' : 'default'} className="!mr-0">
                        通道 {status.real_trading_ready ? '就绪' : '未就绪'}
                    </Tag>
                    <Tag className="!mr-0">券商 {status.broker_selected || '未选择'}</Tag>
                    <Tag color={status.trading_time ? 'green' : 'default'} className="!mr-0">
                        {status.trading_time ? '交易时段' : '非交易时段'}
                    </Tag>
                    {status.consecutive_rejects > 0 && (
                        <Tag color="orange" className="!mr-0">
                            连续拒单 {status.consecutive_rejects}
                        </Tag>
                    )}
                    {blockedLabel && (
                        <span className="text-slate-500">
                            <AlertTriangle size={11} className="inline -mt-0.5 mr-1 text-amber-500" />
                            {blockedLabel}
                        </span>
                    )}
                </div>
            )}

            {status && !status.real_trading_ready && (
                <Alert
                    type="warning"
                    showIcon
                    className="!text-xs"
                    message="实盘通道未就绪"
                    description="需要 ENABLE_REAL_TRADING=true，且「券商实盘接入」中把 A 股券商选为「大 QMT(执行端)」并测试连接通过，镜像单才会真正下发。"
                />
            )}

            {/* 开关与急停 */}
            <div className="rounded-2xl border border-gray-200 bg-gray-50/60 p-4 flex flex-wrap items-center justify-between gap-3">
                <div className="flex items-center gap-3">
                    <span className="text-xs font-bold text-gray-700">镜像开关</span>
                    <Button
                        size="small"
                        type={status?.enabled ? 'primary' : 'default'}
                        icon={<Power size={13} />}
                        loading={saving}
                        onClick={() => void putJson('/qmt-mirror/enabled', { enabled: !status?.enabled }, status?.enabled ? '已关闭镜像' : '已开启镜像')}
                    >
                        {status?.enabled ? '关闭' : '开启'}
                    </Button>
                    {status?.env_enabled && (
                        <span className="text-[11px] text-slate-400">env 基线已开启（SIMULATION_MIRROR_TO_REAL）</span>
                    )}
                </div>
                <Popconfirm
                    title="确认急停？"
                    description="置位后立即停止所有镜像真单，虚拟账本不受影响。"
                    okText="急停"
                    okButtonProps={{ danger: true }}
                    cancelText="取消"
                    onConfirm={() => void putJson('/qmt-mirror/kill', { on: true }, '已急停')}
                >
                    <Button danger size="small" disabled={saving || !status?.enabled}>
                        急停
                    </Button>
                </Popconfirm>
                {status?.kill_switch && (
                    <Button size="small" loading={saving} onClick={() => void putJson('/qmt-mirror/kill', { on: false }, '已解除急停')}>
                        解除急停
                    </Button>
                )}
            </div>

            {/* 限额 */}
            <div className="rounded-2xl border border-gray-200 p-4 space-y-3">
                <div className="text-xs font-bold text-gray-700">限额（覆盖 env 基线）</div>
                <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                    {(
                        [
                            ['max_order_value', '单笔上限(元)'],
                            ['max_daily_value', '单日累计(元)'],
                            ['max_daily_symbols', '单日标的数'],
                            ['max_daily_orders', '单日笔数'],
                        ] as const
                    ).map(([key, label]) => (
                        <div key={key}>
                            <div className="text-[11px] text-gray-500 mb-1">{label}</div>
                            <InputNumber
                                className="w-full"
                                min={1}
                                value={limits[key] as number | undefined}
                                onChange={(value) => setLimits({ ...limits, [key]: value ?? undefined })}
                            />
                        </div>
                    ))}
                </div>
                <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                    <div>
                        <div className="text-[11px] text-gray-500 mb-1">滑点上限</div>
                        <InputNumber
                            className="w-full"
                            min={0.001}
                            max={0.2}
                            step={0.005}
                            value={limits.max_slippage_pct}
                            onChange={(value) =>
                                setLimits({ ...limits, max_slippage_pct: value ?? undefined })
                            }
                        />
                    </div>
                    <div>
                        <div className="text-[11px] text-gray-500 mb-1">连续拒单熔断</div>
                        <InputNumber
                            className="w-full"
                            min={0}
                            value={limits.max_consecutive_rejects}
                            onChange={(value) =>
                                setLimits({ ...limits, max_consecutive_rejects: value ?? undefined })
                            }
                        />
                    </div>
                    <div className="md:col-span-2 flex items-end">
                        <Button
                            type="primary"
                            size="small"
                            loading={saving}
                            onClick={() =>
                                void putJson(
                                    '/qmt-mirror/config',
                                    {
                                        max_order_value: limits.max_order_value,
                                        max_daily_value: limits.max_daily_value,
                                        max_daily_symbols: limits.max_daily_symbols,
                                        max_daily_orders: limits.max_daily_orders,
                                        max_slippage_pct: limits.max_slippage_pct,
                                        max_consecutive_rejects: limits.max_consecutive_rejects,
                                    },
                                    '限额已更新'
                                )
                            }
                        >
                            保存限额
                        </Button>
                    </div>
                </div>
                {quota && cfg && (
                    <div className="text-[11px] text-gray-500">
                        当日（{quota.date}）已用：{fmtMoney(quota.daily_value)} / {fmtMoney(cfg.max_daily_value)}，
                        委托 {quota.daily_orders} / {cfg.max_daily_orders} 笔，
                        标的 {quota.daily_symbols} / {cfg.max_daily_symbols} 只，
                        队列 {status?.queue_length ?? 0} 笔
                        <Button
                            size="small"
                            type="link"
                            className="!px-2"
                            icon={<Send size={12} />}
                            loading={saving}
                            onClick={drainQueue}
                        >
                            立即补交
                        </Button>
                    </div>
                )}
            </div>

            {/* 名单 */}
            <div className="rounded-2xl border border-gray-200 p-4 space-y-3">
                <div className="text-xs font-bold text-gray-700">镜像名单</div>
                <div>
                    <div className="text-[11px] text-gray-500 mb-1">
                        白名单（tenant / tenant:user / tenant:user:strategy，留空=全部关闭）
                    </div>
                    <Select
                        mode="tags"
                        className="w-full"
                        placeholder="如 default:1:42"
                        value={whitelist}
                        onChange={(value) => setWhitelist(value)}
                        tokenSeparators={[',', ' ']}
                    />
                </div>
                <div>
                    <div className="text-[11px] text-gray-500 mb-1">黑名单标的（前缀式，如 SH600519）</div>
                    <Select
                        mode="tags"
                        className="w-full"
                        placeholder="如 SH600519"
                        value={blacklist}
                        onChange={(value) => setBlacklist(value)}
                        tokenSeparators={[',', ' ']}
                    />
                </div>
                <Button
                    type="primary"
                    size="small"
                    loading={saving}
                    onClick={() =>
                        void putJson(
                            '/qmt-mirror/lists',
                            { whitelist, blacklist },
                            '名单已更新'
                        )
                    }
                >
                    保存名单
                </Button>
            </div>

            {/* 对账 */}
            <div className="rounded-2xl border border-gray-200 p-4 space-y-3">
                <div className="flex flex-wrap items-center justify-between gap-2">
                    <div className="text-xs font-bold text-gray-700">虚拟 vs 真单对账</div>
                    <div className="flex items-center gap-2">
                        <DatePicker
                            size="small"
                            value={dayjs(reconDate)}
                            onChange={(value) =>
                                setReconDate(value ? value.format('YYYY-MM-DD') : dayjs().format('YYYY-MM-DD'))
                            }
                            allowClear={false}
                        />
                        <Button
                            size="small"
                            icon={<RefreshCw size={12} />}
                            loading={reconLoading}
                            onClick={() => void loadReconcile(reconDate)}
                        >
                            刷新
                        </Button>
                    </div>
                </div>
                {recon && (
                    <div className="flex flex-wrap gap-3 text-[11px] text-gray-600">
                        <span>虚拟委托 {recon.summary.virtual_orders}</span>
                        <span>已镜像 {recon.summary.mirrored}</span>
                        <span>成交 {recon.summary.filled}</span>
                        <span>拒单/撤单 {recon.summary.rejected_or_cancelled}</span>
                        <span>平均滑点 {Number(recon.summary.avg_abs_slippage ?? 0).toFixed(3)}</span>
                        <span>费用差合计 {Number(recon.summary.fee_diff_total ?? 0).toFixed(2)}</span>
                    </div>
                )}
                <Table<ReconcileItem>
                    size="small"
                    rowKey={(row) => `${row.client_order_id}-${row.symbol}-${row.side}`}
                    loading={reconLoading}
                    dataSource={recon?.items ?? []}
                    columns={columns}
                    pagination={{ pageSize: 10, size: 'small' }}
                    scroll={{ x: 900 }}
                />
            </div>
        </div>
    );
};

export default QmtMirrorCard;
