/**
 * 后台「模型管理」页（模型目录 / 训练任务 两个页签，2026-10-10 展示名改版）。
 *
 * 展示口径：一切列表/标题以**人能读懂的名字**为主 —— 模型目录优先
 * metadata.display_name → model_name → job_name；训练任务优先
 * request_payload.display_name → job_name；长 ID（model_id / run_id）一律
 * 降级为副行 admin-num 小字（保留工程师核对用）。无名字段不回填假名，
 * 训练任务缺名时按「类型 + 创建时间」生成兜底名。
 */
import React, { useMemo, useState, useCallback } from 'react';
import {
    Table, Button, message, Space, Tag, Modal, Collapse, Descriptions,
    Tooltip, Typography, Spin, Tabs, Progress, Select, Segmented
} from 'antd';
import {
    ScanOutlined, FolderOpenOutlined,
    FileOutlined, ReloadOutlined,
    ThunderboltOutlined, HistoryOutlined,
} from '@ant-design/icons';
import dayjs from 'dayjs';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { adminService } from '../services/adminService';
import { ModelDirectoryInfo, ModelScanResult } from '../types';
import { Panel } from './ui/AdminPrimitives';

const { Text } = Typography;

const MODEL_MARKET_OPTIONS = [
    { value: 'all', label: '全部', color: 'default' },
    { value: 'a_share', label: 'A股', color: 'red' },
    { value: 'hong_kong', label: '港股', color: 'blue' },
    { value: 'us_stock', label: '美股', color: 'green' },
    { value: 'crypto', label: '加密', color: 'purple' },
    { value: 'futures', label: '期货', color: 'orange' },
];

function extractModelMarket(model: ModelDirectoryInfo): string {
    const meta = model.metadata || {};
    const wf = model.workflow_config || {};
    const qlib = model.qlib_config || {};
    // context.market 必须兜底：训练脚本把市场写在 metadata.context 里，
    // 早期产物顶层没有 market 字段，只读顶层会把美股/港股模型统统显示成「A股」。
    const ctxMarket = (meta.context as { market?: string } | undefined)?.market || '';
    const raw = String(meta.market || ctxMarket || wf.market || qlib.market || '').toLowerCase();
    if (raw.includes('hk') || raw.includes('hong_kong') || raw.includes('港股')) return 'hong_kong';
    if (raw.includes('us') || raw.includes('美股')) return 'us_stock';
    if (raw.includes('crypto') || raw.includes('加密')) return 'crypto';
    if (raw.includes('futures') || raw.includes('期货')) return 'futures';
    if (raw.includes('cn') || raw.includes('a_share') || raw.includes('a股') || raw.includes('sh') || raw.includes('sz')) return 'a_share';
    return 'a_share'; // default
}

/** 模型展示名：磁盘 metadata 的 display_name → model_name → job_name 依次回落（无名为空串）。 */
const resolveModelDisplayName = (
    meta?: Record<string, any> | null,
    qlib?: Record<string, any> | null,
): string => String(meta?.display_name || meta?.model_name || meta?.job_name || qlib?.job_name || '').trim();

/** 训练任务状态中文名（筛选下拉与表格 Tag 共用一份，避免两处口径漂移）。 */
const JOB_STATUS_META: Record<string, { label: string; color: string }> = {
    pending: { label: '待执行', color: 'default' },
    provisioning: { label: '分配中', color: 'purple' },
    running: { label: '训练中', color: 'blue' },
    waiting_callback: { label: '等待回调', color: 'gold' },
    completed: { label: '已完成', color: 'green' },
    failed: { label: '已失败', color: 'red' },
    cancelled: { label: '已取消', color: 'default' },
};

const jobStatusLabel = (status: string): string => JOB_STATUS_META[status]?.label ?? status;

// 格式化文件大小
const fmtSize = (bytes: number) => {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
};

const resolveTrainingTargetMeta = (metadata?: Record<string, any> | null) => {
    const raw = metadata || {};
    const horizonCandidates = [
        raw.target_horizon_days,
        raw.horizon_days,
        raw.label_horizon_days,
        raw.t_plus_n,
    ];
    const horizonDays = horizonCandidates
        .map((value) => Number(value))
        .find((value) => Number.isFinite(value) && value > 0) ?? null;

    const modeValue = String(
        raw.target_mode ?? raw.targetMode ?? raw.target_type ?? raw.label_mode ?? ''
    ).toLowerCase();
    const targetMode = modeValue === 'classification' || modeValue === 'binary'
        ? 'classification'
        : modeValue === 'return' || modeValue === 'regression'
            ? 'return'
            : null;

    const labelFormula = raw.label_formula ?? raw.labelFormula ?? raw.label ?? null;
    const trainingWindow = raw.training_window ?? raw.trainingWindow ?? null;

    return {
        horizonDays,
        targetMode,
        labelFormula: labelFormula ? String(labelFormula) : null,
        trainingWindow: trainingWindow ? String(trainingWindow) : null,
    };
};

