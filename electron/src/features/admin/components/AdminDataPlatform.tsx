import React, { useEffect, useMemo, useState } from 'react';
import {
    Alert,
    Badge,
    Button,
    Card,
    Col,
    Empty,
    Input,
    Modal,
    Row,
    Segmented,
    Select,
    Space,
    Spin,
    Statistic,
    Switch,
    Table,
    Tabs,
    Tag,
    Tooltip,
    Typography,
    message,
} from 'antd';
import {
    ApiOutlined,
    CloudServerOutlined,
    ReloadOutlined,
    SyncOutlined,
    WarningFilled,
    CheckCircleFilled,
    CloseCircleFilled,
} from '@ant-design/icons';
import dayjs from 'dayjs';
import {
    dataPlatformService,
    FieldCoverageRow,
    FreshnessItem,
    HealthCell,
    HealthMatrix,
    OnlineStatusItem,
    QualityAlert,
    SourceSummary,
} from '../services/dataPlatformService';

const { Title, Text } = Typography;

type Market = 'A' | 'HK' | 'US';

/* ----------------------------- helpers ----------------------------- */
const formatTime = (s?: string | null) => (s ? dayjs(s).format('MM-DD HH:mm:ss') : '—');

function healthScore(cell?: HealthCell): { color: string; label: string } {
    if (!cell) return { color: '#e2e8f0', label: '无数据' };
    if (!cell.registered) return { color: '#cbd5e1', label: '未注册' };
    const er = cell.error_rate_1h || 0;
    if (er >= 0.3) return { color: '#ef4444', label: '严重' };
    if (er >= 0.1) return { color: '#f59e0b', label: '告警' };
    if (!cell.last_success_at && !cell.last_error_at) return { color: '#94a3b8', label: '未调用' };
    return { color: '#10b981', label: '正常' };
}

/* 健康状态色板（与 healthScore 对齐） */
const STATUS_COLORS = { ok: '#10b981', warn: '#f59e0b', err: '#ef4444', na: '#94a3b8' };

const TIER_LABELS: Record<string, string> = {
    T1: '离线日级',
    T2: '分钟',
    T3: '实时报价',
    T4: '逐笔订单流',
    T5: '资金流',
};
const TIER_ORDER = ['T1', 'T2', 'T3', 'T4', 'T5'];

/** 字段是否命中异常（主/备任一源为告警或严重） */
function isFieldAbnormal(
    field: string,
    cellsByField: Record<string, Record<string, HealthCell>>,
): boolean {
    const cells = Object.values(cellsByField[field] || {});
    return cells.some((c) => {
        const color = healthScore(c).color;
        return color === STATUS_COLORS.err || color === STATUS_COLORS.warn;
    });
}

/** 图例小圆点 */
const LegendDot: React.FC<{ color: string; label: string }> = ({ color, label }) => (
    <span className="inline-flex items-center gap-1.5">
        <span className="inline-block w-3 h-3 rounded-full" style={{ background: color }} />
        <span className="text-slate-500">{label}</span>
    </span>
);

const severityColor = (s: string) => {
    const map: Record<string, string> = {
        info: 'blue',
        warning: 'orange',
        error: 'red',
        critical: 'volcano',
    };
    return map[s] || 'default';
};

