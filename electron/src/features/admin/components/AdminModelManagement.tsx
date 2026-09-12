import React, { useMemo, useState, useCallback } from 'react';
import {
    Table, Button, message, Space, Tag, Modal, Collapse, Descriptions,
    Badge, Tooltip, Typography, Spin, Tabs, Progress, Select, Segmented
} from 'antd';
import {
    ScanOutlined, FolderOpenOutlined,
    CheckCircleOutlined, FileOutlined, ReloadOutlined,
    ThunderboltOutlined, HistoryOutlined,
    GlobalOutlined
} from '@ant-design/icons';
import dayjs from 'dayjs';
import { useNavigate } from 'react-router-dom';
import { useDispatch } from 'react-redux';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { adminService } from '../services/adminService';
import { ModelDirectoryInfo, ModelScanResult } from '../types';
import { setCurrentTab } from '../../../store/slices/aiStrategySlice';

const { Panel } = Collapse;
const { Text, Link } = Typography;

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
    const navigate = useNavigate();
    const dispatch = useDispatch();
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


    const handleGoBacktestCenter = () => {
        dispatch(setCurrentTab('backtest'));
        navigate('/');
    };

    const handleQuickRescan = async () => {
        await handleScan(true);
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
            title: '模型目录',
            dataIndex: 'model_id',
            key: 'model_id',
            width: 280,
            render: (id: string, record: ModelDirectoryInfo) => {
                const meta = record.metadata || {};
                const qlib = record.qlib_config || {};
                const jobName = String(meta.job_name || qlib.job_name || '');
                const modelType = String(meta.model_type || qlib.model?.type || '');
                return (
                    <div className="max-w-full">
                        <Space size={4} className="w-full">
                            <FolderOpenOutlined className="text-amber-500 shrink-0" />
                            <Tooltip title={id}>
                                <Text
                                    strong
                                    className="text-slate-700 text-xs"
                                    ellipsis={{ tooltip: false }}
                                    style={{ width: record.is_production ? 120 : 200 }}
                                >
                                    {id}
                                </Text>
                            </Tooltip>
                            {record.is_production && (
                                <Tag color="green" className="text-[9px] font-bold px-1 m-0 shrink-0 border-none bg-green-50 text-green-600">
                                    PROD
                                </Tag>
                            )}
                            {record.error && (
                                <Tag color="red" className="text-[9px] m-0 shrink-0">ERR</Tag>
                            )}
                        </Space>
                        {(jobName || modelType) && (
                            <div className="flex items-center gap-1 mt-0.5 pl-4">
                                {modelType && (
                                    <Tag color="cyan" className="text-[9px] font-bold m-0">{modelType}</Tag>
                                )}
                                {jobName && (
                                    <Text className="text-[10px] text-slate-400 font-mono" ellipsis style={{ maxWidth: 140 }}>
                                        {jobName}
                                    </Text>
                                )}
                            </div>
                        )}
                    </div>
                );
            },
        },
        {
            title: '市场',
            key: 'market',
            width: 80,
            render: (_: any, record: ModelDirectoryInfo) => {
                const mkt = extractModelMarket(record);
                const opt = MODEL_MARKET_OPTIONS.find(o => o.value === mkt);
                return opt && opt.value !== 'all' ? (
                    <Tag color={opt.color} className="text-[10px] m-0">{opt.label}</Tag>
                ) : <span className="text-slate-300 text-xs">—</span>;
            },
        },
        {
            title: '模型类',
            dataIndex: 'resolved_class',
            key: 'resolved_class',
            render: (cls: string | null) => cls ? (
                <Tooltip title={cls}>
                    <Text code className="text-[10px]">{cls.split('.').pop()}</Text>
                </Tooltip>
            ) : <span className="text-slate-300 text-xs">—</span>,
        },
        {
            title: '特征维度',
            dataIndex: 'feature_count',
            key: 'feature_count',
            align: 'center' as const,
            render: (n: number | null) => n != null ? (
                <Tag color="blue" className="font-mono font-bold">{n}D</Tag>
            ) : <span className="text-slate-300 text-xs">—</span>,
        },
        {
            title: '训练/测试区间',
            key: 'train_range',
            render: (_: any, r: ModelDirectoryInfo) => (
                <div className="flex flex-col">
                    {r.train_start ? (
                        <span className="text-[10px] text-slate-500 font-mono">
                            <Tag className="m-0 text-[10px] scale-90" color="default">TRAIN</Tag> {r.train_start} → {r.train_end}
                        </span>
                    ) : null}
                    {r.test_start ? (
                        <span className="text-[10px] text-indigo-500 font-mono mt-0.5">
                            <Tag className="m-0 text-[10px] scale-90" color="indigo">TEST</Tag> {r.test_start} → {r.test_end}
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
            render: (_: any, r: ModelDirectoryInfo) => {
                const targetMeta = resolveTrainingTargetMeta(r.metadata);
                if (!targetMeta.horizonDays) {
                    return <span className="text-slate-300 text-xs">—</span>;
                }

                return (
                    <div className="flex flex-col gap-1 items-center text-center">
                        <Tag color="blue" className="m-0 font-bold">
                            T+{targetMeta.horizonDays}
                        </Tag>
                        <span className="text-[10px] text-slate-500">
                            {targetMeta.targetMode === 'classification' ? '分类' : '回归'}
                        </span>
                        {targetMeta.labelFormula && (
                            <Text code className="text-[10px] break-all text-center">
                                {targetMeta.labelFormula}
                            </Text>
                        )}
                    </div>
                );
            },
        },
        {
            title: '格式',
            dataIndex: 'model_format',
            key: 'model_format',
            render: (fmt: string | null) => fmt ? (
                <Tag color="purple" className="text-[10px] uppercase">{fmt}</Tag>
            ) : <span className="text-slate-300 text-xs">—</span>,
        },
        {
            title: '文件数',
            key: 'files',
            align: 'center' as const,
            render: (_: any, r: ModelDirectoryInfo) => (
                <Badge count={r.files?.length || 0} color="geekblue"
                    className="font-mono" />
            ),
        },
        {
            title: '最近更新',
            dataIndex: 'updated_at',
            key: 'updated_at',
            render: (d: string) => (
                <span className="text-xs text-slate-400 font-mono">
                    {dayjs(d).format('YYYY-MM-DD HH:mm')}
                </span>
            ),
        },
        {
            title: '操作',
            key: 'action',
            align: 'right' as const,
            render: (_: any, record: ModelDirectoryInfo) => (
                <Button
                    size="small"
                    type="link"
                    className="font-bold"
                    onClick={() => handleViewDetail(record)}
                >
                    查看详情
                </Button>
            ),
        },
    ];

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
                  <div className="space-y-6 pt-2">
            {/* 标题栏 */}
            <div className="flex justify-between items-center">
                <div>
                    <h3 className="text-xl font-black text-slate-800 tracking-tight">模型库管理</h3>
                    <p className="text-slate-400 text-xs mt-1 italic">
                        自动扫描 models/ 目录，聚合 metadata.json / workflow_config.yaml / best_params.yaml
                    </p>
                </div>
                <Space size="middle">
                    {scanResult && (
                        <Button
                            icon={<ReloadOutlined />}
                            loading={scanning}
                            className="rounded-xl h-10 px-4 font-bold"
                            onClick={() => handleScan(true)}
                            title="跳过 5 分钟缓存，强制重新扫描磁盘"
                        >
                            强制刷新
                        </Button>
                    )}
                    <Button
                        type="primary"
                        icon={<ScanOutlined />}
                        loading={scanning}
                        className="rounded-xl h-10 px-6 bg-slate-900 border-none font-bold shadow-lg shadow-slate-200"
                        onClick={() => handleScan(false)}
                    >
                        {scanning ? '扫描中…' : scanResult ? '重新扫描' : '开始扫描'}
                    </Button>
                </Space>
            </div>

            {/* 错误提示 */}
            {scanError && !scanning && (
                <div className="px-4 py-3 bg-rose-50 border border-rose-100 rounded-2xl text-xs text-rose-700">
                    <strong className="font-bold">扫描失败：</strong>{scanError}
                </div>
            )}

            {/* 首次未扫描提示 */}
            {!scanResult && !scanning && !scanError && (
                <div className="px-4 py-8 bg-slate-50 rounded-2xl text-center text-slate-500">
                    <ScanOutlined className="text-3xl text-slate-300 mb-2" />
                    <p className="text-sm">点击右上角 <strong className="text-slate-700">"开始扫描"</strong> 加载模型目录</p>
                    <p className="text-xs text-slate-400 mt-1">扫描结果会缓存 5 分钟，重复点击不会再次读盘</p>
                </div>
            )}

            {/* 扫描统计 */}
            {scanResult && !scanning && (
                <div className="flex items-center justify-between px-4 py-2 bg-slate-50 rounded-2xl text-xs text-slate-500">
                    <Space>
                        <CheckCircleOutlined className="text-green-500" />
                        共发现 <span className="font-bold text-slate-800">{scanResult.total}</span> 个模型目录
                        （生产：{scanResult.models.filter(m => m.is_production).length} 个）
                        {scanResult.from_cache && (
                            <Tag color="blue" className="text-[10px] m-0">缓存命中</Tag>
                        )}
                        {modelMarketFilter !== 'all' && (
                            <span>· 当前筛选：<Tag color={MODEL_MARKET_OPTIONS.find(o => o.value === modelMarketFilter)?.color} className="m-0">{MODEL_MARKET_OPTIONS.find(o => o.value === modelMarketFilter)?.label}</Tag> {filteredModels.length} 个</span>
                        )}
                    </Space>
                    <Segmented
                        size="small"
                        value={modelMarketFilter}
                        onChange={(val) => setModelMarketFilter(val as string)}
                        options={MODEL_MARKET_OPTIONS.map(m => ({ value: m.value, label: m.label }))}
                    />
                </div>
            )}

            {/* 模型列表 */}
            <Spin spinning={scanning} tip="正在扫描模型目录…">
                <Table
                    columns={columns.map(c => ({ ...c, align: 'center' as const }))}
                    dataSource={filteredModels}
                    rowKey="model_id"
                    pagination={{ pageSize: 10 }}
                    scroll={{ x: 'max-content' }}
                    className="admin-table border-none shadow-sm rounded-3xl overflow-hidden"
                    locale={{ emptyText: scanning ? ' ' : '暂无模型，点击"重新扫描"加载' }}
                />
            </Spin>
                  </div>
                ),
              },
              {
                key: 'training-jobs',
                label: <span className="font-bold text-xs px-1"><HistoryOutlined className="mr-1.5" />训练任务</span>,
                children: (
                  <div className="space-y-4 pt-2">
                    <div className="flex justify-between items-center">
                        <div>
                            <h3 className="text-xl font-black text-slate-800 tracking-tight">训练任务历史</h3>
                            <p className="text-slate-400 text-xs mt-1 italic">管理员查看所有用户的模型训练任务记录</p>
                        </div>
                        <Space>
                            <Select
                                placeholder="按状态筛选"
                                allowClear
                                value={jobsStatusFilter}
                                onChange={(val) => {
                                    setJobsStatusFilter(val);
                                    setJobsPage(1);
                                    loadTrainingJobs(1, val);
                                }}
                                className="w-36"
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
                                icon={<ReloadOutlined />}
                                className="rounded-xl h-9 border-slate-200 font-bold text-xs"
                                loading={jobsLoading}
                                onClick={() => loadTrainingJobs(jobsPage, jobsStatusFilter)}
                            >
                                刷新
                            </Button>
                        </Space>
                    </div>
                    <Spin spinning={jobsLoading}>
                        <Table
                            columns={[
                                {
                                    title: '任务 ID',
                                    dataIndex: 'run_id',
                                    key: 'run_id',
                                    render: (id: string) => (
                                        <Tooltip title={id}>
                                            <Typography.Text code className="text-[10px]">
                                                {id.length > 28 ? `${id.slice(0, 28)}…` : id}
                                            </Typography.Text>
                                        </Tooltip>
                                    ),
                                },
                                {
                                    title: '用户',
                                    key: 'user',
                                    render: (_: any, r: any) => (
                                        <div>
                                            <div className="text-xs font-bold text-slate-700">{r.user_id}</div>
                                            <div className="text-[10px] text-slate-400">{r.tenant_id}</div>
                                        </div>
                                    ),
                                },
                                {
                                    title: '状态',
                                    dataIndex: 'status',
                                    key: 'status',
                                    render: (status: string, r: any) => {
                                        const colorMap: Record<string, string> = {
                                            completed: 'green', failed: 'red', running: 'blue',
                                            pending: 'default', provisioning: 'purple', waiting_callback: 'gold',
                                        };
                                        return (
                                            <div>
                                                <Tag color={colorMap[status] ?? 'default'} className="font-bold text-[10px]">
                                                    {status}
                                                </Tag>
                                                {status === 'running' && (
                                                    <Progress percent={r.progress} size="small" className="mt-1 w-24" />
                                                )}
                                            </div>
                                        );
                                    },
                                },
                                {
                                    title: '模型类型 / 特征数',
                                    key: 'model_info',
                                    render: (_: any, r: any) => (
                                        <div>
                                            {r.model_type && <Tag color="cyan" className="text-[10px] font-bold">{r.model_type}</Tag>}
                                            {r.features_count > 0 && (
                                                <span className="text-[10px] text-slate-400">{r.features_count} 个特征</span>
                                            )}
                                        </div>
                                    ),
                                },
                                {
                                    title: '训练区间',
                                    key: 'train_range',
                                    render: (_: any, r: any) => r.train_start ? (
                                        <span className="text-[10px] font-mono text-slate-500">
                                            {r.train_start} → {r.train_end}
                                        </span>
                                    ) : <span className="text-slate-300">—</span>,
                                },
                                {
                                    title: '注册模型',
                                    dataIndex: 'registered_model_id',
                                    key: 'registered_model_id',
                                    render: (id: string) => id ? (
                                        <Tag color="green" className="text-[10px] font-mono font-bold">{id}</Tag>
                                    ) : <span className="text-slate-300 text-xs">—</span>,
                                },
                                {
                                    title: '创建时间',
                                    dataIndex: 'created_at',
                                    key: 'created_at',
                                    render: (d: string) => (
                                        <span className="text-xs text-slate-400 font-mono">
                                            {d ? new Date(d).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : '—'}
                                        </span>
                                    ),
                                },
                                {
                                    title: '操作',
                                    key: 'action',
                                    render: (_: any, r: any) => (
                                        <Button
                                            type="link"
                                            size="small"
                                            className="font-bold"
                                            onClick={() => handleOpenJobDetail(r.run_id)}
                                        >
                                            详情
                                        </Button>
                                    ),
                                },
                            ]}
                            dataSource={jobsData?.items ?? []}
                            rowKey="run_id"
                            pagination={{
                                current: jobsPage,
                                pageSize: 20,
                                total: jobsData?.total ?? 0,
                                onChange: (p) => { setJobsPage(p); loadTrainingJobs(p, jobsStatusFilter); },
                                showTotal: (t) => `共 ${t} 条`,
                            }}
                            className="admin-table border-none shadow-sm rounded-3xl overflow-hidden"
                            locale={{ emptyText: jobsLoading ? ' ' : '暂无训练任务记录，点击刷新加载' }}
                        />
                    </Spin>
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
                    <div className="font-black text-slate-800 flex items-center gap-2">
                        <ThunderboltOutlined className="text-blue-500" />
                        训练任务详情
                    </div>
                }
            >
                {jobDetailLoading ? (
                    <div className="flex items-center justify-center h-40"><Spin /></div>
                ) : jobDetail ? (
                    <div className="space-y-4 mt-4">
                        <Descriptions column={2} size="small" bordered>
                            <Descriptions.Item label="任务 ID" span={2}>
                                <Typography.Text code className="text-[10px] break-all">{jobDetail.run_id}</Typography.Text>
                            </Descriptions.Item>
                            <Descriptions.Item label="状态">
                                <Tag color={{ completed: 'green', failed: 'red', running: 'blue', pending: 'default' }[jobDetail.status as string] ?? 'default'} className="font-bold">
                                    {jobDetail.status}
                                </Tag>
                            </Descriptions.Item>
                            <Descriptions.Item label="进度">
                                {jobDetail.status === 'running' ? (
                                    <Progress percent={jobDetail.progress} size="small" />
                                ) : <span className="text-slate-500 text-xs">{jobDetail.progress ?? 0}%</span>}
                            </Descriptions.Item>
                            <Descriptions.Item label="用户">{jobDetail.user_id}</Descriptions.Item>
                            <Descriptions.Item label="租户">{jobDetail.tenant_id}</Descriptions.Item>
                            <Descriptions.Item label="创建时间" span={2}>
                                {jobDetail.created_at ? new Date(jobDetail.created_at).toLocaleString('zh-CN') : '—'}
                            </Descriptions.Item>
                        </Descriptions>
                        {jobDetail.result?.model_registration && (
                            <div className="p-3 bg-green-50 rounded-xl border border-green-200">
                                <div className="text-xs font-bold text-green-700 mb-1">✅ 已注册模型</div>
                                <div className="text-xs font-mono text-green-600">
                                    model_id: {jobDetail.result.model_registration.model_id}
                                </div>
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
                    <div className="font-black text-slate-800 flex items-center gap-2">
                        <FolderOpenOutlined className="text-amber-500" />
                        {detailModel?.model_id}
                        {detailModel?.is_production && (
                            <Tag color="green" className="ml-2 text-[10px]">PRODUCTION</Tag>
                        )}
                    </div>
                }
            >
                {detailModel && (
                    <div className="space-y-4 mt-2">
                        {/* 基本信息 */}
                        <Descriptions size="small" column={2} bordered>
                            <Descriptions.Item label="模型目录">
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