// workflow_config 中提取的关键字段渲染
const WorkflowSummary: React.FC<{ model: ModelDirectoryInfo }> = ({ model }) => {
    const wf = model.workflow_config || {};
    const qlib = model.qlib_config || {};
    
    // 优先尝试从 workflow_config 提取项 (旧款)
    const task = wf.task || {};
    const modelCls = model.resolved_class || task.model?.class || '—';
    const port = wf.port_analysis_config?.strategy?.class || qlib.port_analysis?.strategy?.class || '—';
    const backtest = wf.port_analysis_config?.backtest || qlib.port_analysis?.backtest || {};
    const targetMeta = resolveTrainingTargetMeta(model.metadata || wf?.metadata || qlib?.metadata || null);
    
    return (
        <Descriptions size="small" column={2} bordered className="text-xs">
            <Descriptions.Item label="模型类">{modelCls}</Descriptions.Item>
            <Descriptions.Item label="策略类">{port}</Descriptions.Item>
            <Descriptions.Item label="训练开始">{model.train_start || '—'}</Descriptions.Item>
            <Descriptions.Item label="训练结束">{model.train_end || '—'}</Descriptions.Item>
            <Descriptions.Item label="训练目标" style={{ textAlign: 'center' }} contentStyle={{ textAlign: 'center' }}>
                {targetMeta.horizonDays ? (
                    <div className="flex justify-center">
                        <Space size={4} align="center">
                            <Tag color="blue" className="m-0 font-bold">
                                T+{targetMeta.horizonDays}
                            </Tag>
                            <span className="text-[10px] text-slate-500">
                                {targetMeta.targetMode === 'classification' ? '分类' : '回归'}
                            </span>
                        </Space>
                    </div>
                ) : '—'}
            </Descriptions.Item>
            <Descriptions.Item label="标签公式">
                {targetMeta.labelFormula ? (
                    <Text code className="text-[10px] break-all">
                        {targetMeta.labelFormula}
                    </Text>
                ) : '—'}
            </Descriptions.Item>
            <Descriptions.Item label="测试/回测开始">{model.test_start || backtest.start_time || '—'}</Descriptions.Item>
            <Descriptions.Item label="测试/回测结束">{model.test_end || backtest.end_time || '—'}</Descriptions.Item>
            <Descriptions.Item label="训练窗口" span={2}>
                {targetMeta.trainingWindow ? (
                    <Text code className="text-[10px] break-all">
                        {targetMeta.trainingWindow}
                    </Text>
                ) : '—'}
            </Descriptions.Item>
            <Descriptions.Item label="基准">{String(wf.benchmark || qlib.benchmark || '—')}</Descriptions.Item>
            <Descriptions.Item label="市场">{String(wf.market || qlib.market || '—')}</Descriptions.Item>
        </Descriptions>
    );
};

// 新增性能指标展示组件
const PerformanceOverview: React.FC<{ metrics: Record<string, any> }> = ({ metrics }) => {
    const renderMetric = (label: string, data: any) => {
        if (!data) return null;
        return (
            <div className="bg-slate-50 p-2 rounded-lg border border-slate-100">
                <div className="text-[10px] text-slate-400 font-bold uppercase">{label}</div>
                <div className="grid grid-cols-2 gap-2 mt-1">
                    <div>
                        <div className="text-[10px] text-slate-500">Mean IC</div>
                        <div className="text-sm font-mono font-bold text-slate-800">{(data.mean_ic || 0).toFixed(4)}</div>
                    </div>
                    <div>
                        <div className="text-[10px] text-slate-500">ICIR</div>
                        <div className="text-sm font-mono font-bold text-slate-800">{(data.icir || 0).toFixed(4)}</div>
                    </div>
                </div>
            </div>
        );
    };

    return (
        <div className="grid grid-cols-3 gap-3">
            {renderMetric('Training', metrics.train)}
            {renderMetric('Validation', metrics.valid)}
            {renderMetric('Test', metrics.test)}
        </div>
    );
};