/* ============================================================ */
export const AdminDataPlatform: React.FC = () => {
    const [market, setMarket] = useState<Market>('A');
    const [matrix, setMatrix] = useState<HealthMatrix | null>(null);
    const [matrixLoading, setMatrixLoading] = useState(false);
    const [onlyAbnormal, setOnlyAbnormal] = useState(false);

    const [sources, setSources] = useState<SourceSummary[]>([]);
    const [sourcesLoading, setSourcesLoading] = useState(false);

    const [coverage, setCoverage] = useState<Record<string, FieldCoverageRow[]>>({});

    const [alerts, setAlerts] = useState<QualityAlert[]>([]);
    const [alertsTotal, setAlertsTotal] = useState(0);
    const [alertsLoading, setAlertsLoading] = useState(false);
    const [alertFilter, setAlertFilter] = useState<{
        severity?: string;
        acknowledged?: boolean;
    }>({});

    const [syncOpen, setSyncOpen] = useState(false);
    const [syncSource, setSyncSource] = useState<string>('');
    const [syncField, setSyncField] = useState<string>('daily_kline');
    const [syncSymbols, setSyncSymbols] = useState<string>('600519.SH');
    const [syncLoading, setSyncLoading] = useState(false);
    const [syncResult, setSyncResult] = useState<any[] | null>(null);

    /* ---- 一键同步（当前 market 全源 sweep）---- */
    const [sweepOpen, setSweepOpen] = useState(false);
    const [sweepField, setSweepField] = useState<string>('daily_kline');
    const [sweepSymbols, setSweepSymbols] = useState<string>('600519.SH, 000001.SZ, 601318.SH');
    const [sweepIncludeFallbacks, setSweepIncludeFallbacks] = useState(true);
    const [sweepLoading, setSweepLoading] = useState(false);
    const [sweepResult, setSweepResult] = useState<any | null>(null);

    // 新鲜度
    const [freshness, setFreshness] = useState<FreshnessItem[]>([]);
    const [freshnessLoading, setFreshnessLoading] = useState(false);

    // 源在线状态
    const [onlineItems, setOnlineItems] = useState<OnlineStatusItem[]>([]);
    const [onlineLoading, setOnlineLoading] = useState(false);
    const [onlineSummary, setOnlineSummary] = useState({ total: 0, online: 0, offline: 0 });

    const defaultSweepSymbols = (m: Market): string => {
        if (m === 'A') return '600519.SH, 000001.SZ, 601318.SH, 000858.SZ, 600036.SH';
        if (m === 'HK') return '00700.HK, 09988.HK, 00388.HK';
        return 'AAPL, MSFT, NVDA, GOOGL';
    };

    const loadMatrix = async (m: Market) => {
        setMatrixLoading(true);
        try {
            const data = await dataPlatformService.getHealthMatrix(m);
            setMatrix(data);
        } catch (e: any) {
            message.error(`加载健康矩阵失败: ${e?.message || e}`);
        } finally {
            setMatrixLoading(false);
        }
    };

    const loadSources = async () => {
        setSourcesLoading(true);
        try {
            const data = await dataPlatformService.listSources();
            setSources(data.sources || []);
        } catch (e: any) {
            message.error(`加载数据源失败: ${e?.message || e}`);
        } finally {
            setSourcesLoading(false);
        }
    };

    const loadCoverage = async () => {
        try {
            const data = await dataPlatformService.getFieldCoverage();
            setCoverage(data.coverage || {});
        } catch (e: any) {
            message.error(`加载字段覆盖失败: ${e?.message || e}`);
        }
    };

    const loadAlerts = async () => {
        setAlertsLoading(true);
        try {
            const data = await dataPlatformService.listAlerts({
                severity: alertFilter.severity,
                acknowledged: alertFilter.acknowledged,
                limit: 50,
                offset: 0,
            });
            setAlerts(data.items || []);
            setAlertsTotal(data.total || 0);
        } catch (e: any) {
            message.error(`加载告警失败: ${e?.message || e}`);
        } finally {
            setAlertsLoading(false);
        }
    };

    const loadFreshness = async (m: Market) => {
        setFreshnessLoading(true);
        try {
            const data = await dataPlatformService.getFreshness(m);
            setFreshness(data.items || []);
        } catch (e: any) {
            message.error(`加载数据新鲜度失败: ${e?.message || e}`);
        } finally {
            setFreshnessLoading(false);
        }
    };

    const loadOnlineStatus = async () => {
        setOnlineLoading(true);
        try {
            const data = await dataPlatformService.getOnlineStatus();
            setOnlineItems(data.items || []);
            setOnlineSummary({ total: data.total, online: data.online, offline: data.offline });
        } catch (e: any) {
            message.error(`加载源状态失败: ${e?.message || e}`);
        } finally {
            setOnlineLoading(false);
        }
    };

    useEffect(() => {
        loadMatrix(market);
        loadFreshness(market);
    }, [market]);

    useEffect(() => {
        loadSources();
        loadCoverage();
        loadAlerts();
        loadFreshness(market);
    }, []);

    useEffect(() => {
        loadAlerts();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [alertFilter.severity, alertFilter.acknowledged]);

    /* ---- 派生：cellsByField[field][source] ---- */
    const cellsByField = useMemo(() => {
        const m: Record<string, Record<string, HealthCell>> = {};
        (matrix?.cells || []).forEach((c) => {
            if (!m[c.field]) m[c.field] = {};
            m[c.field][c.source] = c;
        });
        return m;
    }, [matrix]);

    /* ---- 派生：健康卡片的分组 / 过滤 ---- */
    const orderedTiers = useMemo(() => {
        if (!matrix) return TIER_ORDER;
        const present = new Set<string>(Object.values(matrix.field_tiers || {}));
        return TIER_ORDER.filter((t) => present.has(t));
    }, [matrix]);

    const totalShownFields = useMemo(() => {
        if (!matrix) return 0;
        const tierMap = matrix.field_tiers || {};
        return matrix.fields.filter((f) => {
            const tier = tierMap[f] || 'T1';
            if (!orderedTiers.includes(tier)) return false;
            if (onlyAbnormal && !isFieldAbnormal(f, cellsByField)) return false;
            return true;
        }).length;
    }, [matrix, onlyAbnormal, orderedTiers, cellsByField]);

    const totalCells = matrix?.cells.length || 0;
    const okCells = (matrix?.cells || []).filter((c) => healthScore(c).color === '#10b981').length;
    const errorCells = (matrix?.cells || []).filter((c) => healthScore(c).color === '#ef4444').length;
    const warnCells = (matrix?.cells || []).filter((c) => healthScore(c).color === '#f59e0b').length;

    const ackAlert = async (id: number) => {
        try {
            await dataPlatformService.ackAlert(id);
            message.success(`告警 #${id} 已确认`);
            loadAlerts();
        } catch (e: any) {
            message.error(`确认失败: ${e?.message || e}`);
        }
    };

    const triggerSync = async () => {
        const symbols = syncSymbols
            .split(/[\s,，\n]+/)
            .map((s) => s.trim())
            .filter(Boolean);
        if (!syncSource || !syncField || symbols.length === 0) {
            message.warning('请填写数据源 / 字段 / 至少 1 个 symbol');
            return;
        }
        setSyncLoading(true);
        try {
            const data = await dataPlatformService.triggerSync(syncSource, {
                market,
                field: syncField,
                symbols,
            });
            setSyncResult(data.results || []);
            message.success(`同步完成（${data.results?.length || 0} 个 symbol）`);
            loadMatrix(market);
        } catch (e: any) {
            message.error(`同步失败: ${e?.message || e}`);
        } finally {
            setSyncLoading(false);
        }
    };

    const runSweep = async () => {
        const symbols = sweepSymbols
            .split(/[\s,，\n]+/)
            .map((s) => s.trim())
            .filter(Boolean);
        if (symbols.length === 0) {
            message.warning('请填写至少 1 个 symbol');
            return;
        }
        setSweepLoading(true);
        setSweepResult(null);
        try {
            const data = await dataPlatformService.sweepMarket({
                market,
                field: sweepField,
                symbols,
                include_fallbacks: sweepIncludeFallbacks,
            });
            setSweepResult(data);
            const s = data.summary || { ok: 0, failed: 0 };
            message.success(`一键同步完成：成功 ${s.ok} / 失败 ${s.failed}（${data.sources?.length || 0} 个源）`);
            loadMatrix(market);
            loadSources();
        } catch (e: any) {
            message.error(`一键同步失败: ${e?.response?.data?.detail || e?.message || e}`);
        } finally {
            setSweepLoading(false);
        }
    };

    const openSweep = () => {
        setSweepSymbols(defaultSweepSymbols(market));
        setSweepResult(null);
        setSweepOpen(true);
    };

    /* ============================================================ */
    return (
        <div className="space-y-4">
            <Card
                bordered={false}
                className="rounded-2xl shadow-sm"
                styles={{ body: { padding: 20 } }}
            >
                <div className="flex items-center justify-between flex-wrap gap-3">
                    <div>
                        <Title level={4} className="!mb-1 !font-black">
                            <CloudServerOutlined className="mr-2 text-blue-500" />
                            数据源监控
                        </Title>
                        <Text className="text-slate-400 text-xs">
                            字段聚合 / 多源容灾 / 共识投票 / 数据质量告警
                        </Text>
                    </div>
                    <Space>
                        <Segmented
                            value={market}
                            onChange={(v) => setMarket(v as Market)}
                            options={[
                                { label: 'A 股', value: 'A' },
                                { label: '港股', value: 'HK' },
                                { label: '美股', value: 'US' },
                            ]}
                        />
                        <Button
                            type="primary"
                            icon={<SyncOutlined />}
                            onClick={openSweep}
                        >
                            一键同步
                        </Button>
                        <Button
                            icon={<ReloadOutlined />}
                            onClick={() => {
                                loadMatrix(market);
                                loadSources();
                                loadCoverage();
                                loadAlerts();
                                loadFreshness(market);
                            }}
                        >
                            刷新
                        </Button>
                    </Space>
                </div>

                <Row gutter={16} className="mt-4">
                    <Col xs={12} sm={6}>
                        <Statistic title="字段 × 源 总数" value={totalCells} style={{ textAlign: 'center' }} />
                    </Col>
                    <Col xs={12} sm={6}>
                        <Statistic
                            title="正常"
                            value={okCells}
                            valueStyle={{ color: '#10b981' }}
                            prefix={<CheckCircleFilled />}
                            style={{ textAlign: 'center' }}
                        />
                    </Col>
                    <Col xs={12} sm={6}>
                        <Statistic
                            title="告警"
                            value={warnCells}
                            valueStyle={{ color: '#f59e0b' }}
                            prefix={<WarningFilled />}
                            style={{ textAlign: 'center' }}
                        />
                    </Col>
                    <Col xs={12} sm={6}>
                        <Statistic
                            title="严重"
                            value={errorCells}
                            valueStyle={{ color: '#ef4444' }}
                            prefix={<CloseCircleFilled />}
                            style={{ textAlign: 'center' }}
                        />
                    </Col>
                </Row>
            </Card>

            <Tabs
                items={[
                    {
                        key: 'matrix',
                        label: '健康矩阵',
                        children: (
                            <Card bordered={false} className="rounded-2xl shadow-sm">
                                <Spin spinning={matrixLoading}>
                                    {matrix ? (
                                        <div>
                                            {/* 图例 + 只看异常 */}
                                            <div className="flex flex-wrap items-center gap-x-5 gap-y-2 mb-4 text-xs">
                                                <LegendDot color={STATUS_COLORS.ok} label="正常" />
                                                <LegendDot color={STATUS_COLORS.warn} label="告警" />
                                                <LegendDot color={STATUS_COLORS.err} label="严重" />
                                                <LegendDot color={STATUS_COLORS.na} label="未调用" />
                                                <span className="text-slate-500">标签「主/备」= 该字段路由下的主源/备份源</span>
                                                <div className="ml-auto flex items-center gap-2">
                                                    <Switch
                                                        checked={onlyAbnormal}
                                                        onChange={setOnlyAbnormal}
                                                        checkedChildren="只看异常"
                                                        unCheckedChildren="全部"
                                                    />
                                                    <Text type="secondary">
                                                        显示 {totalShownFields}/{matrix.fields.length} 个字段
                                                    </Text>
                                                </div>
                                            </div>

                                            {orderedTiers.map((tier) => {
                                                const tierFields = matrix.fields.filter(
                                                    (f) => (matrix.field_tiers?.[f] || 'T1') === tier,
                                                );
                                                if (tierFields.length === 0) return null;
                                                const shown = onlyAbnormal
                                                    ? tierFields.filter((f) => isFieldAbnormal(f, cellsByField))
                                                    : tierFields;
                                                if (shown.length === 0) return null;
                                                return (
                                                    <div key={tier} className="mb-5">
                                                        <div className="flex items-baseline gap-2 mb-2">
                                                            <Text strong className="text-sm">{tier}</Text>
                                                            <Text type="secondary" className="text-xs">
                                                                {TIER_LABELS[tier] || ''}
                                                            </Text>
                                                            <Text type="secondary" className="text-xs">
                                                                {onlyAbnormal
                                                                    ? `${shown.length}/${tierFields.length} 个字段有异常`
                                                                    : `${shown.length} 个字段`}
                                                            </Text>
                                                        </div>
                                                        <Row gutter={[12, 12]}>
                                                            {shown.map((f) => {
                                                                const scells = cellsByField[f] || {};
                                                                const fieldSources = matrix.sources.filter((s) => scells[s]);
                                                                const primaryCell = fieldSources
                                                                    .map((s) => scells[s])
                                                                    .find((c) => c.is_primary) || fieldSources.map((s) => scells[s])[0];
                                                                const info = healthScore(primaryCell);
                                                                return (
                                                                    <Col xs={24} lg={12} xxl={8} key={f}>
                                                                        <div className="border border-slate-200 rounded-xl p-3 bg-white h-full flex flex-col gap-2">
                                                                            <div className="flex items-start justify-between">
                                                                                <Space size={6}>
                                                                                    <Text strong className="font-mono">{f}</Text>
                                                                                    <Tag color="geekblue" style={{ marginInlineEnd: 0 }}>{tier}</Tag>
                                                                                </Space>
                                                                                <span
                                                                                    className="text-xs font-bold shrink-0"
                                                                                    style={{ color: info.color }}
                                                                                >
                                                                                    {info.label === '无数据' ? '—' : info.label}
                                                                                </span>
                                                                            </div>
                                                                            {/* 主/备源链路 */}
                                                                            <div className="flex flex-wrap gap-1.5">
                                                                                {fieldSources.length === 0 ? (
                                                                                    <span className="text-xs text-slate-400">该字段暂无路由源</span>
                                                                                ) : fieldSources.map((s) => {
                                                                                    const c = scells[s];
                                                                                    const i = healthScore(c);
                                                                                    const isP = c?.is_primary;
                                                                                    return (
                                                                                        <Tooltip key={s} title={
                                                                                            <div className="text-xs">
                                                                                                <div>{isP ? '主源' : '备份'} · {i.label}</div>
                                                                                                <div>错误率(1h): {c ? `${(c.error_rate_1h * 100).toFixed(2)}%` : '—'}</div>
                                                                                                <div>平均时延: {c ? `${c.avg_latency_ms.toFixed(0)}ms` : '—'}</div>
                                                                                                <div>上次成功: {formatTime(c?.last_success_at)}</div>
                                                                                                {c?.fallback_triggered_count ? (
                                                                                                    <div>触发备份 × {c.fallback_triggered_count}</div>
                                                                                                ) : null}
                                                                                            </div>
                                                                                        }>
                                                                                            <span
                                                                                                className="inline-flex items-center px-2 py-0.5 rounded text-[11px] text-white font-medium cursor-pointer"
                                                                                                style={{ background: i.color }}
                                                                                            >
                                                                                                {isP ? '主' : '备'}·{s}
                                                                                            </span>
                                                                                        </Tooltip>
                                                                                    );
                                                                                })}
                                                                            </div>
                                                                            {/* 指标 */}
                                                                            <div className="grid grid-cols-3 gap-2 text-[11px] text-slate-400 border-t border-slate-100 pt-2 mt-auto">
                                                                                <div>错误率(1h)<br />
                                                                                    <span className="text-slate-600 font-mono">
                                                                                        {primaryCell ? `${(primaryCell.error_rate_1h * 100).toFixed(1)}%` : '—'}
                                                                                    </span>
                                                                                </div>
                                                                                <div>平均时延<br />
                                                                                    <span className="text-slate-600 font-mono">
                                                                                        {primaryCell ? `${primaryCell.avg_latency_ms.toFixed(0)}ms` : '—'}
                                                                                    </span>
                                                                                </div>
                                                                                <div>最近成功<br />
                                                                                    <span className="text-slate-600 font-mono">
                                                                                        {primaryCell ? formatTime(primaryCell.last_success_at) : '—'}
                                                                                    </span>
                                                                                </div>
                                                                            </div>
                                                                        </div>
                                                                    </Col>
                                                                );
                                                            })}
                                                        </Row>
                                                    </div>
                                                );
                                            })}

                                            {totalShownFields === 0 && <Empty description="无匹配字段" />}
                                        </div>
                                    ) : (
                                        <Empty description="暂无数据" />
                                    )}
                                </Spin>
                            </Card>
                        ),
                    },
                    {
                        key: 'sources',
                        label: '数据源清单',
                        children: (
                            <Card bordered={false} className="rounded-2xl shadow-sm">
                                <Table
                                    rowKey="name"
                                    loading={sourcesLoading}
                                    dataSource={sources}
                                    pagination={false}
                                    size="middle"
                                    columns={[
                                        { title: '名称', dataIndex: 'name', render: (n) => <Tag color="blue">{n}</Tag> },
                                        { title: '类', dataIndex: 'class' },
                                        {
                                            title: '市场',
                                            dataIndex: 'markets',
                                            render: (arr: string[]) =>
                                                arr.map((m) => <Tag key={m}>{m}</Tag>),
                                        },
                                        { title: '声明字段', dataIndex: 'field_count', align: 'center' },
                                        {
                                            title: '路由命中',
                                            dataIndex: 'covered_field_count',
                                            align: 'center',
                                        },
                                        {
                                            title: '错误率(1h)',
                                            dataIndex: ['health_summary', 'error_rate_1h'],
                                            render: (v: number) =>
                                                v != null ? `${(v * 100).toFixed(1)}%` : '—',
                                        },
                                        {
                                            title: '上次成功',
                                            dataIndex: ['health_summary', 'last_success_at'],
                                            render: formatTime,
                                        },
                                        {
                                            title: '操作',
                                            render: (_: any, r: SourceSummary) => (
                                                <Button
                                                    size="small"
                                                    icon={<SyncOutlined />}
                                                    onClick={() => {
                                                        setSyncSource(r.name);
                                                        setSyncOpen(true);
                                                    }}
                                                >
                                                    触发同步
                                                </Button>
                                            ),
                                        },
                                    ]}
                                />
                            </Card>
                        ),
                    },
                    {
                        key: 'coverage',
                        label: '字段路由表',
                        children: (
                            <Card bordered={false} className="rounded-2xl shadow-sm">
                                <Tabs
                                    items={Object.keys(coverage).map((m) => ({
                                        key: m,
                                        label: m,
                                        children: (
                                            <Table
                                                rowKey="field"
                                                dataSource={coverage[m]}
                                                pagination={false}
                                                size="small"
                                                columns={[
                                                    { title: '字段', dataIndex: 'field', width: 160 },
                                                    {
                                                        title: 'Tier',
                                                        dataIndex: 'tier',
                                                        width: 80,
                                                        render: (t) => <Tag color="geekblue">{t}</Tag>,
                                                    },
                                                    {
                                                        title: '主源',
                                                        dataIndex: 'primary',
                                                        render: (s) => <Tag color="green">{s}</Tag>,
                                                    },
                                                    {
                                                        title: '备份源',
                                                        dataIndex: 'fallbacks',
                                                        render: (arr: string[]) =>
                                                            (arr || []).map((s) => (
                                                                <Tag key={s}>{s}</Tag>
                                                            )),
                                                    },
                                                    {
                                                        title: '共识',
                                                        dataIndex: 'consensus',
                                                        render: (v: boolean | string[]) => {
                                                            if (Array.isArray(v)) {
                                                                return v.length
                                                                    ? v.map((s) => (
                                                                          <Tag key={s} color="purple">
                                                                              {s}
                                                                          </Tag>
                                                                      ))
                                                                    : <Tag>否</Tag>;
                                                            }
                                                            return v ? (
                                                                <Tag color="purple">参与</Tag>
                                                            ) : (
                                                                <Tag>否</Tag>
                                                            );
                                                        },
                                                    },
                                                    {
                                                        title: '清洗',
                                                        dataIndex: 'cleanup',
                                                        render: (arr?: string[]) =>
                                                            (arr || []).length
                                                                ? (arr || []).map((s) => (
                                                                      <Tag key={s} color="cyan">
                                                                          {s}
                                                                      </Tag>
                                                                  ))
                                                                : <Tag>默认</Tag>,
                                                    },
                                                ]}
                                            />
                                        ),
                                    }))}
                                />
                            </Card>
                        ),
                    },
                    {
                        key: 'freshness',
                        label: '数据新鲜度',
                        children: (
                            <Card bordered={false} className="rounded-2xl shadow-sm">
                                <Spin spinning={freshnessLoading}>
                                    {freshness.length > 0 ? (
                                        <div className="overflow-auto">
                                            <table className="border-collapse text-xs w-full">
                                                <thead>
                                                    <tr>
                                                        <th className="border border-slate-200 bg-slate-50 px-3 py-2 text-left">字段</th>
                                                        <th className="border border-slate-200 bg-slate-50 px-3 py-2 text-left">数据源</th>
                                                        <th className="border border-slate-200 bg-slate-50 px-3 py-2 text-center">角色</th>
                                                        <th className="border border-slate-200 bg-slate-50 px-3 py-2 text-center">新鲜度</th>
                                                        <th className="border border-slate-200 bg-slate-50 px-3 py-2 text-right">天数</th>
                                                        <th className="border border-slate-200 bg-slate-50 px-3 py-2 text-right">错误率(1h)</th>
                                                        <th className="border border-slate-200 bg-slate-50 px-3 py-2 text-right">时延</th>
                                                        <th className="border border-slate-200 bg-slate-50 px-3 py-2 text-left">上次成功</th>
                                                    </tr>
                                                </thead>
                                                <tbody>
                                                    {freshness.map((item, idx) => {
                                                        const freshColor = item.freshness === 'fresh' ? '#10b981'
                                                            : item.freshness === 'stale' ? '#f59e0b'
                                                            : item.freshness === 'outdated' ? '#ef4444'
                                                            : '#94a3b8';
                                                        const freshLabel = item.freshness === 'fresh' ? '当天'
                                                            : item.freshness === 'stale' ? '1-3天'
                                                            : item.freshness === 'outdated' ? '3天+'
                                                            : '未知';
                                                        return (
                                                            <tr key={idx}>
                                                                <td className="border border-slate-200 px-3 py-1.5 font-mono">{item.field}</td>
                                                                <td className="border border-slate-200 px-3 py-1.5">
                                                                    <Tag color={item.is_primary ? 'green' : 'default'}>{item.source}</Tag>
                                                                </td>
                                                                <td className="border border-slate-200 px-3 py-1.5 text-center">
                                                                    {item.is_primary ? <Tag color="blue">主源</Tag> : <Tag>备份</Tag>}
                                                                </td>
                                                                <td className="border border-slate-200 px-3 py-1.5 text-center">
                                                                    <span className="inline-block w-3 h-3 rounded-full mr-1" style={{ background: freshColor }} />
                                                                    {freshLabel}
                                                                </td>
                                                                <td className="border border-slate-200 px-3 py-1.5 text-right font-mono">
                                                                    {item.days_stale != null ? item.days_stale : '—'}
                                                                </td>
                                                                <td className="border border-slate-200 px-3 py-1.5 text-right" style={{ color: item.error_rate_1h >= 0.3 ? '#ef4444' : item.error_rate_1h >= 0.1 ? '#f59e0b' : undefined }}>
                                                                    {(item.error_rate_1h * 100).toFixed(1)}%
                                                                </td>
                                                                <td className="border border-slate-200 px-3 py-1.5 text-right font-mono">
                                                                    {item.avg_latency_ms.toFixed(0)}ms
                                                                </td>
                                                                <td className="border border-slate-200 px-3 py-1.5 text-xs text-slate-500">
                                                                    {formatTime(item.last_success_at)}
                                                                </td>
                                                            </tr>
                                                        );
                                                    })}
                                                </tbody>
                                            </table>
                                            <div className="text-[11px] text-slate-400 mt-3">
                                                颜色：绿=当天更新 / 黄=1-3天未更新 / 红=3天以上未更新 / 灰=未知
                                            </div>
                                        </div>
                                    ) : (
                                        <Empty description="暂无新鲜度数据" />
                                    )}
                                </Spin>
                            </Card>
                        ),
                    },
                    {
                        key: 'online',
                        label: '源状态',
                        children: (
                            <Card bordered={false} className="rounded-2xl shadow-sm">
                                <div className="mb-3 flex items-center gap-3">
                                    <Tag color="green">在线 {onlineSummary.online}</Tag>
                                    <Tag color="red">离线 {onlineSummary.offline}</Tag>
                                    <Tag>总计 {onlineSummary.total}</Tag>
                                    <Button
                                        icon={<ReloadOutlined />}
                                        loading={onlineLoading}
                                        onClick={loadOnlineStatus}
                                    >
                                        重新检测
                                    </Button>
                                </div>
                                <Table
                                    rowKey="name"
                                    loading={onlineLoading}
                                    dataSource={onlineItems}
                                    pagination={false}
                                    size="middle"
                                    columns={[
                                        {
                                            title: '状态',
                                            dataIndex: 'status',
                                            width: 80,
                                            render: (s: string) => {
                                                const color = s === 'online' ? 'green' : s === 'error' ? 'red' : s === 'unavailable' ? 'default' : 'warning';
                                                const label = s === 'online' ? '在线' : s === 'error' ? '异常' : s === 'unavailable' ? '不可用' : '未知';
                                                return <Tag color={color}>{label}</Tag>;
                                            },
                                        },
                                        { title: '名称', dataIndex: 'name', render: (n: string) => <Tag color="blue">{n}</Tag> },
                                        { title: '类', dataIndex: 'class' },
                                        {
                                            title: '市场',
                                            dataIndex: 'markets',
                                            render: (arr: string[]) => arr.map((m) => <Tag key={m}>{m}</Tag>),
                                        },
                                        {
                                            title: '支持字段',
                                            dataIndex: 'fields',
                                            render: (arr: string[]) => (
                                                <Tooltip title={arr.join(', ')}>
                                                    <Tag>{arr.length} 个</Tag>
                                                </Tooltip>
                                            ),
                                        },
                                        {
                                            title: '检测延迟',
                                            dataIndex: 'latency_ms',
                                            width: 100,
                                            render: (v: number | null) => v != null ? `${v.toFixed(0)}ms` : '—',
                                        },
                                        {
                                            title: '错误信息',
                                            dataIndex: 'error',
                                            ellipsis: true,
                                            render: (e?: string | null) => e ? (
                                                <Tooltip title={e}>
                                                    <Text type="danger" ellipsis style={{ maxWidth: 300 }}>{e}</Text>
                                                </Tooltip>
                                            ) : <Text type="success">—</Text>,
                                        },
                                        {
                                            title: '检测时间',
                                            dataIndex: 'checked_at',
                                            width: 140,
                                            render: formatTime,
                                        },
                                    ]}
                                />
                            </Card>
                        ),
                    },
                    {
                        key: 'alerts',
                        label: (
                            <Badge count={alertsTotal} offset={[8, -2]} size="small">
                                <span>质量告警</span>
                            </Badge>
                        ),
                        children: (
                            <Card bordered={false} className="rounded-2xl shadow-sm">
                                <Space className="mb-3" wrap>
                                    <Select
                                        allowClear
                                        placeholder="严重程度"
                                        style={{ width: 140 }}
                                        value={alertFilter.severity}
                                        onChange={(v) =>
                                            setAlertFilter({ ...alertFilter, severity: v })
                                        }
                                        options={[
                                            { label: 'info', value: 'info' },
                                            { label: 'warning', value: 'warning' },
                                            { label: 'error', value: 'error' },
                                            { label: 'critical', value: 'critical' },
                                        ]}
                                    />
                                    <Select
                                        allowClear
                                        placeholder="确认状态"
                                        style={{ width: 140 }}
                                        value={
                                            alertFilter.acknowledged === undefined
                                                ? undefined
                                                : alertFilter.acknowledged
                                                ? 'yes'
                                                : 'no'
                                        }
                                        onChange={(v) =>
                                            setAlertFilter({
                                                ...alertFilter,
                                                acknowledged:
                                                    v === undefined ? undefined : v === 'yes',
                                            })
                                        }
                                        options={[
                                            { label: '未确认', value: 'no' },
                                            { label: '已确认', value: 'yes' },
                                        ]}
                                    />
                                    <Button icon={<ReloadOutlined />} onClick={loadAlerts}>
                                        刷新
                                    </Button>
                                </Space>
                                <Table
                                    rowKey="id"
                                    loading={alertsLoading}
                                    dataSource={alerts}
                                    size="small"
                                    pagination={{ pageSize: 50, total: alertsTotal }}
                                    columns={[
                                        { title: 'ID', dataIndex: 'id', width: 70 },
                                        {
                                            title: '严重',
                                            dataIndex: 'severity',
                                            width: 90,
                                            render: (s) => <Tag color={severityColor(s)}>{s}</Tag>,
                                        },
                                        { title: '类型', dataIndex: 'alert_type', width: 160 },
                                        { title: '市场', dataIndex: 'market', width: 70 },
                                        { title: '字段', dataIndex: 'field', width: 140 },
                                        { title: '源', dataIndex: 'source', width: 120 },
                                        { title: '消息', dataIndex: 'message', ellipsis: true },
                                        {
                                            title: '时间',
                                            dataIndex: 'created_at',
                                            width: 140,
                                            render: formatTime,
                                        },
                                        {
                                            title: '操作',
                                            width: 110,
                                            render: (_: any, r: QualityAlert) =>
                                                r.acknowledged ? (
                                                    <Tag color="default">已确认</Tag>
                                                ) : (
                                                    <Button
                                                        size="small"
                                                        type="primary"
                                                        ghost
                                                        onClick={() => ackAlert(r.id)}
                                                    >
                                                        确认
                                                    </Button>
                                                ),
                                        },
                                    ]}
                                />
                            </Card>
                        ),
                    },
                ]}
            />

            <Modal
                title={`触发同步 — ${syncSource}`}
                open={syncOpen}
                onCancel={() => {
                    setSyncOpen(false);
                    setSyncResult(null);
                }}
                onOk={triggerSync}
                confirmLoading={syncLoading}
                okText="开始同步"
                width={680}
            >
                <div className="space-y-3">
                    <Alert
                        type="info"
                        showIcon
                        message="开发期同步，单次最多 50 个 symbol，串行执行"
                    />
                    <div>
                        <Text strong>市场</Text>
                        <Tag className="ml-2">{market}</Tag>
                    </div>
                    <div>
                        <Text strong>字段</Text>
                        <Input
                            value={syncField}
                            onChange={(e) => setSyncField(e.target.value)}
                            placeholder="daily_kline / minute_kline / valuation ..."
                            className="mt-1"
                        />
                    </div>
                    <div>
                        <Text strong>Symbols（逗号或空格分隔）</Text>
                        <Input.TextArea
                            value={syncSymbols}
                            onChange={(e) => setSyncSymbols(e.target.value)}
                            rows={3}
                            placeholder="600519.SH, 000001.SZ"
                            className="mt-1"
                        />
                    </div>
                    {syncResult && (
                        <Card size="small" title="结果">
                            <pre className="text-xs whitespace-pre-wrap max-h-60 overflow-auto">
                                {JSON.stringify(syncResult, null, 2)}
                            </pre>
                        </Card>
                    )}
                </div>
            </Modal>

            <Modal
                title={`一键同步 — ${market} 市场`}
                open={sweepOpen}
                onCancel={() => setSweepOpen(false)}
                onOk={runSweep}
                confirmLoading={sweepLoading}
                okText={sweepLoading ? '同步中…' : '开始同步'}
                width={760}
                destroyOnHidden
            >
                <div className="space-y-3">
                    <Alert
                        type="info"
                        showIcon
                        message="对当前市场所选字段的全部声明源依次拉取一次，用于点亮健康矩阵。单次最多 20 个 symbol。"
                    />
                    <Row gutter={12}>
                        <Col span={12}>
                            <Text strong>字段</Text>
                            <Select
                                className="w-full mt-1"
                                value={sweepField}
                                onChange={setSweepField}
                                options={[
                                    { label: 'daily_kline 日 K 线', value: 'daily_kline' },
                                    { label: 'minute_kline 分钟 K 线', value: 'minute_kline' },
                                    { label: 'adj_factor 复权因子', value: 'adj_factor' },
                                    { label: 'financial_report 财报', value: 'financial_report' },
                                    { label: 'valuation 估值', value: 'valuation' },
                                    { label: 'technical_indicators 技术指标', value: 'technical_indicators' },
                                ]}
                            />
                        </Col>
                        <Col span={12}>
                            <Text strong>包含备份源</Text>
                            <div className="mt-1">
                                <Segmented
                                    value={sweepIncludeFallbacks ? 'yes' : 'no'}
                                    onChange={(v) => setSweepIncludeFallbacks(v === 'yes')}
                                    options={[
                                        { label: '主源 + 备份', value: 'yes' },
                                        { label: '仅主源', value: 'no' },
                                    ]}
                                />
                            </div>
                        </Col>
                    </Row>
                    <div>
                        <Text strong>Symbols（逗号或空格分隔，最多 20 个）</Text>
                        <Input.TextArea
                            value={sweepSymbols}
                            onChange={(e) => setSweepSymbols(e.target.value)}
                            rows={3}
                            className="mt-1"
                        />
                    </div>
                    {sweepResult && (
                        <Card size="small" title="结果摘要">
                            <Space className="mb-2" wrap>
                                <Tag color="green">成功 {sweepResult.summary?.ok ?? 0}</Tag>
                                <Tag color="red">失败 {sweepResult.summary?.failed ?? 0}</Tag>
                                <Tag>触发源：{(sweepResult.sources || []).join(', ')}</Tag>
                            </Space>
                            <Table
                                size="small"
                                rowKey="source"
                                pagination={false}
                                dataSource={sweepResult.per_source || []}
                                columns={[
                                    { title: '源', dataIndex: 'source', width: 160 },
                                    {
                                        title: '成功 / 失败',
                                        render: (_: any, r: any) => {
                                            const ok = (r.results || []).filter((x: any) => x.ok).length;
                                            const fail = (r.results || []).length - ok;
                                            return (
                                                <Space>
                                                    <Tag color="green">{ok}</Tag>
                                                    <Tag color="red">{fail}</Tag>
                                                </Space>
                                            );
                                        },
                                    },
                                    {
                                        title: '首个错误',
                                        render: (_: any, r: any) => {
                                            const firstErr = (r.results || []).find(
                                                (x: any) => !x.ok,
                                            );
                                            return firstErr ? (
                                                <Tooltip title={firstErr.error}>
                                                    <Text type="danger" ellipsis style={{ maxWidth: 320 }}>
                                                        {firstErr.symbol}: {firstErr.error}
                                                    </Text>
                                                </Tooltip>
                                            ) : (
                                                <Text type="success">—</Text>
                                            );
                                        },
                                    },
                                ]}
                            />
                            <Text className="block mt-2 text-xs text-slate-400">
                                聚合链路结果：
                                {(sweepResult.aggregated || [])
                                    .map((r: any) =>
                                        r.ok
                                            ? `${r.symbol}=${r.source_used}(${r.rows})`
                                            : `${r.symbol}=ERR`,
                                    )
                                    .join(' · ')}
                            </Text>
                        </Card>
                    )}
                </div>
            </Modal>
        </div>
    );
};

export default AdminDataPlatform;