export const AdminModelManagement: React.FC = () => {
    const [scanResult, setScanResult] = useState<ModelScanResult | null>(null);
    const [scanning, setScanning] = useState(false);
    const [scanError, setScanError] = useState<string | null>(null);
    const [detailModel, setDetailModel] = useState<ModelDirectoryInfo | null>(null);
    const [detailVisible, setDetailVisible] = useState(false);

    // 首次进入页面自动扫描（后端有 5 分钟 Redis 缓存 + 15s 超时保护，代价低）
    // 避免用户看到空表误以为没有模型；仍保留手动"重新扫描/强制刷新"按钮
    const autoScanned = React.useRef(false);
    React.useEffect(() => {
        if (!autoScanned.current) {
            autoScanned.current = true;
            handleScan(false);
        }
    }, []);

    const handleScan = async (refresh = false) => {
        setScanning(true);
        setScanError(null);
        try {
            const result = await adminService.scanModels(refresh);
            setScanResult(result);
        } catch (err: any) {
            const errMsg = err?._adminReauthHint
                || (err?.response?.status === 403 ? '权限不足，请退出并重新登录以刷新管理员权限' : null)
                || err?.response?.data?.detail
                || err?.message
                || '扫描模型目录失败';
            setScanError(errMsg);
            message.error(errMsg);
        } finally {
            setScanning(false);
        }
    };

    const handleViewDetail = (model: ModelDirectoryInfo) => {
        setDetailModel(model);
        setDetailVisible(true);
    };

    // ── 训练任务 Tab 状态 ──────────────────────────────────────────────────
    const [jobsLoading, setJobsLoading] = useState(false);
    const [jobsData, setJobsData] = useState<{
        total: number;
        page: number;
        page_size: number;
        items: any[];
    } | null>(null);
    const [jobsPage, setJobsPage] = useState(1);
    const [jobsStatusFilter, setJobsStatusFilter] = useState<string | undefined>(undefined);
    const [jobDetailVisible, setJobDetailVisible] = useState(false);
    const [jobDetail, setJobDetail] = useState<any>(null);
    const [jobDetailLoading, setJobDetailLoading] = useState(false);
    const [activeAdminTab, setActiveAdminTab] = useState('models');
    const [modelMarketFilter, setModelMarketFilter] = useState<string>('all');

    const filteredModels = useMemo(() => {
        if (!scanResult?.models) return [];
        if (modelMarketFilter === 'all') return scanResult.models;
        return scanResult.models.filter(m => extractModelMarket(m) === modelMarketFilter);
    }, [scanResult, modelMarketFilter]);

    const loadTrainingJobs = useCallback(async (page = 1, status?: string) => {
        setJobsLoading(true);
        try {
            const resp = await adminService.listTrainingJobs({ status, page, page_size: 20 });
            setJobsData(resp);
        } catch (err: any) {
            message.error(`加载训练任务失败: ${err?.message ?? '未知错误'}`);
        } finally {
            setJobsLoading(false);
        }
    }, []);

    const handleOpenJobDetail = async (runId: string) => {
        setJobDetailVisible(true);
        setJobDetailLoading(true);
        setJobDetail(null);
        try {
            const detail = await adminService.getTrainingRun(runId);
            setJobDetail(detail);
        } catch (err: any) {
            message.error(`加载训练详情失败: ${err?.message ?? '未知错误'}`);
        } finally {
            setJobDetailLoading(false);
        }
    };

    const handleTabChange = (key: string) => {
        setActiveAdminTab(key);
        if (key === 'training-jobs' && !jobsData) {
            loadTrainingJobs(1, jobsStatusFilter);
        }
    };


    const columns = [
        {
            title: '模型',
            dataIndex: 'model_id',
            key: 'model_id',
            width: 300,
            render: (id: string, record: ModelDirectoryInfo) => {
                const meta = record.metadata || {};
                const qlib = record.qlib_config || {};
                const name = resolveModelDisplayName(meta, qlib);
                return (
                    <div className="min-w-0">
                        <div className="flex items-center gap-1.5">
                            {name ? (
                                <Tooltip title={`${name}（${id}）`}>
                                    <span className="truncate text-[12px] font-semibold text-slate-800">{name}</span>
                                </Tooltip>
                            ) : (
                                <Tooltip title={id}>
                                    <span className="truncate admin-num text-[11px] text-slate-500">{id}</span>
                                </Tooltip>
                            )}
                            {record.is_production && (
                                <Tag color="green" className="m-0 shrink-0 border-none bg-green-50 px-1 text-[9px] font-bold text-green-600">
                                    生产
                                </Tag>
                            )}
                            {record.error && (
                                <Tag color="red" className="m-0 shrink-0 text-[9px]">ERR</Tag>
                            )}
                        </div>
                        {name && <div className="mt-0.5 truncate admin-num text-[10px] text-slate-400">{id}</div>}
                    </div>
                );
            },
        },
        {
            title: '市场',
            key: 'market',
            width: 72,
            render: (_: any, record: ModelDirectoryInfo) => {
                const mkt = extractModelMarket(record);
                const opt = MODEL_MARKET_OPTIONS.find(o => o.value === mkt);
                return opt && opt.value !== 'all' ? (
                    <Tag color={opt.color} className="m-0 text-[10px]">{opt.label}</Tag>
                ) : <span className="text-slate-300 text-xs">—</span>;
            },
        },
        {
            // 模型类 / 算法类型 / 格式三合一一列，避免一列一个短语把表格拉散
            title: '类型',
            key: 'model_type',
            width: 132,
            render: (_: any, record: ModelDirectoryInfo) => {
                const meta = record.metadata || {};
                const modelType = String(meta.model_type || '');
                const clsShort = record.resolved_class ? record.resolved_class.split('.').pop() : '';
                const sub = [clsShort, record.model_format].filter(Boolean).join(' · ');
                if (!modelType && !sub) return <span className="text-slate-300 text-xs">—</span>;
                return (
                    <div className="flex flex-col items-start gap-0.5">
                        {modelType && (
                            <Tag color="cyan" className="m-0 text-[10px] font-bold">{modelType}</Tag>
                        )}
                        {sub && <span className="admin-num text-[10px] text-slate-400">{sub}</span>}
                    </div>
                );
            },
        },
        {
            title: '特征',
            dataIndex: 'feature_count',
            key: 'feature_count',
            align: 'center' as const,
            width: 72,
            render: (n: number | null) => n != null ? (
                <Tag color="blue" className="admin-num m-0 font-bold">{n}D</Tag>
            ) : <span className="text-slate-300 text-xs">—</span>,
        },
        {
            title: '训练 / 测试区间',
            key: 'train_range',
            render: (_: any, r: ModelDirectoryInfo) => (
                <div className="flex flex-col gap-0.5">
                    {r.train_start ? (
                        <span className="admin-num text-[10px] text-slate-500">
                            <Tag className="m-0 text-[10px] scale-90" color="default">训练</Tag> {r.train_start} → {r.train_end}
                        </span>
                    ) : null}
                    {r.test_start ? (
                        <span className="admin-num mt-0.5 text-[10px] text-indigo-500">
                            <Tag className="m-0 text-[10px] scale-90" color="indigo">测试</Tag> {r.test_start} → {r.test_end}
                        </span>
                    ) : null}
                    {!r.train_start && !r.test_start && <span className="text-xs text-slate-300 italic">未记录</span>}
                </div>
            )
        },
        {
            title: '训练目标',
            key: 'target',
            align: 'center' as const,
            width: 96,
            render: (_: any, r: ModelDirectoryInfo) => {
                const targetMeta = resolveTrainingTargetMeta(r.metadata);
                if (!targetMeta.horizonDays) {
                    return <span className="text-slate-300 text-xs">—</span>;
                }
                // 标签公式过长会把行撑高，收进 Tooltip（详情弹窗里有完整版）
                return (
                    <Tooltip title={targetMeta.labelFormula || undefined}>
                        <div className="flex flex-col items-center gap-0.5">
                            <Tag color="blue" className="m-0 font-bold">
                                T+{targetMeta.horizonDays}
                            </Tag>
                            <span className="text-[10px] text-slate-500">
                                {targetMeta.targetMode === 'classification' ? '分类' : '回归'}
                            </span>
                        </div>
                    </Tooltip>
                );
            },
        },
        {
            title: '最近更新',
            dataIndex: 'updated_at',
            key: 'updated_at',
            width: 128,
            render: (d: string) => (
                <span className="admin-num text-[11px] text-slate-400">
                    {dayjs(d).format('YYYY-MM-DD HH:mm')}
                </span>
            ),
        },
        {
            title: '操作',
            key: 'action',
            align: 'right' as const,
            width: 88,
            render: (_: any, record: ModelDirectoryInfo) => (
                <Button
                    size="small"
                    type="link"
                    className="px-0 text-[12px] font-medium"
                    onClick={() => handleViewDetail(record)}
                >
                    查看详情
                </Button>
            ),
        },
    ];

    // 任务展示名：display_name（用户起名）→ job_name（机器名）→ 类型+时间兜底
    const jobDisplayName = (r: any): string => {
        const name = String(r?.display_name || r?.job_name || '').trim();
        if (name) return name;
        const typeLabel = String(r?.model_type || '').trim();
        const when = r?.created_at ? dayjs(r.created_at).format('MM-DD HH:mm') : '—';
        return `${typeLabel ? `${typeLabel} ` : ''}训练任务 · ${when}`;
    };

    const jobColumns = [
        {
            title: '任务名称',
            key: 'name',
            width: 300,
            render: (_: any, r: any) => {
                const hasName = Boolean(String(r.display_name || r.job_name || '').trim());
                return (
                    <div className="min-w-0">
                        <Tooltip title={hasName ? jobDisplayName(r) : undefined}>
                            <div className={`truncate text-[12px] ${hasName ? 'font-semibold text-slate-800' : 'text-slate-600'}`}>
                                {jobDisplayName(r)}
                            </div>
                        </Tooltip>
                        <div className="truncate admin-num text-[10px] text-slate-400">{r.run_id}</div>
                    </div>
                );
            },
        },
        {
            title: '用户',
            key: 'user',
            width: 130,
            render: (_: any, r: any) => (
                <div>
                    <div className="admin-num text-xs font-semibold text-slate-700">{r.user_id}</div>
                    <div className="text-[10px] text-slate-400">{r.tenant_id}</div>
                </div>
            ),
        },
        {
            title: '状态',
            dataIndex: 'status',
            key: 'status',
            width: 130,
            // 中文状态名与筛选下拉共用 JOB_STATUS_META；running 另带进度条
            render: (status: string, r: any) => (
                <div>
                    <Tag color={JOB_STATUS_META[status]?.color ?? 'default'} className="m-0 text-[10px] font-bold">
                        {jobStatusLabel(status)}
                    </Tag>
                    {status === 'running' && (
                        <Progress percent={r.progress} size="small" className="mt-1 w-24" />
                    )}
                </div>
            ),
        },
        {
            title: '模型 / 特征',
            key: 'model_info',
            width: 120,
            render: (_: any, r: any) => (
                <div className="flex flex-col items-start gap-0.5">
                    {r.model_type && <Tag color="cyan" className="m-0 text-[10px] font-bold">{r.model_type}</Tag>}
                    {r.features_count > 0 && <span className="admin-num text-[10px] text-slate-400">{r.features_count} 特征</span>}
                </div>
            ),
        },
        {
            title: '训练区间',
            key: 'train_range',
            width: 180,
            render: (_: any, r: any) => r.train_start ? (
                <span className="admin-num text-[10px] text-slate-500">
                    {r.train_start} → {r.train_end}
                </span>
            ) : <span className="text-slate-300">—</span>,
        },
        {
            title: '注册模型',
            dataIndex: 'registered_model_id',
            key: 'registered_model_id',
            width: 220,
            render: (id: string, r: any) => {
                if (!id) return <span className="text-slate-300 text-xs">—</span>;
                const name = String(r.registered_model_display_name || '').trim();
                return (
                    <Tooltip title={id}>
                        <div className="min-w-0">
                            {name ? (
                                <>
                                    <div className="truncate text-[11px] font-medium text-slate-700">{name}</div>
                                    <div className="truncate admin-num text-[10px] text-slate-400">{id}</div>
                                </>
                            ) : (
                                <div className="truncate admin-num text-[10px] text-slate-500">{id}</div>
                            )}
                        </div>
                    </Tooltip>
                );
            },
        },
        {
            title: '创建时间',
            dataIndex: 'created_at',
            key: 'created_at',
            width: 110,
            render: (d: string) => (
                <Tooltip title={d || undefined}>
                    <span className="admin-num text-[11px] text-slate-400">
                        {d ? dayjs(d).format('MM-DD HH:mm') : '—'}
                    </span>
                </Tooltip>
            ),
        },
        {
            title: '操作',
            key: 'action',
            align: 'right' as const,
            width: 64,
            render: (_: any, r: any) => (
                <Button
                    type="link"
                    size="small"
                    className="px-0 text-[12px] font-medium"
                    onClick={() => handleOpenJobDetail(r.run_id)}
                >
                    详情
                </Button>
            ),
        },
    ];

    // 详情弹窗标题名：admin 详情接口顶层 display_name → 请求参数里的原始名
    const jobDetailDisplayName = String(
        jobDetail?.display_name
        || jobDetail?.request_payload?.display_name
        || jobDetail?.request_payload?.job_name
        || '',
    ).trim();

    return (
        <div className="space-y-4">
        <Tabs
            activeKey={activeAdminTab}
            onChange={handleTabChange}
            items={[
              {
                key: 'models',
                label: <span className="font-bold text-xs px-1"><ScanOutlined className="mr-1.5" />模型目录</span>,
                children: (
                  <div className="pt-2">
                    <Panel
                        title="模型目录"
                        sub={scanResult
                            ? `共 ${scanResult.total} 个目录 · 生产 ${scanResult.models.filter(m => m.is_production).length} 个`
                            : '自动扫描 models 产物目录，聚合 metadata / 配置 / 性能指标'}
                        right={
                            <Space size={8}>
                                {modelMarketFilter !== 'all' && (
                                    <span className="text-[11px] text-slate-400">筛选 {filteredModels.length} 个</span>
                                )}
                                <Segmented
                                    size="small"
                                    value={modelMarketFilter}
                                    onChange={(val) => setModelMarketFilter(val as string)}
                                    options={MODEL_MARKET_OPTIONS.map(m => ({ value: m.value, label: m.label }))}
                                />
                                <Button
                                    size="small"
                                    icon={<ReloadOutlined />}
                                    loading={scanning}
                                    onClick={() => handleScan(true)}
                                    title="跳过 5 分钟缓存，强制重新扫描磁盘"
                                >
                                    强制刷新
                                </Button>
                                <Button
                                    size="small"
                                    type="primary"
                                    icon={<ScanOutlined />}
                                    loading={scanning}
                                    onClick={() => handleScan(false)}
                                >
                                    {scanning ? '扫描中…' : '重新扫描'}
                                </Button>
                            </Space>
                        }
                        bodyClassName="p-0"
                    >
                        {scanError && !scanning && (
                            <div className="mx-4 mt-3 rounded-md border border-rose-100 bg-rose-50 px-3 py-2 text-xs text-rose-700">
                                <strong className="font-semibold">扫描失败：</strong>{scanError}
                            </div>
                        )}
                        <Spin spinning={scanning} tip="正在扫描模型目录…">
                            <Table
                                columns={columns}
                                dataSource={filteredModels}
                                rowKey="model_id"
                                size="small"
                                pagination={{ pageSize: 10, showTotal: (t) => `共 ${t} 条` }}
                                scroll={{ x: 'max-content' }}
                                locale={{ emptyText: scanning ? ' ' : '暂无模型，点击「重新扫描」加载' }}
                            />
                        </Spin>
                    </Panel>
                  </div>
                ),
              },
              {
                key: 'training-jobs',
                label: <span className="font-bold text-xs px-1"><HistoryOutlined className="mr-1.5" />训练任务</span>,
                children: (
                  <div className="pt-2">
                    <Panel
                        title="训练任务"
                        sub={jobsData ? `共 ${jobsData.total} 条 · 全部用户` : '管理员查看所有用户的模型训练任务记录'}
                        right={
                            <Space size={8}>
                                <Select
                                    size="small"
                                    placeholder="按状态筛选"
                                    allowClear
                                    value={jobsStatusFilter}
                                    onChange={(val) => {
                                        setJobsStatusFilter(val);
                                        setJobsPage(1);
                                        loadTrainingJobs(1, val);
                                    }}
                                    className="w-32"
                                    options={[
                                        { value: 'pending', label: '待执行' },
                                        { value: 'provisioning', label: '分配中' },
                                        { value: 'running', label: '训练中' },
                                        { value: 'waiting_callback', label: '等待回调' },
                                        { value: 'completed', label: '已完成' },
                                        { value: 'failed', label: '已失败' },
                                    ]}
                                />
                                <Button
                                    size="small"
                                    icon={<ReloadOutlined />}
                                    loading={jobsLoading}
                                    onClick={() => loadTrainingJobs(jobsPage, jobsStatusFilter)}
                                >
                                    刷新
                                </Button>
                            </Space>
                        }
                        bodyClassName="p-0"
                    >
                        <Spin spinning={jobsLoading}>
                            <Table
                                columns={jobColumns}
                                dataSource={jobsData?.items ?? []}
                                rowKey="run_id"
                                size="small"
                                pagination={{
                                    current: jobsPage,
                                    pageSize: 20,
                                    total: jobsData?.total ?? 0,
                                    onChange: (p) => { setJobsPage(p); loadTrainingJobs(p, jobsStatusFilter); },
                                    showTotal: (t) => `共 ${t} 条`,
                                }}
                                locale={{ emptyText: jobsLoading ? ' ' : '暂无训练任务记录，点击「刷新」加载' }}
                            />
                        </Spin>
                    </Panel>
                  </div>
                ),
              },
            ]}
        />

            {/* 训练任务详情 Modal */}
            <Modal
                open={jobDetailVisible}
                onCancel={() => { setJobDetailVisible(false); setJobDetail(null); }}
                footer={null}
                width={720}
                title={
                    <div className="flex items-center gap-2 text-[14px] font-semibold text-slate-800">
                        <ThunderboltOutlined className="text-blue-500" />
                        {jobDetailDisplayName || '训练任务详情'}
                    </div>
                }
            >
                {jobDetailLoading ? (
                    <div className="flex items-center justify-center h-40"><Spin /></div>
                ) : jobDetail ? (
                    <div className="mt-4 space-y-4">
                        <Descriptions column={2} size="small" bordered>
                            {jobDetail.display_name && (
                                <Descriptions.Item label="展示名" span={2}>
                                    {jobDetail.display_name}
                                </Descriptions.Item>
                            )}
                            <Descriptions.Item label="任务 ID" span={2}>
                                <Typography.Text code className="text-[10px] break-all">{jobDetail.run_id}</Typography.Text>
                            </Descriptions.Item>
                            <Descriptions.Item label="状态">
                                <Tag color={JOB_STATUS_META[jobDetail.status as string]?.color ?? 'default'} className="font-bold">
                                    {jobStatusLabel(jobDetail.status as string)}
                                </Tag>
                            </Descriptions.Item>
                            <Descriptions.Item label="进度">
                                {jobDetail.status === 'running' ? (
                                    <Progress percent={jobDetail.progress} size="small" />
                                ) : <span className="admin-num text-xs text-slate-500">{jobDetail.progress ?? 0}%</span>}
                            </Descriptions.Item>
                            <Descriptions.Item label="用户">{jobDetail.user_id}</Descriptions.Item>
                            <Descriptions.Item label="租户">{jobDetail.tenant_id}</Descriptions.Item>
                            <Descriptions.Item label="创建时间" span={2}>
                                <span className="admin-num">
                                    {jobDetail.created_at ? new Date(jobDetail.created_at).toLocaleString('zh-CN') : '—'}
                                </span>
                            </Descriptions.Item>
                        </Descriptions>
                        {jobDetail.result?.model_registration && (
                            <div className="rounded-lg border border-green-200 bg-green-50 p-3">
                                <div className="mb-1 text-xs font-semibold text-green-700">已注册模型</div>
                                <div className="text-xs font-medium text-green-800">
                                    {jobDetail.registered_model_display_name || jobDetail.result.model_registration.model_id}
                                </div>
                                {jobDetail.registered_model_display_name && (
                                    <div className="mt-0.5 truncate admin-num text-[10px] text-green-600">
                                        {jobDetail.result.model_registration.model_id}
                                    </div>
                                )}
                            </div>
                        )}
                        {jobDetail.logs && (
                            <div>
                                <div className="text-xs font-semibold text-slate-500 mb-1">训练日志</div>
                                <pre className="p-3 bg-slate-900 text-green-300 text-[10px] rounded-xl overflow-auto max-h-48 font-mono whitespace-pre-wrap">
                                    {typeof jobDetail.logs === 'string' ? jobDetail.logs : JSON.stringify(jobDetail.logs, null, 2)}
                                </pre>
                            </div>
                        )}
                        {jobDetail.request_payload && (
                            <Collapse ghost size="small" items={[{
                                key: '1',
                                label: <span className="text-xs font-bold text-slate-500">请求参数（request_payload）</span>,
                                children: (
                                    <pre className="p-3 bg-slate-50 text-slate-700 text-[10px] rounded-xl overflow-auto max-h-40 font-mono whitespace-pre-wrap">
                                        {JSON.stringify(jobDetail.request_payload, null, 2)}
                                    </pre>
                                ),
                            }]} />
                        )}
                    </div>
                ) : (
                    <div className="text-center text-slate-400 py-10 text-sm">暂无数据</div>
                )}
            </Modal>

            {/* 详情 Modal */}
            <Modal
                open={detailVisible}
                onCancel={() => setDetailVisible(false)}
                footer={null}
                width={780}
                title={
                    <div className="flex items-center gap-2 text-[14px] font-semibold text-slate-800">
                        <FolderOpenOutlined className="text-amber-500" />
                        {resolveModelDisplayName(detailModel?.metadata, detailModel?.qlib_config) || detailModel?.model_id}
                        {detailModel?.is_production && (
                            <Tag color="green" className="ml-2 text-[10px]">生产</Tag>
                        )}
                    </div>
                }
            >
                {detailModel && (
                    <div className="space-y-4 mt-2">
                        {/* 基本信息 */}
                        <Descriptions size="small" column={2} bordered>
                            <Descriptions.Item label="模型 ID" span={2}>
                                <Text code className="text-[10px] break-all">{detailModel.model_id}</Text>
                            </Descriptions.Item>
                            <Descriptions.Item label="模型目录" span={2}>
                                <Text code className="text-[10px] break-all">{detailModel.dir_path}</Text>
                            </Descriptions.Item>
                            <Descriptions.Item label="特征维度">
                                <Tag color="blue" className="font-bold font-mono">
                                    {detailModel.feature_count ?? '—'}D
                                </Tag>
                            </Descriptions.Item>
                            <Descriptions.Item label="模型类">
                                <Text code className="text-[10px]">{detailModel.resolved_class || '—'}</Text>
                            </Descriptions.Item>
                            <Descriptions.Item label="模型格式">
                                {detailModel.model_format || '—'}
                            </Descriptions.Item>
                            <Descriptions.Item label="市场">
                                {(() => {
                                    const mkt = extractModelMarket(detailModel);
                                    const opt = MODEL_MARKET_OPTIONS.find(o => o.value === mkt);
                                    return opt && opt.value !== 'all' ? <Tag color={opt.color}>{opt.label}</Tag> : '—';
                                })()}
                            </Descriptions.Item>
                            <Descriptions.Item label="训练目标" style={{ textAlign: 'center' }} contentStyle={{ textAlign: 'center' }}>
                                {(() => {
                                    const targetMeta = resolveTrainingTargetMeta(detailModel.metadata);
                                    return targetMeta.horizonDays ? (
                                        <div className="flex justify-center">
                                            <Space size={4} align="center">
                                                <Tag color="blue" className="m-0 font-bold">
                                                    T+{targetMeta.horizonDays}
                                                </Tag>
                                                <span className="text-[10px] text-slate-500">
                                                    {targetMeta.targetMode === 'classification' ? '分类' : '回归'}
                                                </span>
                                            </Space>
                                        </div>
                                    ) : '—';
                                })()}
                            </Descriptions.Item>
                            <Descriptions.Item label="训练区间" span={2}>
                                <span className="font-mono text-xs">
                                    {detailModel.train_start || '—'} → {detailModel.train_end || '—'}
                                </span>
                            </Descriptions.Item>
                            <Descriptions.Item label="标签公式" span={2}>
                                {(() => {
                                    const targetMeta = resolveTrainingTargetMeta(detailModel.metadata);
                                    return targetMeta.labelFormula ? (
                                        <Text code className="text-[10px] break-all">
                                            {targetMeta.labelFormula}
                                        </Text>
                                    ) : '—';
                                })()}
                            </Descriptions.Item>
                            <Descriptions.Item label="训练窗口" span={2}>
                                {(() => {
                                    const targetMeta = resolveTrainingTargetMeta(detailModel.metadata);
                                    return targetMeta.trainingWindow ? (
                                        <Text code className="text-[10px] break-all">
                                            {targetMeta.trainingWindow}
                                        </Text>
                                    ) : '—';
                                })()}
                            </Descriptions.Item>
                            <Descriptions.Item label="SHA-256" span={2}>
                                <Text code className="text-[10px] break-all">
                                    {detailModel.sha256 || '—'}
                                </Text>
                            </Descriptions.Item>
                            <Descriptions.Item label="最近更新" span={2}>
                                {dayjs(detailModel.updated_at).format('YYYY-MM-DD HH:mm:ss')}
                            </Descriptions.Item>
                        </Descriptions>

                        <Collapse
                            ghost
                            size="small"
                            defaultActiveKey={['workflow', 'params']}
                            items={[
                                // 性能指标 (v10)
                                ...(detailModel.performance_metrics ? [{
                                    key: 'performance',
                                    label: <span className="font-bold text-slate-700 text-xs uppercase tracking-wide">模型性能指标 (IC/ICIR)</span>,
                                    children: (
                                        <PerformanceOverview metrics={detailModel.performance_metrics} />
                                    ),
                                }] : []),
                                // 特征描述 (feature_description.md)
                                ...(detailModel.feature_description ? [{
                                    key: 'features',
                                    label: <span className="font-bold text-slate-700 text-xs uppercase tracking-wide">特征描述看板 (Markdown)</span>,
                                    children: (
                                        <div className="p-4 bg-slate-50 rounded-xl max-h-96 overflow-auto border border-slate-100 prose prose-sm prose-slate max-w-none">
                                            <ReactMarkdown remarkPlugins={[remarkGfm]}>
                                                {detailModel.feature_description}
                                            </ReactMarkdown>
                                        </div>
                                    ),
                                }] : []),
                                // workflow_config
                                ...(detailModel.workflow_config ? [{
                                    key: 'workflow',
                                    label: <span className="font-bold text-slate-700 text-xs uppercase tracking-wide">workflow_config.yaml</span>,
                                    children: (
                                        <div className="space-y-4">
                                            <WorkflowSummary model={detailModel} />
                                            <pre className="p-3 bg-slate-900 text-slate-100 text-[10px] rounded-xl overflow-auto max-h-80 mt-2 font-mono">
                                                {JSON.stringify(detailModel.workflow_config, null, 2)}
                                            </pre>
                                        </div>
                                    ),
                                }] : []),
                                // qlib_config (v10 style)
                                ...(detailModel.qlib_config ? [{
                                    key: 'qlib_config',
                                    label: <span className="font-bold text-slate-700 text-xs uppercase tracking-wide">config.yaml (Qlib)</span>,
                                    children: (
                                        <div className="space-y-4">
                                            {!detailModel.workflow_config && <WorkflowSummary model={detailModel} />}
                                            <pre className="p-3 bg-slate-900 text-slate-100 text-[10px] rounded-xl overflow-auto max-h-80 mt-2 font-mono">
                                                {JSON.stringify(detailModel.qlib_config, null, 2)}
                                            </pre>
                                        </div>
                                    ),
                                }] : []),
                                // best_params
                                ...(detailModel.best_params ? [{
                                    key: 'params',
                                    label: <span className="font-bold text-slate-700 text-xs uppercase tracking-wide">best_params.yaml</span>,
                                    children: (
                                        <pre className="p-3 bg-slate-900 text-slate-100 text-[10px] rounded-xl overflow-auto max-h-80 mt-2 font-mono">
                                            {JSON.stringify(detailModel.best_params, null, 2)}
                                        </pre>
                                    ),
                                }] : []),
                                // metadata
                                ...(detailModel.metadata ? [{
                                    key: 'metadata',
                                    label: <span className="font-bold text-slate-700 text-xs uppercase tracking-wide">metadata.json</span>,
                                    children: (
                                        <pre className="p-3 bg-slate-900 text-slate-100 text-[10px] rounded-xl overflow-auto max-h-80 mt-2 font-mono">
                                            {JSON.stringify(detailModel.metadata, null, 2)}
                                        </pre>
                                    ),
                                }] : []),
                                // 文件列表
                                {
                                    key: 'files',
                                    label: (
                                        <span className="font-bold text-slate-700 text-xs uppercase tracking-wide">
                                            目录文件 ({detailModel.files.length})
                                        </span>
                                    ),
                                    children: (
                                        <div className="space-y-1">
                                            {detailModel.files.map(f => (
                                                <div key={f.name} className="flex justify-between items-center text-xs px-3 py-1.5 bg-slate-50 rounded-lg">
                                                    <Space>
                                                        <FileOutlined className="text-slate-400" />
                                                        <span className="font-mono text-slate-700">{f.name}</span>
                                                    </Space>
                                                    <Space className="text-slate-400">
                                                        <span>{fmtSize(f.size)}</span>
                                                        <span className="text-slate-300">|</span>
                                                        <span>{dayjs(f.modified_at).format('MM-DD HH:mm')}</span>
                                                    </Space>
                                                </div>
                                            ))}
                                        </div>
                                    ),
                                },
                            ]}
                        />
                    </div>
                )}
            </Modal>

        </div>
    );
};
