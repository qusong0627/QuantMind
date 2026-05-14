import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ArrowLeft, CheckCircle2, ClipboardList, Loader2, Search, Zap, BarChart3, User as UserIcon, Settings2, Sparkles, Filter, Link, ArrowRight, Layers, TrendingUp, Activity, History, Cpu, Clock, Brain, Target, Database, Code, Calendar, Wand2, Eye, Play, CreditCard, Binary, ShieldAlert, Info, TerminalSquare, AlertTriangle } from 'lucide-react';
import { DatePicker, Empty, Input, Select, Spin, Tag, message, Tooltip, Badge } from 'antd';
import dayjs from 'dayjs';
import utc from 'dayjs/plugin/utc';
import timezone from 'dayjs/plugin/timezone';
dayjs.extend(utc);
dayjs.extend(timezone);
import { strategyManagementService } from '../../../services/strategyManagementService';
import {
    modelTrainingService,
    type InferenceRankingResult,
    type InferenceRunRecord,
    type UserModelRecord,
} from '../../../services/modelTrainingService';
import { 
    getMeta, 
    getMetrics, 
    resolveMetricNumber, 
    extractModelType, 
} from '../../modelRegistryUtils';
import { normalizeStockCode } from '../../../utils/portfolioUtils';
import type {
    ManualExecutionLogEntry,
    ManualExecutionLogSnapshot,
    ManualExecutionPreview,
    ManualExecutionPreviewOrder,
    ManualExecutionTaskRecord,
} from '../../../services/realTradingService';
import type { StrategyFile } from '../../../types/backtest/strategy';

interface ManualTaskPageProps {
    tenantId: string;
    userId: string;
    tradingMode?: 'real' | 'simulation';
    onBack?: () => void;
}

const STEP_TITLES = ['选择模型', '选择推理批次', '选择策略', '生成调仓预案', '确认提交'];
const TERMINAL_STATUSES = new Set(['completed', 'failed']);

const loadRealTradingService = async () => {
    const module = await import('../../../services/realTradingService');
    return module.realTradingService;
};

/** 解析训练窗口范围字符串 (格式: "2020-01-01 -> 2020-12-31 | ...") */
const parseTrainingWindowRanges = (raw: unknown): Array<[string, string]> => {
    if (typeof raw !== 'string' || !raw.trim()) return [];
    const text = raw.replaceAll('→', '->').replaceAll('—', '-');
    const segments = text.split('|').map((item) => item.trim()).filter(Boolean);
    const ranges: Array<[string, string]> = [];
    for (const segment of segments) {
        const matched = segment.match(/(\d{4}-\d{2}-\d{2})\s*->\s*(\d{4}-\d{2}-\d{2})/);
        if (matched) {
            ranges.push([matched[1], matched[2]]);
        }
    }
    return ranges;
};

/** 从元数据中提取时间周期 */
const extractTimePeriods = (meta: Record<string, any>) => {
    const windowRanges = parseTrainingWindowRanges(meta.training_window ?? meta.trainingWindow);
    const ts = meta.train_start ?? meta.trainStart ?? windowRanges[0]?.[0];
    const te = meta.train_end ?? meta.trainEnd ?? windowRanges[0]?.[1];
    if (!ts || !te) return null;
    const vs = meta.val_start ?? meta.valStart ?? windowRanges[1]?.[0];
    const ve = meta.val_end ?? meta.valEnd ?? windowRanges[1]?.[1];
    const xs = meta.test_start ?? meta.testStart ?? windowRanges[2]?.[0];
    const xe = meta.test_end ?? meta.testEnd ?? windowRanges[2]?.[1];
    return {
        train: [String(ts).slice(0, 10), String(te).slice(0, 10)] as [string, string],
        val: vs && ve ? [String(vs).slice(0, 10), String(ve).slice(0, 10)] as [string, string] : null,
        test: xs && xe ? [String(xs).slice(0, 10), String(xe).slice(0, 10)] as [string, string] : null,
    };
};

/** 将 UTC 无时区后缀的时间字符串转为上海时区显示 */
const toShanghaiTime = (raw?: string | null): string => {
    if (!raw) return '-';
    try {
        return dayjs.utc(raw).tz('Asia/Shanghai').format('YYYY-MM-DD HH:mm:ss');
    } catch {
        return raw;
    }
};

const STAGE_LABELS: Record<string, string> = {
    queued: '排队中',
    validating: '校验中',
    signal_loading: '加载信号',
    dispatching: '派发订单',
    running: '执行中',
    completed: '提交完成',
    failed: '已失败',
};

const stageLabel = (value?: string | null) => STAGE_LABELS[String(value || '').trim().toLowerCase()] || String(value || '-');

const statusTone = (index: number, currentStep: number) => {
    if (index < currentStep) return 'border-emerald-200 bg-emerald-50 text-emerald-700';
    if (index === currentStep) return 'border-blue-200 bg-blue-50 text-blue-700';
    return 'border-gray-200 bg-white text-gray-400';
};

const renderMoney = (value?: number) => {
    if (!Number.isFinite(value)) return '--';
    return `¥${Number(value).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
};

const modelDisplayName = (model: any) => {
    if (!model) return '-';
    const meta = getMeta(model as any);
    return (meta.display_name || meta.model_name || model.model_id) as string;
};

const renderOrderCard = (order: ManualExecutionPreviewOrder, tone: 'buy' | 'sell') => (
    <div
        key={`${order.side}-${order.symbol}-${order.quantity}`}
        className={`group relative overflow-hidden rounded-xl border transition-all duration-300 hover:shadow-md ${
            tone === 'sell' 
                ? 'border-rose-100 bg-white hover:border-rose-300' 
                : 'border-emerald-100 bg-white hover:border-emerald-300'
        }`}
    >
        <div className={`absolute top-0 left-0 w-1 h-full ${tone === 'sell' ? 'bg-rose-500' : 'bg-emerald-500'}`} />
        <div className="p-3">
            <div className="flex items-center justify-between gap-2.5 mb-2.5">
                <div className="flex items-center gap-2">
                    <div className={`w-7 h-7 rounded-lg flex items-center justify-center font-bold text-[10px] ${tone === 'sell' ? 'bg-rose-50 text-rose-600' : 'bg-emerald-50 text-emerald-600'}`}>
                        {tone === 'sell' ? 'S' : 'B'}
                    </div>
                    <div>
                        <div className="font-mono text-xs font-bold text-gray-900 leading-none">{order.symbol}</div>
                        <div className="text-[9px] text-gray-400 mt-1 uppercase tracking-tighter">{order.reason || (tone === 'sell' ? 'EXIT' : 'ENTRY')}</div>
                    </div>
                </div>
                <Tag color={tone === 'sell' ? 'volcano' : 'cyan'} bordered={false} className="m-0 text-[10px] font-bold px-1.5 py-0 rounded-md">
                    {order.side}
                </Tag>
            </div>
            
            <div className="grid grid-cols-2 gap-x-4 gap-y-2 pt-2.5 border-t border-gray-50 text-[10px]">
                <div className="flex flex-col">
                    <span className="text-gray-400 font-bold uppercase tracking-tighter">数量</span>
                    <span className="font-mono text-gray-900 font-bold">{order.quantity.toLocaleString()}</span>
                </div>
                <div className="flex flex-col">
                    <span className="text-gray-400 font-bold uppercase tracking-tighter">委托</span>
                    <span className="font-mono text-gray-900 font-bold">{order.order_type}</span>
                </div>
                <div className="flex flex-col">
                    <span className="text-gray-400 font-bold uppercase tracking-tighter">目标价</span>
                    <span className="font-mono text-blue-600 font-bold">
                        {order.price === 0 ? <span className="text-gray-400 font-medium">未获取</span> : renderMoney(order.price)}
                    </span>
                </div>
                <div className="flex flex-col">
                    <span className="text-gray-400 font-bold uppercase tracking-tighter">预估金额</span>
                    <span className="font-mono text-gray-900 font-bold">
                        {order.price === 0 ? <span className="text-gray-300">--</span> : renderMoney(order.estimated_notional)}
                    </span>
                </div>
            </div>

            <div className="mt-2.5 flex items-center justify-between bg-gray-50/50 rounded-lg p-2 text-[9px] font-medium">
                <span className="text-gray-400">持仓: <span className="text-gray-700">{order.current_volume ?? 0}</span></span>
                <span className="text-gray-400">Ref: {order.reference_price === 0 ? '未获取' : renderMoney(order.reference_price)}</span>
            </div>
        </div>
    </div>
);

const ManualTaskPage: React.FC<ManualTaskPageProps> = ({ tradingMode, onBack }) => {
    const [currentStep, setCurrentStep] = useState(0);

    const [strategies, setStrategies] = useState<StrategyFile[]>([]);
    const [defaultModel, setDefaultModel] = useState<UserModelRecord | null>(null);
    const [userModels, setUserModels] = useState<UserModelRecord[]>([]);
    const [manualModelId, setManualModelId] = useState('');

    const [runIdQuery, setRunIdQuery] = useState('');
    const [dateQuery, setDateQuery] = useState('');
    const [runs, setRuns] = useState<InferenceRunRecord[]>([]);
    const [total, setTotal] = useState(0);
    const [page, setPage] = useState(1);
    const [loadingRuns, setLoadingRuns] = useState(false);
    const [selectedRunId, setSelectedRunId] = useState('');
    const [selectedRunDetail, setSelectedRunDetail] = useState<InferenceRankingResult | null>(null);
    const [loadingRunDetail, setLoadingRunDetail] = useState(false);

    const [selectedStrategyId, setSelectedStrategyId] = useState('');
    const [note, setNote] = useState('');

    const [preview, setPreview] = useState<ManualExecutionPreview | null>(null);
    const [previewLoading, setPreviewLoading] = useState(false);

    const [submitting, setSubmitting] = useState(false);
    const [selectedTaskId, setSelectedTaskId] = useState('');
    const [selectedTask, setSelectedTask] = useState<ManualExecutionTaskRecord | null>(null);
    const [logs, setLogs] = useState<ManualExecutionLogEntry[]>([]);
    const [snapshot, setSnapshot] = useState<ManualExecutionLogSnapshot | null>(null);

    const cursorRef = useRef('0-0');
    const logsRef = useRef<ManualExecutionLogEntry[]>([]);
    const viewportRef = useRef<HTMLDivElement | null>(null);

    const isRealMode = tradingMode !== 'simulation';
    const effectiveModelId = useMemo(() => manualModelId.trim() || defaultModel?.model_id || 'model_qlib', [defaultModel, manualModelId]);

    const [modelSearch, setModelSearch] = useState('');
    const [strategySearch, setStrategySearch] = useState('');

    const filteredModelOptions = useMemo(() => {
        const q = modelSearch.toLowerCase().trim();
        return userModels.filter(m => {
            const name = modelDisplayName(m).toLowerCase();
            return name.includes(q) || m.model_id.toLowerCase().includes(q);
        });
    }, [userModels, modelSearch]);

    const selectedModelObject = useMemo(() => {
        return filteredModelOptions.find(m => m.model_id === effectiveModelId) || null;
    }, [effectiveModelId, filteredModelOptions]);

    const filteredStrategyOptions = useMemo(() => {
        const q = strategySearch.toLowerCase().trim();
        return strategies
            .filter((s) => !s.is_system) // 过滤掉系统模板，只显示个人策略
            .filter(s => s.name.toLowerCase().includes(q) || s.id.toLowerCase().includes(q))
            .sort((a, b) => {
                const idA = Number(a.id);
                const idB = Number(b.id);
                if (!isNaN(idA) && !isNaN(idB)) return idA - idB;
                return String(a.id).localeCompare(String(b.id));
            });
    }, [strategies, strategySearch]);

    const strategyOptions = useMemo(
        () =>
            strategies
                .filter((strategy) => strategy.is_verified && /^\d+$/.test(strategy.id))
                .map((strategy) => ({
                    label: strategy.is_system ? `(内置) ${strategy.name}` : strategy.name,
                    value: strategy.id,
                })),
        [strategies],
    );

    const selectedRun = useMemo(
        () => runs.find((item) => item.run_id === selectedRunId) || selectedRunDetail?.summary || null,
        [runs, selectedRunDetail, selectedRunId],
    );
    const selectedStrategy = useMemo(
        () => strategies.find((item) => item.id === selectedStrategyId) || null,
        [selectedStrategyId, strategies],
    );

    const loadStrategies = useCallback(async () => {
        try {
            setStrategies(await strategyManagementService.loadStrategies());
        } catch (error) {
            console.error('Failed to load strategies', error);
            message.error('无法加载策略列表');
        }
    }, []);

    const loadModels = useCallback(async () => {
        try {
            const [defaultRes, userRes] = await Promise.all([
                modelTrainingService.getDefaultModel(),
                modelTrainingService.listUserModels(),
            ]);
            setDefaultModel(defaultRes || null);
            setUserModels(userRes.items || []);
            if (!manualModelId && defaultRes?.model_id) {
                setManualModelId(defaultRes.model_id);
            }
        } catch (error) {
            console.warn('Failed to load models', error);
        }
    }, [manualModelId]);

    const loadRuns = useCallback(async (nextPage = 1) => {
        if (!effectiveModelId) return;
        setLoadingRuns(true);
        try {
            const result = await modelTrainingService.listInferenceHistory(effectiveModelId, {
                runId: runIdQuery.trim() || undefined,
                status: 'completed',
                inferenceDate: dateQuery.trim() || undefined,
                page: nextPage,
                pageSize: 10,
            });
            setRuns(result.items || []);
            setTotal(result.total || 0);
            setPage(result.page || nextPage);
            if (!selectedRunId && result.items?.[0]?.run_id) {
                setSelectedRunId(result.items[0].run_id);
            }
        } catch (error) {
            console.warn('Failed to load inference runs', error);
            message.error('加载推理批次失败');
        } finally {
            setLoadingRuns(false);
        }
    }, [dateQuery, effectiveModelId, runIdQuery, selectedRunId]);

    const loadRunDetail = useCallback(async (runId: string) => {
        if (!runId) return;
        setLoadingRunDetail(true);
        try {
            setSelectedRunDetail(await modelTrainingService.getInferenceResult(runId));
        } catch (error) {
            console.warn('Failed to load inference detail', error);
            setSelectedRunDetail(null);
            message.error('加载推理明细失败');
        } finally {
            setLoadingRunDetail(false);
        }
    }, []);

    const refreshTask = useCallback(async (taskId: string) => {
        if (!taskId) return null;
        try {
            const realTradingService = await loadRealTradingService();
            const task = await realTradingService.getManualExecution(taskId);
            setSelectedTask(task);
            return task;
        } catch (error) {
            console.warn('Failed to load manual execution task', error);
            return null;
        }
    }, []);

    const loadTaskLogs = useCallback(async (taskId: string, afterId = '0-0', reset = false) => {
        if (!taskId) return;
        try {
            const realTradingService = await loadRealTradingService();
            const result = await realTradingService.getManualExecutionLogs(taskId, afterId, 200);
            setSnapshot(result.snapshot || null);
            cursorRef.current = result.next_id || afterId;
            const merged = reset ? (result.entries || []) : (() => {
                const next = [...logsRef.current];
                for (const entry of result.entries || []) {
                    if (!next.some((item) => item.id === entry.id)) next.push(entry);
                }
                return next;
            })();
            logsRef.current = merged;
            setLogs(merged);
            if (result.task) setSelectedTask(result.task);
        } catch (error) {
            console.warn('Failed to load manual execution logs', error);
        }
    }, []);

    useEffect(() => {
        void loadStrategies();
        void loadModels();
    }, [loadModels, loadStrategies]);

    useEffect(() => {
        if (effectiveModelId) {
            void loadRuns(1);
        }
    }, [effectiveModelId, loadRuns]);

    useEffect(() => {
        if (selectedRunId) {
            void loadRunDetail(selectedRunId);
        }
    }, [loadRunDetail, selectedRunId]);

    useEffect(() => {
        if (!selectedTaskId) return;
        let cancelled = false;
        let timer: number | undefined;
        const poll = async () => {
            if (cancelled) return;
            const task = await refreshTask(selectedTaskId);
            if (!cancelled && task) {
                const reset = logsRef.current.length === 0;
                await loadTaskLogs(selectedTaskId, cursorRef.current || '0-0', reset);
                if (TERMINAL_STATUSES.has(String(task.status || '').toLowerCase())) {
                    cancelled = true;
                    // 终态后再做一次最终刷新，确保展示 DB 最新数据
                    await refreshTask(selectedTaskId);
                    return;
                }
            }
            if (!cancelled) {
                timer = window.setTimeout(() => void poll(), 2000);
            }
        };
        void poll();
        return () => {
            cancelled = true;
            if (timer) window.clearTimeout(timer);
        };
    }, [loadTaskLogs, refreshTask, selectedTaskId]);

    useEffect(() => {
        const node = viewportRef.current;
        if (node) node.scrollTop = node.scrollHeight;
    }, [logs.length]);

    const resetDownstreamFromModel = (value: string) => {
        setManualModelId(value);
        setSelectedRunId('');
        setSelectedRunDetail(null);
        setSelectedStrategyId('');
        setPreview(null);
        setSelectedTaskId('');
        setSelectedTask(null);
        setLogs([]);
        setSnapshot(null);
        logsRef.current = [];
        cursorRef.current = '0-0';
        setCurrentStep(0);
    };

    const resetPreviewAndTask = () => {
        setPreview(null);
        setSelectedTaskId('');
        setSelectedTask(null);
        setLogs([]);
        setSnapshot(null);
        logsRef.current = [];
        cursorRef.current = '0-0';
    };

    const generatePreview = useCallback(async () => {
        if (!effectiveModelId || !selectedRunId || !selectedStrategyId) {
            message.warning('请先完成模型、推理批次和策略选择');
            return;
        }
        if (!isRealMode) {
            message.warning('引导式手动任务首版仅支持 REAL 模式');
            return;
        }
        setPreviewLoading(true);
        try {
            const realTradingService = await loadRealTradingService();
            const result = await realTradingService.previewManualExecution({
                model_id: effectiveModelId,
                run_id: selectedRunId,
                strategy_id: selectedStrategyId,
                trading_mode: 'REAL',
                note: note.trim() || undefined,
            });
            setPreview(result);
            message.success('调仓预案已生成');
        } catch (error: any) {
            const msg = error?.response?.data?.detail || error?.message || '生成调仓预案失败';
            message.error(String(msg));
            setPreview(null);
        } finally {
            setPreviewLoading(false);
        }
    }, [effectiveModelId, isRealMode, note, selectedRunId, selectedStrategyId]);

    const submitExecution = useCallback(async () => {
        if (!preview || !effectiveModelId || !selectedRunId || !selectedStrategyId) {
            message.warning('请先生成调仓预案');
            return;
        }
        setSubmitting(true);
        try {
            const realTradingService = await loadRealTradingService();
            const result = await realTradingService.createManualExecution({
                model_id: effectiveModelId,
                run_id: selectedRunId,
                strategy_id: selectedStrategyId,
                trading_mode: 'REAL',
                preview_hash: preview.preview_hash,
                note: note.trim() || undefined,
            });
            const taskId = String(result.task_id || '');
            if (taskId) {
                setSelectedTaskId(taskId);
                await refreshTask(taskId);
                await loadTaskLogs(taskId, '0-0', true);
                message.success('调仓预案已提交到执行队列');
            }
        } catch (error: any) {
            const msg = error?.response?.data?.detail || error?.message || '提交执行失败';
            message.error(String(msg));
        } finally {
            setSubmitting(false);
        }
    }, [effectiveModelId, loadTaskLogs, note, preview, refreshTask, selectedRunId, selectedStrategyId]);

    const previewSummary = preview?.summary;
    const previewTaskSummary = (selectedTask?.result_json as Record<string, unknown> | undefined)?.preview_summary as Record<string, unknown> | undefined;

    return (
        <div className="h-full overflow-y-auto bg-gray-50 p-4 pb-32 custom-scrollbar">
            <div className="flex items-center justify-between mb-4">
                <div className="flex items-center gap-3">
                    <div className="p-2 rounded-xl bg-blue-50 text-blue-600">
                        <ClipboardList size={18} />
                    </div>
                    <div>
                        <div className="text-lg font-bold text-gray-900">手动任务</div>
                        <div className="text-[11px] text-gray-500 font-medium">采用 5 步向导式执行流程，经由 Hash 核对后推送到 QMT 柜台</div>
                    </div>
                </div>
                <div className="flex items-center gap-3">
                    <div className="flex items-center gap-2 mr-2">
                        <button
                            type="button"
                            disabled={currentStep === 0}
                            onClick={() => {
                                if (currentStep === 3) setPreview(null);
                                setCurrentStep(Math.max(0, currentStep - 1));
                            }}
                            className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-xl border border-gray-200 text-xs font-bold text-gray-600 hover:bg-gray-100 disabled:opacity-30 disabled:cursor-not-allowed transition-all"
                        >
                            <ArrowLeft size={14} />
                            上一步
                        </button>
                        {currentStep < 4 ? (
                            <button
                                type="button"
                                disabled={
                                    (currentStep === 1 && !selectedRunId) ||
                                    (currentStep === 2 && !selectedStrategyId) ||
                                    (currentStep === 3 && !preview)
                                }
                                onClick={() => {
                                    if (currentStep === 3) {
                                        setCurrentStep(4);
                                    } else {
                                        setCurrentStep(currentStep + 1);
                                    }
                                }}
                                className="inline-flex items-center gap-1.5 px-4 py-1.5 rounded-xl bg-blue-600 text-white text-xs font-bold hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed shadow-md shadow-blue-100 transition-all active:scale-[0.98]"
                            >
                                下一步
                                <ArrowRight size={14} />
                            </button>
                        ) : (
                            <button
                                type="button"
                                onClick={() => {
                                    setPreview(null);
                                    setSelectedTaskId('');
                                    setSelectedTask(null);
                                    setLogs([]);
                                    setCurrentStep(0);
                                }}
                                className="inline-flex items-center gap-1.5 px-4 py-1.5 rounded-xl bg-emerald-600 text-white text-xs font-bold hover:bg-emerald-700 transition-all"
                            >
                                完成任务
                                <CheckCircle2 size={14} />
                            </button>
                        )}
                    </div>
                </div>
            </div>

            {!isRealMode ? (
                <div className="rounded-2xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-700">
                    引导式手动任务首版仅支持实盘模式，请切换到实盘后继续。
                </div>
            ) : null}

            <div className="grid grid-cols-1 lg:grid-cols-5 gap-2 mb-3 px-0.5">
                {STEP_TITLES.map((title, index) => (
                    <button
                        key={title}
                        type="button"
                        onClick={() => {
                            if (index <= currentStep) {
                                // 回退到 Step 4 之前（index < 3）时清除预案，避免用户在未重新计算的情况下直接进入 Step 4 看到旧预案
                                if (index < 3) setPreview(null);
                                setCurrentStep(index);
                            }
                        }}
                        className={`rounded-xl border px-3 py-1.5 text-left transition-all ${statusTone(index, currentStep)}`}
                    >
                        <div className="text-[9px] font-bold uppercase tracking-wider opacity-60">Step {index + 1}</div>
                        <div className="mt-0.5 text-xs font-bold truncate">{title}</div>
                    </button>
                ))}
            </div>

            <div className="rounded-2xl border border-gray-100 bg-white p-4 shadow-sm">
                {currentStep === 0 ? (
                    <div className="animate-in fade-in slide-in-from-bottom-2 duration-500">
                        <div className="flex flex-col lg:flex-row h-[520px] bg-white border border-gray-100 rounded-2xl overflow-hidden shadow-sm">
                            {/* 左侧：模型列表 */}
                            <div className="w-full lg:w-[320px] flex flex-col border-r border-gray-100 bg-gray-50/20">
                                <div className="p-4 border-b border-gray-100">
                                    <h2 className="text-sm font-bold text-gray-900 flex items-center gap-2 mb-3">
                                        <Layers size={14} className="text-blue-600" />
                                        部署推理模型
                                    </h2>
                                    <div className="relative">
                                        <div className="absolute inset-y-0 left-3 flex items-center pointer-events-none">
                                            <Search className="text-gray-400" size={13} />
                                        </div>
                                        <input
                                            type="text"
                                            placeholder="快速搜索..."
                                            value={modelSearch}
                                            onChange={(e) => setModelSearch(e.target.value)}
                                            className="w-full pl-9 pr-3 py-1.5 !rounded-2xl border border-gray-100 bg-white focus:ring-2 focus:ring-blue-500/10 focus:border-blue-500 outline-none transition-all text-xs"
                                        />
                                    </div>
                                </div>

                                <div className="flex-1 overflow-y-auto custom-scrollbar p-2 space-y-1">
                                    {filteredModelOptions.map((model: any) => {
                                        const isActive = model.model_id === effectiveModelId;
                                        const name = modelDisplayName(model);
                                        
                                        return (
                                            <button
                                                key={model.model_id}
                                                type="button"
                                                onClick={() => resetDownstreamFromModel(model.model_id)}
                                                className={`group relative w-full flex flex-col p-3 rounded-xl border-2 text-left transition-all duration-300 ${isActive
                                                        ? 'bg-gradient-to-br from-blue-50 to-white border-blue-500 shadow-xl shadow-blue-100/50 scale-[1.02] z-10'
                                                        : 'bg-white border-gray-100 hover:border-blue-100 text-gray-700'
                                                    }`}
                                            >
                                                <div className="flex items-center justify-between mb-1.5 w-full overflow-hidden">
                                                    <div className="flex items-center gap-2 truncate">
                                                        <span className={`text-[11px] font-black truncate ${isActive ? 'text-blue-950' : 'text-slate-800'}`}>
                                                            {name}
                                                        </span>
                                                        {isActive && <CheckCircle2 size={12} className="text-blue-500 shrink-0" />}
                                                    </div>
                                                    <span className="shrink-0 flex items-center gap-1">
                                                        <span className={`px-1.5 rounded text-[8px] font-black uppercase tracking-tighter ${isActive ? 'bg-blue-500 text-white' : 'bg-blue-50 text-blue-500'}`}>
                                                            {extractModelType(model)}
                                                        </span>
                                                        <UserIcon size={10} className={isActive ? 'text-blue-500' : 'text-blue-300'} />
                                                    </span>
                                                </div>
                                                <div className="flex items-center justify-between w-full">
                                                    <div className={`font-mono text-[9px] truncate tracking-tight ${isActive ? 'text-blue-800/70' : 'text-slate-400'}`}>
                                                        ID: {model.model_id.slice(-8).toUpperCase()}
                                                    </div>
                                                    <div className={`text-[9px] font-bold ${isActive ? 'text-blue-700' : 'text-blue-500/60'}`}>
                                                        T+{Number(getMeta(model).target_horizon_days ?? getMeta(model).horizon_days ?? 5)}
                                                    </div>
                                                </div>
                                                <div className="mt-2.5 flex items-center gap-1.5">
                                                    <Tag bordered={false} className={`m-0 text-[8px] px-2 py-0 rounded-md font-bold ${isActive ? 'bg-blue-50 text-blue-600' : 'bg-gray-100 text-gray-500'}`}>
                                                        私有
                                                    </Tag>
                                                    <span className={`text-[8px] font-bold uppercase tracking-wider ${isActive ? 'text-blue-600/40' : 'text-gray-400'}`}>
                                                        {extractModelType(model as any)}
                                                    </span>
                                                </div>
                                            </button>
                                        );
                                    })}
                                    {filteredModelOptions.length === 0 && (
                                        <div className="py-20 text-center text-gray-400">
                                            <Empty description={<span className="text-[10px]">未找到匹配模型</span>} image={Empty.PRESENTED_IMAGE_SIMPLE} />
                                        </div>
                                    )}
                                </div>
                            </div>

                            {/* 右侧：监控面板与核心指标 */}
                            <div className="flex-1 flex flex-col bg-white overflow-hidden">
                                {selectedModelObject ? (
                                    <div className="flex-1 flex flex-col p-6 overflow-y-auto custom-scrollbar">
                                        <div className="flex items-start justify-between mb-6">
                                            <div>
                                                <div className="flex items-center gap-2 mb-1">
                                                    <h2 className="text-xl font-bold text-gray-900">
                                                        {modelDisplayName(selectedModelObject as any)}
                                                    </h2>
                                                    <Tag color="blue" bordered={false} className="m-0 text-[10px] font-black rounded-md px-2 py-0.5 uppercase tracking-wider">
                                                        {extractModelType(selectedModelObject as any)}
                                                    </Tag>
                                                    <Tag color="default" bordered={false} className="m-0 text-[10px] font-bold rounded-md px-2 py-0.5 uppercase tracking-wider bg-gray-100 text-gray-500">
                                                        {String(getMeta(selectedModelObject as any).data_source || '全局数据')}
                                                    </Tag>
                                                    <Tag color="purple" bordered={false} className="m-0 text-[10px] font-bold rounded-md px-2 py-0.5 uppercase tracking-wider bg-purple-50 text-purple-600">
                                                        {String(getMeta(selectedModelObject as any).freq || 'Daily')}
                                                    </Tag>
                                                </div>
                                                <div className="text-xs text-gray-400 font-mono flex items-center gap-2">
                                                    <span className="bg-gray-50 px-1.5 py-0.5 rounded text-gray-500 border border-gray-100">{selectedModelObject.model_id}</span>
                                                    <span>创建于 {dayjs(selectedModelObject.created_at).format('YYYY-MM-DD')}</span>
                                                    <span className="text-gray-200">|</span>
                                                    <span className="text-[10px] font-bold text-gray-400 uppercase tracking-widest">{String(getMeta(selectedModelObject as any).version || 'v1.0.0')}</span>
                                                </div>
                                            </div>
                                            <div className="flex flex-col items-end gap-1.5">
                                                <div className="text-[10px] font-bold text-gray-400 uppercase tracking-widest">当前状态</div>
                                                <div className="flex items-center gap-2 px-3 py-1 bg-emerald-50 border border-emerald-100 rounded-full">
                                                    <div className="w-1.5 h-1.5 bg-emerald-500 rounded-full animate-pulse" />
                                                    <span className="text-[10px] font-bold text-emerald-600 uppercase">Deployed / Ready</span>
                                                </div>
                                            </div>
                                        </div>

                                        {/* 预测目标横幅 */}
                                        {(() => {
                                            const meta = getMeta(selectedModelObject as any);
                                            const formula = meta.label || meta.target_label || meta.formula;
                                            if (!formula) return null;
                                            return (
                                                <div className="mb-6 p-3 rounded-xl bg-slate-900 border border-slate-800 shadow-inner group transition-all hover:bg-slate-800">
                                                    <div className="flex items-center gap-2 text-[10px] font-bold text-slate-500 uppercase tracking-widest mb-1.5">
                                                        <Target size={12} className="text-blue-400" />
                                                        预测目标 (Target Factor Formula)
                                                    </div>
                                                    <code className="text-[11px] font-mono font-bold text-blue-300 break-all leading-relaxed">
                                                        {String(formula)}
                                                    </code>
                                                </div>
                                            );
                                        })()}

                                        <div className="grid grid-cols-3 gap-3 mb-6">
                                            {(() => {
                                                const metricsData = getMetrics(selectedModelObject as any);
                                                
                                                const icVal = resolveMetricNumber(metricsData, ['ic', 'test_ic', 'val_ic', 'IC', 'mean_ic', 'train_ic']);
                                                const icirVal = resolveMetricNumber(metricsData, ['icir', 'test_rank_icir', 'val_rank_icir', 'test_icir', 'val_icir', 'ICIR', 'IC_IR', 'rank_icir', 'train_rank_icir']);
                                                const rankIcVal = resolveMetricNumber(metricsData, ['rank_ic', 'test_rank_ic', 'val_rank_ic', 'Rank_IC', 'rankIC', 'train_rank_ic']);
                                                
                                                return (
                                                    <>
                                                        <div className="rounded-2xl bg-blue-50/50 border border-blue-100/50 p-4">
                                                            <div className="text-[10px] font-bold text-blue-500 uppercase tracking-widest mb-1">平均 IC</div>
                                                            <div className="text-xl font-black text-slate-800 font-mono tracking-tighter">
                                                                {icVal?.toFixed(4) || '0.0000'}
                                                            </div>
                                                        </div>

                                                        <div className="rounded-2xl bg-indigo-50/50 border border-indigo-100/50 p-4">
                                                            <div className="text-[10px] font-bold text-indigo-500 uppercase tracking-widest mb-1">Rank IC</div>
                                                            <div className="text-xl font-black text-slate-800 font-mono tracking-tighter">
                                                                {rankIcVal?.toFixed(4) || icVal?.toFixed(4) || '0.0000'}
                                                            </div>
                                                        </div>

                                                        <div className="rounded-2xl bg-emerald-50/50 border border-emerald-100/50 p-4">
                                                            <div className="text-[10px] font-bold text-emerald-500 uppercase tracking-widest mb-1">ICIR (稳定度)</div>
                                                            <div className="text-xl font-black text-slate-800 font-mono tracking-tighter">
                                                                {icirVal?.toFixed(3) || '0.000'}
                                                            </div>
                                                        </div>
                                                    </>
                                                );
                                            })()}
                                        </div>

                                        <div className="space-y-4">
                                            {/* 模型属性清单 */}
                                            <div className="grid grid-cols-2 gap-4">
                                                {/* 特征因子预览 */}
                                                {(() => {
                                                    const meta = getMeta(selectedModelObject as any);
                                                    const features = (meta.features || []) as string[];
                                                    if (features.length === 0) return null;
                                                    return (
                                                        <div className="p-4 rounded-2xl bg-slate-50 border border-slate-200/60 overflow-hidden flex flex-col">
                                                            <div className="text-[10px] font-bold text-slate-400 uppercase tracking-widest mb-3 flex items-center gap-2">
                                                                <Database size={12} className="text-blue-500" />
                                                                特征因子 (Top 5)
                                                            </div>
                                                            <div className="flex flex-wrap gap-1.5">
                                                                {features.slice(0, 5).map(f => (
                                                                    <span key={f} className="px-2 py-0.5 rounded-md bg-white border border-slate-100 text-[10px] font-mono text-slate-600 font-bold truncate max-w-full">
                                                                        {f}
                                                                    </span>
                                                                ))}
                                                                {features.length > 5 && (
                                                                    <span className="text-[10px] text-slate-300 font-bold">+{features.length - 5}</span>
                                                                )}
                                                            </div>
                                                        </div>
                                                    );
                                                })()}

                                                {/* 时间窗口可视化 */}
                                                {(() => {
                                                    const periods = extractTimePeriods(getMeta(selectedModelObject as any));
                                                    if (!periods) return null;
                                                    return (
                                                        <div className="p-4 rounded-2xl bg-indigo-50/20 border border-indigo-100/50">
                                                            <div className="text-[10px] font-bold text-indigo-400 uppercase tracking-widest mb-3 flex items-center gap-2">
                                                                <Calendar size={12} />
                                                                数据周期
                                                            </div>
                                                            <div className="space-y-2">
                                                                <div className="flex items-center justify-between">
                                                                    <span className="text-[9px] font-bold text-slate-400 uppercase">训练</span>
                                                                    <span className="text-[10px] font-mono text-blue-700 font-black">{periods.train[0].slice(2)} → {periods.train[1].slice(2)}</span>
                                                                </div>
                                                                {periods.test && (
                                                                    <div className="flex items-center justify-between">
                                                                        <span className="text-[9px] font-bold text-slate-400 uppercase">测试</span>
                                                                        <span className="text-[10px] font-mono text-emerald-700 font-black">{periods.test[0].slice(2)} → {periods.test[1].slice(2)}</span>
                                                                    </div>
                                                                )}
                                                            </div>
                                                        </div>
                                                    );
                                                })()}
                                            </div>

                                            <div className="p-4 rounded-2xl bg-gray-50/50 border border-gray-100">
                                                <div className="text-[10px] font-bold text-gray-400 uppercase tracking-widest mb-2 flex items-center gap-2">
                                                    <Search size={12} />
                                                    模型描述与适用场景
                                                </div>
                                                <div className="text-[11px] text-gray-600 leading-relaxed italic">
                                                    {String(getMeta(selectedModelObject as any).description || '暂无详细描述。该模型专为 A 股市场 T+N 调仓场景设计，采用了 Qlib 工业级特征工程，在回测期内表现稳健。')}
                                                </div>
                                            </div>

                                            <div className="grid grid-cols-2 gap-3">
                                                <div className="flex flex-col gap-1.5">
                                                    <span className="text-[10px] font-bold text-gray-400 uppercase tracking-widest">特征维度</span>
                                                    <div className="text-xs font-bold text-gray-800 flex items-center gap-2">
                                                        <Cpu size={14} className="text-blue-500" />
                                                        {(() => {
                                                            const meta = getMeta(selectedModelObject as any);
                                                            return Number(meta.feature_count ?? meta.features?.length ?? 0);
                                                        })()} 维
                                                    </div>
                                                </div>
                                                <div className="flex flex-col gap-1.5">
                                                    <span className="text-[10px] font-bold text-gray-400 uppercase tracking-widest">预测周期</span>
                                                    <div className="text-xs font-bold text-gray-800 flex items-center gap-2">
                                                        <Clock size={14} className="text-amber-500" />
                                                        T+{(() => {
                                                            const meta = getMeta(selectedModelObject as any);
                                                            return Number(meta.target_horizon_days ?? meta.horizon_days ?? 5);
                                                        })()} 天
                                                    </div>
                                                </div>
                                            </div>
                                        </div>
                                        <div className="mt-auto pt-6 border-t border-gray-50 flex items-center justify-between">
                                            <div className="flex items-center gap-2">
                                                <Sparkles className="text-amber-400" size={16} />
                                                <p className="text-[11px] text-gray-400 font-medium">只有处于 READY 状态的模型可产生有效信号</p>
                                            </div>
                                        </div>
                                    </div>
                                ) : (
                                    <div className="flex-1 flex flex-col items-center justify-center text-center p-12 space-y-4">
                                        <div className="w-20 h-20 rounded-3xl bg-gray-50 flex items-center justify-center text-gray-200">
                                            <Brain size={40} />
                                        </div>
                                        <div>
                                            <h3 className="font-bold text-gray-900">请选择左侧模型</h3>
                                            <p className="text-xs text-gray-400 mt-1 max-w-[240px]">
                                                选择一个推理模型以查看其详细性能指标与运行轨迹。
                                            </p>
                                        </div>
                                    </div>
                                )}
                            </div>
                        </div>
                    </div>
                ) : null}

                {currentStep === 1 ? (
                    <div className="animate-in fade-in slide-in-from-bottom-2 duration-500">
                        <div className="flex flex-col lg:flex-row gap-6">
                            <div className="flex-1 space-y-5">
                                <div className="flex items-center justify-between mb-2">
                                    <div>
                                        <h2 className="text-lg font-bold text-gray-900 flex items-center gap-2">
                                            第二步：加载模型推理数据
                                            <BarChart3 className="text-emerald-500" size={18} />
                                        </h2>
                                        <p className="text-[13px] text-gray-500 mt-1">
                                            展示当前模型下已完成的推理批次。每个批次包含了对全市场的股票排序与评分。
                                        </p>
                                    </div>
                                </div>

                                <div className="flex flex-col md:flex-row gap-2">
                                    <div className="flex-[3] relative">
                                        <div className="absolute inset-y-0 left-3 flex items-center pointer-events-none">
                                            <Search className="text-gray-400" size={13} />
                                        </div>
                                        <Input 
                                            value={runIdQuery} 
                                            onChange={(e) => setRunIdQuery(e.target.value)} 
                                            placeholder="搜索 Run ID" 
                                            className="pl-9 py-1.5 !rounded-2xl border-gray-100 bg-gray-50/50 text-xs"
                                            allowClear 
                                        />
                                    </div>
                                    <div className="flex-[2] relative">
                                        <DatePicker
                                            value={dateQuery ? dayjs(dateQuery) : null}
                                            onChange={(value) => setDateQuery(value ? value.format('YYYY-MM-DD') : '')}
                                            className="w-full py-1.5 !rounded-2xl border-gray-100 bg-gray-50/50 text-xs"
                                            placeholder="选择交易日"
                                        />
                                    </div>
                                </div>

                                <div className="rounded-2xl border border-gray-100 bg-gray-50/10 p-4">
                                    <div className="flex items-center justify-between mb-4 px-1">
                                        <div className="font-bold text-gray-900 text-xs flex items-center gap-2">
                                            <History size={14} className="text-blue-500" />
                                            推理批次历史
                                        </div>
                                        <div className="text-[10px] font-bold text-gray-400 bg-gray-100/50 px-2 py-0.5 rounded-md">Total: {total}</div>
                                    </div>
                                    
                                    <div className="max-h-[360px] overflow-y-auto grid grid-cols-2 gap-3 p-1.5 custom-scrollbar">
                                        {loadingRuns ? (
                                            <div className="col-span-2 py-20 text-center"><Spin size="small" /></div>
                                        ) : runs.length > 0 ? runs.map((run) => {
                                            const active = run.run_id === selectedRunId;
                                            return (
                                                <button
                                                    key={run.run_id}
                                                    type="button"
                                                    onClick={() => {
                                                        setSelectedRunId(run.run_id);
                                                        resetPreviewAndTask();
                                                    }}
                                                    className={`group relative flex flex-col p-3 rounded-xl border transition-all duration-300 ${
                                                        active 
                                                            ? 'border-blue-500 bg-blue-50/80 ring-2 ring-blue-500/10 shadow-sm scale-[1.02] z-10' 
                                                            : 'bg-white border-gray-100 hover:border-blue-200'
                                                    }`}
                                                >
                                                    <div className="flex items-center justify-between gap-2 mb-2 w-full">
                                                        <div className={`font-mono text-[10px] font-bold truncate ${active ? 'text-blue-900' : 'text-slate-600'}`}>
                                                            {run.run_id.slice(-12).toUpperCase()}
                                                        </div>
                                                        <Tag color={active ? 'blue' : 'default'} bordered={false} className="m-0 text-[8px] px-1 py-0 rounded-md font-bold shrink-0">
                                                            {run.status}
                                                        </Tag>
                                                    </div>
                                                    <div className="flex items-center justify-between w-full">
                                                        <div className={`flex flex-col ${active ? 'text-blue-600' : 'text-gray-400'}`}>
                                                            <span className="text-[8px] uppercase font-bold opacity-60">Trade Date</span>
                                                            <span className="font-mono text-[10px] font-bold">{run.prediction_trade_date || run.target_date || '-'}</span>
                                                        </div>
                                                        <div className={`flex flex-col items-end ${active ? 'text-blue-600' : 'text-gray-400'}`}>
                                                            <span className="text-[8px] uppercase font-bold opacity-60">Signals</span>
                                                            <span className="font-mono text-[10px] font-bold">{run.signals_count ?? 0}</span>
                                                        </div>
                                                    </div>
                                                </button>
                                            );
                                        }) : (
                                            <div className="col-span-2 py-20">
                                                <Empty description="暂无推理数据" image={Empty.PRESENTED_IMAGE_SIMPLE} />
                                            </div>
                                        )}
                                    </div>
                                    
                                    <div className="mt-4 flex items-center justify-between px-1">
                                        <button disabled={page <= 1 || loadingRuns} onClick={() => void loadRuns(page - 1)} className="p-1.5 rounded-lg border border-gray-100 text-gray-500 hover:bg-white disabled:opacity-30 transition-colors">
                                            <ArrowLeft size={14} />
                                        </button>
                                        <div className="flex items-center gap-1">
                                            <span className="text-[10px] font-bold text-gray-900">PAGE {page}</span>
                                            <span className="text-[10px] font-bold text-gray-300">/</span>
                                            <span className="text-[10px] font-bold text-gray-300">{Math.ceil(total / 10) || 1}</span>
                                        </div>
                                        <button disabled={loadingRuns || page * 10 >= total} onClick={() => void loadRuns(page + 1)} className="p-1.5 rounded-lg border border-gray-100 text-gray-500 hover:bg-white disabled:opacity-30 transition-colors">
                                            <ArrowRight size={14} />
                                        </button>
                                    </div>
                                </div>
                            </div>

                            <div className="w-full lg:w-[350px] space-y-4">
                                <div className="p-5 rounded-2xl bg-gray-50/50 border border-gray-100 h-full flex flex-col">
                                    <div className="flex items-center justify-between mb-4">
                                        <h3 className="font-bold text-gray-900 flex items-center gap-2 text-sm">
                                            <Filter size={16} className="text-emerald-500" />
                                            选定批次信号
                                        </h3>
                                    </div>
                                    
                                    {loadingRunDetail ? (
                                        <div className="flex-1 flex items-center justify-center"><Spin size="small" /></div>
                                    ) : selectedRunDetail ? (
                                        <div className="flex-1 flex flex-col space-y-3 overflow-hidden">
                                            <div className="p-3 rounded-xl bg-white border border-gray-100 space-y-2 shadow-sm">
                                                <div className="flex items-center justify-between text-[10px]">
                                                    <span className="text-gray-400 font-bold uppercase shrink-0">Run ID</span>
                                                    <span className="font-mono text-gray-900 font-bold truncate text-right">{selectedRunDetail.run_id}</span>
                                                </div>
                                                <div className="flex items-center justify-between text-[10px]">
                                                    <span className="text-gray-400 font-bold uppercase">Target Date</span>
                                                    <span className="font-mono text-gray-900 font-bold">{selectedRunDetail.summary?.prediction_trade_date || '-'}</span>
                                                </div>
                                            </div>
                                            
                                            <div className="flex-1 overflow-y-auto space-y-1.5 pr-1 custom-scrollbar">
                                                {selectedRunDetail.rankings.slice(0, 10).map((item) => (
                                                    <div key={`${item.code}-${item.rank}`} className="group flex items-center justify-between p-2.5 rounded-xl bg-white border border-gray-50 hover:border-blue-100 transition-colors shadow-sm">
                                                        <div className="flex items-center gap-3 flex-1 overflow-hidden">
                                                            <div className="flex items-center justify-center min-w-[28px] h-7 rounded-lg bg-gray-50 text-[10px] font-bold text-gray-400 shrink-0">
                                                                #{item.rank}
                                                            </div>
                                                            <div className="flex items-center justify-between flex-1 pr-1">
                                                                    <div className="font-mono text-[11px] font-bold text-gray-900">{normalizeStockCode(item.code)}</div>
                                                                    <div className="text-[9px] text-gray-400 uppercase tracking-tighter">Score: {item.score.toFixed(4)}</div>
                                                            </div>
                                                        </div>
                                                    </div>
                                                ))}
                                            </div>
                                        </div>
                                    ) : (
                                        <div className="flex-1 flex flex-col items-center justify-center text-center space-y-3 opacity-30">
                                            <div className="w-12 h-12 rounded-2xl bg-gray-100 flex items-center justify-center text-gray-400">
                                                <Zap size={24} />
                                            </div>
                                            <p className="text-[10px] font-bold uppercase tracking-widest text-gray-400">Select batch to view</p>
                                        </div>
                                    )}

                                    <div className="mt-4">
                                        <button type="button" onClick={() => setCurrentStep(0)} className="w-full py-2 text-[10px] font-bold text-gray-400 hover:text-gray-900 transition-colors uppercase tracking-widest text-center">
                                            Back to Model
                                        </button>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>
                ) : null}


                {currentStep === 2 ? (
                    <div className="animate-in fade-in slide-in-from-bottom-2 duration-500">
                        <div className="flex flex-col lg:flex-row gap-6">
                            <div className="flex-1 space-y-5">
                                <div className="flex items-center justify-between mb-2">
                                    <div>
                                        <h2 className="text-lg font-bold text-gray-900 flex items-center gap-2">
                                            第三步：绑定交易策略
                                            <Zap className="text-blue-500" size={18} />
                                        </h2>
                                        <p className="text-[11px] text-gray-500 mt-0.5">
                                            选定经过验证的策略，系统将基于推理信号生成调仓方案。
                                        </p>
                                    </div>
                                </div>

                                <div className="relative">
                                    <div className="absolute inset-y-0 left-3.5 flex items-center pointer-events-none">
                                        <Search className="text-gray-400" size={16} />
                                    </div>
                                    <input
                                        type="text"
                                        placeholder="搜索策略名称..."
                                        value={strategySearch}
                                        onChange={(e) => setStrategySearch(e.target.value)}
                                        className="w-full pl-10 pr-4 py-2.5 !rounded-2xl border border-gray-100 bg-gray-50/50 focus:bg-white focus:ring-4 focus:ring-blue-500/5 focus:border-blue-500 outline-none transition-all font-medium text-sm text-gray-900"
                                    />
                                </div>

                                <div className="grid grid-cols-2 gap-x-4 gap-y-3 max-h-[460px] overflow-y-auto pr-2 custom-scrollbar p-1">
                                    {filteredStrategyOptions.map((strategy) => {
                                        const isActive = strategy.id === selectedStrategyId;
                                        return (
                                            <button
                                                key={strategy.id}
                                                type="button"
                                                onClick={() => {
                                                    setSelectedStrategyId(strategy.id);
                                                    resetPreviewAndTask();
                                                }}
                                                className={`group relative flex items-center p-3 rounded-2xl border transition-all duration-300 ${
                                                    isActive 
                                                        ? 'bg-blue-50/80 border-blue-500 ring-2 ring-blue-500/5 shadow-sm scale-[1.02] z-10' 
                                                        : 'bg-white border-gray-100/80 hover:border-blue-200'
                                                }`}
                                            >
                                                {/* ID Badge */}
                                                <div className={`flex items-center justify-center min-w-[32px] h-8 rounded-xl font-mono text-[10px] font-bold shrink-0 transition-colors ${
                                                    isActive ? 'bg-blue-600 text-white shadow-md' : 'bg-gray-100 text-gray-400'
                                                }`}>
                                                    #{strategy.id}
                                                </div>
                                                
                                                {/* Strategy Info */}
                                                <div className="flex-1 min-w-0 mx-3 text-left">
                                                    <div className={`font-bold text-[12px] truncate ${isActive ? 'text-blue-900' : 'text-slate-700'}`}>
                                                        {strategy.name}
                                                    </div>
                                                </div>

                                                {/* Type Tag & Status */}
                                                <div className="flex items-center gap-2 shrink-0">
                                                    <Tag color={strategy.is_system ? 'gold' : 'blue'} bordered={false} className="m-0 text-[8px] px-1.5 py-0 rounded-md font-black uppercase tracking-wider opacity-60">
                                                        {strategy.is_system ? 'SYS' : 'USR'}
                                                    </Tag>
                                                    {isActive && (
                                                        <div className="text-blue-600">
                                                            <CheckCircle2 size={16} strokeWidth={3} />
                                                        </div>
                                                    )}
                                                </div>
                                            </button>
                                        );
                                    })}
                                </div>

                                <div className="space-y-1.5">
                                    <div className="text-[10px] font-bold text-gray-400 uppercase tracking-widest ml-1">执行备注</div>
                                    <Input.TextArea
                                        value={note}
                                        onChange={(e) => {
                                            setNote(e.target.value);
                                            resetPreviewAndTask();
                                        }}
                                        placeholder="填写调仓说明（可选）..."
                                        className="!rounded-2xl border-gray-100 bg-gray-50/50 p-3 focus:bg-white transition-all text-xs"
                                        autoSize={{ minRows: 2, maxRows: 2 }}
                                    />
                                </div>
                            </div>

                            <div className="w-full lg:w-[320px] space-y-3">
                                <div className="p-5 rounded-2xl bg-gray-50/50 border border-gray-100 h-full flex flex-col">
                                    <h3 className="font-bold text-gray-900 flex items-center gap-2 mb-4 text-sm">
                                        <Link size={16} className="text-blue-500" />
                                        策略上下文
                                    </h3>
                                    
                                    <div className="flex-1 space-y-4 font-bold">
                                        <div className="space-y-2">
                                            <div className="p-3.5 rounded-xl bg-white border border-gray-100 shadow-sm">
                                                <div className="text-[9px] uppercase font-bold text-gray-400 mb-0.5">模型路径</div>
                                                <div className="text-[11px] text-gray-900 truncate">{effectiveModelId}</div>
                                            </div>
                                            <div className="p-3.5 rounded-xl bg-white border border-gray-100 shadow-sm">
                                                <div className="text-[9px] uppercase font-bold text-gray-400 mb-0.5">推理批次</div>
                                                <div className="text-[11px] text-gray-900 truncate">{selectedRunId || '未选择'}</div>
                                            </div>
                                        </div>

                                        <div className="p-4 rounded-xl bg-white border border-gray-100 shadow-sm">
                                            <div className="font-bold text-[10px] text-gray-900 mb-2 flex items-center gap-1.5">
                                                <Settings2 size={12} />
                                                参数集
                                            </div>
                                            {selectedStrategy?.parameters && Object.keys(selectedStrategy.parameters).length > 0 ? (
                                                <div className="space-y-2.5 max-h-[120px] overflow-y-auto pr-1 custom-scrollbar">
                                                    {Object.entries(selectedStrategy.parameters).slice(0, 4).map(([key, value]) => (
                                                        <div key={key} className="flex flex-col">
                                                            <div className="text-[9px] text-gray-400 font-bold uppercase tracking-tighter">{key}</div>
                                                            <div className="text-[11px] font-mono text-blue-700 break-all">{String(value)}</div>
                                                        </div>
                                                    ))}
                                                </div>
                                            ) : (
                                                <div className="text-[9px] text-gray-400 italic">默认配置</div>
                                            )}
                                        </div>
                                    </div>

                                    <div className="mt-4">
                                        <button type="button" onClick={() => setCurrentStep(1)} className="w-full py-2 text-[10px] font-bold text-gray-400 hover:text-gray-900 transition-colors uppercase tracking-widest text-center">
                                            Back to Data
                                        </button>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>
                ) : null}

                {currentStep === 3 ? (
                    <div className="animate-in fade-in slide-in-from-bottom-2 duration-500">
                        <div className="flex flex-col lg:flex-row gap-6">
                            <div className="flex-1 space-y-5">
                                <div>
                                    <h2 className="text-lg font-bold text-gray-900 flex items-center gap-2">
                                        第四步：向导生成执行预案
                                        <Eye className="text-blue-500" size={18} />
                                    </h2>
                                    <p className="text-[13px] text-gray-500 mt-1">
                                        系统已根据行情和策略逻辑生成指令概要。此步骤不会发起真实报单，仅供核对执行细节。
                                    </p>
                                </div>

                                {!preview && (
                                    <div className="p-10 rounded-2xl border-2 border-dashed border-gray-100 bg-gray-50/50 flex flex-col items-center justify-center text-center">
                                        <div className="w-16 h-16 rounded-2xl bg-white shadow-md shadow-gray-200/50 flex items-center justify-center text-blue-500 mb-5">
                                            <Wand2 size={32} />
                                        </div>
                                        <h3 className="text-base font-bold text-gray-900 mb-1.5">生成预案以继续</h3>
                                        <p className="text-xs text-gray-400 max-w-xs mb-6 font-medium">点击右侧按钮计算本次任务的具体委托细节。</p>
                                        <button
                                            type="button"
                                            onClick={() => void generatePreview()}
                                            disabled={previewLoading}
                                            className="px-6 py-3 rounded-xl bg-blue-600 text-white text-xs font-bold hover:bg-blue-700 disabled:opacity-50 shadow-md shadow-blue-100 transition-all flex items-center gap-2"
                                        >
                                            {previewLoading ? <Loader2 size={16} className="animate-spin" /> : <Play size={16} fill="currentColor" />}
                                            立即计算调仓预案
                                        </button>
                                    </div>
                                )}

                                {preview && (
                                    <div className="space-y-6 animate-in fade-in duration-500">
                                        <div className="grid grid-cols-1 xl:grid-cols-2 gap-3">
                                            <div className="p-4 rounded-xl border border-gray-100 bg-gray-50/50">
                                                <div className="text-[9px] font-bold text-gray-400 uppercase tracking-widest mb-3 flex items-center gap-2">
                                                    <CreditCard size={12} className="text-blue-500" />
                                                    资产快照
                                                </div>
                                                <div className="grid grid-cols-2 gap-4">
                                                    <div>
                                                        <div className="text-[9px] text-gray-400 font-bold mb-0.5 uppercase">现金</div>
                                                        <div className="font-mono text-sm font-bold text-gray-900">{renderMoney(preview.account_snapshot.available_cash)}</div>
                                                    </div>
                                                    <div>
                                                        <div className="text-[9px] text-gray-400 font-bold mb-0.5 uppercase">市值</div>
                                                        <div className="font-mono text-sm font-bold text-gray-900">{renderMoney(preview.account_snapshot.market_value)}</div>
                                                    </div>
                                                </div>
                                            </div>
                                            <div className="p-4 rounded-xl border border-gray-100 bg-gray-50/50">
                                                <div className="text-[9px] font-bold text-gray-400 uppercase tracking-widest mb-3 flex items-center gap-2">
                                                    <Binary size={12} className="text-emerald-500" />
                                                    运行节点
                                                </div>
                                                <div className="grid grid-cols-2 gap-4">
                                                    <div className="truncate">
                                                        <div className="text-[9px] text-gray-400 font-bold mb-0.5 uppercase">模型 ID</div>
                                                        <div className="font-bold text-[11px] text-gray-900 truncate">{preview.strategy_context.model_id}</div>
                                                    </div>
                                                    <div>
                                                        <div className="text-[9px] text-gray-400 font-bold mb-0.5 uppercase">日期</div>
                                                        <div className="font-mono font-bold text-[11px] text-gray-900">{preview.strategy_context.prediction_trade_date}</div>
                                                    </div>
                                                </div>
                                            </div>
                                        </div>

                                        <div className="grid grid-cols-1 xl:grid-cols-2 gap-5">
                                            <div className="space-y-3">
                                                <h4 className="text-[11px] font-bold text-gray-900 px-1 flex items-center gap-2 uppercase tracking-wider">
                                                    <div className="w-1 h-3 bg-rose-500 rounded-full" />
                                                    卖出列表 ({preview.sell_orders.length})
                                                </h4>
                                                <div className="space-y-2.5 max-h-[400px] overflow-y-auto pr-1 custom-scrollbar">
                                                    {preview.sell_orders.length > 0 ? preview.sell_orders.map((order) => renderOrderCard(order, 'sell')) : (
                                                        <div className="py-10 border border-dashed border-gray-100 rounded-xl text-center text-[10px] font-bold text-gray-300 uppercase tracking-widest">Empty</div>
                                                    )}
                                                </div>
                                            </div>

                                            <div className="space-y-3">
                                                <h4 className="text-[11px] font-bold text-gray-900 px-1 flex items-center gap-2 uppercase tracking-wider">
                                                    <div className="w-1 h-3 bg-emerald-500 rounded-full" />
                                                    买入列表 ({preview.buy_orders.length})
                                                </h4>
                                                <div className="space-y-2.5 max-h-[400px] overflow-y-auto pr-1 custom-scrollbar">
                                                    {preview.buy_orders.length > 0 ? preview.buy_orders.map((order) => renderOrderCard(order, 'buy')) : (
                                                        <div className="py-10 border border-dashed border-gray-100 rounded-xl text-center text-[10px] font-bold text-gray-300 uppercase tracking-widest">Empty</div>
                                                    )}
                                                </div>
                                            </div>
                                        </div>

                                        <div className="p-4 rounded-2xl bg-rose-50/30 border border-rose-100/60 shadow-sm transition-all animate-in fade-in zoom-in-95 duration-700">
                                            <div className="flex items-center gap-2 mb-3 text-[10px] font-bold text-rose-600 uppercase tracking-widest">
                                                <ShieldAlert size={14} />
                                                风控/过滤项 ({preview.skipped_items.length})
                                            </div>
                                            <div className="grid grid-cols-1 md:grid-cols-2 gap-2.5 max-h-[180px] overflow-y-auto pr-1 custom-scrollbar pb-1">
                                                {preview.skipped_items.length > 0 ? preview.skipped_items.map((item, idx) => (
                                                    <div key={idx} className="p-2.5 rounded-xl bg-white border border-rose-100/40 flex items-center gap-3 shadow-sm shadow-rose-50/50">
                                                        <div className="w-8 h-8 rounded-lg bg-rose-50 flex items-center justify-center font-mono text-[10px] font-black text-rose-400">
                                                            {item.symbol.slice(0, 2)}
                                                        </div>
                                                        <div className="flex-1 min-w-0">
                                                            <div className="text-[11px] font-bold text-gray-900">{normalizeStockCode(item.symbol)}</div>
                                                            <div className="text-[9px] text-gray-500 line-clamp-1 font-medium italic">{item.reason}</div>
                                                        </div>
                                                        <div className="text-[8px] font-black px-1.5 py-0.5 rounded-md bg-rose-100/50 text-rose-600 uppercase">
                                                            {item.action}
                                                        </div>
                                                    </div>
                                                )) : (
                                                    <div className="col-span-2 py-6 text-center text-[10px] text-gray-400 font-bold uppercase tracking-widest bg-white/50 rounded-xl border border-dashed border-gray-100">
                                                        No Risk Detected
                                                    </div>
                                                )}
                                            </div>
                                        </div>
                                    </div>
                                )}
                            </div>

                            <div className="w-full lg:w-[320px] shrink-0">
                                <div className="sticky top-4 space-y-3">
                                    <div className="p-5 rounded-2xl bg-white border border-gray-100 shadow-sm flex flex-col h-full font-bold">
                                        <h3 className="font-bold text-gray-900 flex items-center gap-2 mb-5 text-sm">
                                            <Activity size={16} className="text-blue-500" />
                                            执行摘要
                                        </h3>
                                        
                                        <div className="space-y-5 flex-1">
                                            <div className="space-y-1">
                                                <div className="text-[9px] font-bold text-gray-400 uppercase tracking-widest ml-1">预案指纹</div>
                                                <div className="font-mono text-[10px] text-gray-600 bg-gray-50 px-3 py-2 rounded-xl flex items-center justify-between border border-gray-100">
                                                    {preview ? preview.preview_hash.slice(0, 16) : 'PENDING'}...
                                                    <CheckCircle2 size={12} className={preview ? 'text-emerald-500' : 'text-gray-300'} />
                                                </div>
                                            </div>

                                            <div className="p-4 rounded-xl bg-blue-50/50 border border-blue-100 space-y-3.5">
                                                <div className="flex items-center justify-between text-[11px]">
                                                    <span className="text-blue-600/70 uppercase">卖出总额</span>
                                                    <span className="font-mono text-gray-900">{renderMoney(previewSummary?.estimated_sell_proceeds)}</span>
                                                </div>
                                                <div className="flex items-center justify-between text-[11px]">
                                                    <span className="text-blue-600/70 uppercase">买入总额</span>
                                                    <span className="font-mono text-gray-900">{renderMoney(previewSummary?.estimated_buy_amount)}</span>
                                                </div>
                                                <div className="h-px bg-blue-100" />
                                                <div className="flex items-center justify-between text-[11px]">
                                                    <span className="text-blue-600 uppercase tracking-tighter">预估剩余</span>
                                                    <span className="font-mono text-blue-700">{renderMoney(previewSummary?.estimated_remaining_cash)}</span>
                                                </div>
                                            </div>

                                            <div className="grid grid-cols-2 gap-2">
                                                <div className="p-3 rounded-xl bg-gray-50 border border-gray-100 flex flex-col items-center justify-center">
                                                    <span className="text-[9px] font-bold text-gray-400 uppercase mb-0.5">信号</span>
                                                    <span className="text-xs text-gray-900">{previewSummary?.signal_count ?? 0}</span>
                                                </div>
                                                <div className="p-3 rounded-xl bg-gray-50 border border-gray-100 flex flex-col items-center justify-center">
                                                    <span className="text-[9px] font-bold text-gray-400 uppercase mb-0.5">派发</span>
                                                    <span className="text-xs text-emerald-600">{(previewSummary?.buy_order_count || 0) + (previewSummary?.sell_order_count || 0)}</span>
                                                </div>
                                            </div>
                                        </div>

                                        <div className="mt-6 space-y-2">
                                            <button
                                                type="button"
                                                onClick={() => {
                                                    if (!preview) void generatePreview();
                                                }}
                                                disabled={previewLoading || !!preview}
                                                className={`w-full py-3.5 rounded-2xl text-[13px] font-black transition-all active:scale-[0.95] flex items-center justify-center gap-2 ${preview
                                                        ? 'bg-emerald-50 text-emerald-600 border border-emerald-100 cursor-default'
                                                        : 'bg-blue-600 text-white hover:bg-blue-700 shadow-xl shadow-blue-200/50'
                                                    }`}
                                            >
                                                {previewLoading ? (
                                                    <Loader2 size={16} className="animate-spin" />
                                                ) : preview ? (
                                                    <>预案计算完成 <CheckCircle2 size={14} /></>
                                                ) : (
                                                    <>开始计算调仓预案 <Play size={14} fill="currentColor" /></>
                                                )}
                                            </button>
                                            <button 
                                                type="button" 
                                                onClick={() => { setPreview(null); setCurrentStep(2); }} 
                                                className="w-full py-2 text-[10px] font-bold text-gray-400 hover:text-gray-900 transition-colors uppercase tracking-widest text-center"
                                            >
                                                Back to Strategy
                                            </button>
                                        </div>
                                    </div>
                                    
                                    <div className="p-4 rounded-xl bg-amber-50 border border-amber-100 flex gap-3">
                                        <div className="shrink-0 w-8 h-8 rounded-lg bg-amber-200/50 flex items-center justify-center text-amber-700">
                                            <Info size={16} />
                                        </div>
                                        <div className="text-[10px] text-amber-800 leading-relaxed font-bold">
                                            手动任务采用「原子级幂等」设计，系统通过 Hash 校验确保不会产生重复报单。
                                        </div>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>
                ) : null}

                {currentStep === 4 ? (
                    <div className="animate-in fade-in slide-in-from-bottom-2 duration-500 h-full flex flex-col">
                        <div className="flex flex-col lg:flex-row gap-6 flex-1 min-h-0">
                            <div className="flex-1 space-y-6 min-h-0 flex flex-col">
                                <div>
                                    <h2 className="text-lg font-bold text-gray-900 flex items-center gap-2">
                                        第五步：任务推送与日志
                                        <TerminalSquare className="text-slate-900" size={18} />
                                    </h2>
                                    <p className="text-[13px] text-gray-500 mt-1">
                                        任务已提交至执行队列，控制台显示 QMT Agent 的实时链路状态。
                                    </p>
                                </div>

                                <div className="flex-1 rounded-2xl bg-white border border-gray-100 p-5 flex flex-col shadow-sm min-h-[400px]">
                                    <div className="flex items-center justify-between mb-5">
                                        <div className="flex items-center gap-2.5">
                                            <div className="flex gap-1.5">
                                                <div className="w-2.5 h-2.5 rounded-full bg-rose-500/80" />
                                                <div className="w-2.5 h-2.5 rounded-full bg-amber-500/80" />
                                                <div className="w-2.5 h-2.5 rounded-full bg-emerald-500/80" />
                                            </div>
                                            <div className="h-3 w-px bg-gray-200 mx-1" />
                                            <div className="text-[10px] font-mono font-bold text-gray-400 uppercase tracking-widest">Agent Runtime</div>
                                        </div>
                                        {selectedTaskId && (
                                            <div className="px-2 py-0.5 rounded-md bg-gray-50 border border-gray-100 text-[10px] font-mono text-blue-600/70">
                                                ID: {selectedTaskId.slice(0, 12)}
                                            </div>
                                        )}
                                    </div>

                                    <div className="flex-1 overflow-y-auto custom-scrollbar pr-2 font-mono text-[12px] leading-relaxed">
                                        {logs.length > 0 ? logs.map((entry) => (
                                            <div key={entry.id} className="group flex gap-4 py-1 hover:bg-gray-50/80 transition-colors border-l-2 border-transparent hover:border-blue-500/20 pl-1">
                                                <span className="text-gray-400 select-none shrink-0 w-24 text-[10px]">[{entry.ts?.split(' ')[1] || '-'}]</span>
                                                <span className={`shrink-0 uppercase text-[9px] font-black flex items-center h-4 mt-0.5 px-1.5 rounded-md ${
                                                    entry.level === 'error' ? 'bg-rose-50 text-rose-500' : 
                                                    entry.level === 'warning' ? 'bg-amber-50 text-amber-600' : 
                                                    'bg-emerald-50 text-emerald-600'
                                                }`}>
                                                    {entry.level}
                                                </span>
                                                <span className={`font-medium ${entry.level === 'error' ? 'text-rose-700' : entry.level === 'warning' ? 'text-amber-800' : 'text-gray-700'}`}>
                                                    {entry.line}
                                                </span>
                                            </div>
                                        )) : (
                                            <div className="h-full flex flex-col items-center justify-center text-center space-y-3 opacity-20">
                                                <Cpu size={20} className="text-slate-400 animate-pulse" />
                                                <p className="text-[10px] uppercase font-bold text-slate-500">Connecting...</p>
                                            </div>
                                        )}
                                        <div ref={viewportRef} />
                                    </div>
                                    
                                    {snapshot && (
                                        <div className="mt-5 p-4 rounded-2xl bg-gray-50/50 border border-gray-100 flex items-center justify-between">
                                            <div className="flex items-center gap-6">
                                                <div className="flex flex-col">
                                                    <span className="text-[9px] font-bold text-gray-400 uppercase tracking-tight">Stage</span>
                                                    <span className="text-xs font-bold text-gray-900">{stageLabel(snapshot.stage || snapshot.status)}</span>
                                                </div>
                                                <div className="w-px h-6 bg-gray-200" />
                                                <div className="flex items-center gap-5">
                                                    <div className="flex flex-col">
                                                        <span className="text-[9px] font-bold text-gray-400 uppercase tracking-tight">Success</span>
                                                        <span className="text-sm font-bold text-emerald-600">{snapshot.success_count ?? 0}</span>
                                                    </div>
                                                    <div className="flex flex-col">
                                                        <span className="text-[9px] font-bold text-gray-400 uppercase tracking-tight">Failed</span>
                                                        <span className="text-sm font-bold text-rose-600">{snapshot.failed_count ?? 0}</span>
                                                    </div>
                                                </div>
                                            </div>
                                            {snapshot.stage !== 'completed' && snapshot.status !== 'completed' && (
                                                <div className="flex items-center gap-2 px-2 py-1 rounded-full bg-emerald-50 border border-emerald-100">
                                                    <div className="w-1.5 h-1.5 rounded-full bg-emerald-500 animate-pulse" />
                                                    <span className="text-[10px] font-bold text-emerald-600 uppercase tracking-wider">Agent Live</span>
                                                </div>
                                            )}
                                        </div>
                                    )}
                                </div>
                            </div>

                            <div className="w-full lg:w-[320px] shrink-0">
                                <div className="p-5 rounded-2xl bg-white border border-gray-100 shadow-sm space-y-6 h-full flex flex-col font-bold">
                                    <div className="space-y-5 flex-1">
                                        <div className="flex items-center gap-3">
                                            <div className="w-9 h-9 rounded-xl bg-red-50 text-red-600 flex items-center justify-center">
                                                <AlertTriangle size={18} />
                                            </div>
                                            <div>
                                                <div className="text-sm font-bold text-gray-900">执行确认</div>
                                                <div className="text-[9px] text-gray-400 font-bold uppercase tracking-wider">Final Authorization</div>
                                            </div>
                                        </div>

                                        <div className="p-4 rounded-xl bg-gray-50/50 border border-gray-100 space-y-3 shadow-inner">
                                            <div className="flex items-center justify-between text-[11px]">
                                                <span className="text-gray-400">委托笔数</span>
                                                <span className="text-gray-900">{(previewSummary?.buy_order_count || 0) + (previewSummary?.sell_order_count || 0)} 笔</span>
                                            </div>
                                            <div className="flex items-center justify-between text-[11px]">
                                                <span className="text-gray-400">预估买入</span>
                                                <span className="text-gray-900">{renderMoney(previewSummary?.estimated_buy_amount)}</span>
                                            </div>
                                            <div className="flex items-center justify-between text-[11px]">
                                                <span className="text-gray-400">风险拦截</span>
                                                <span className="text-rose-500">{previewSummary?.skipped_count ?? 0}</span>
                                            </div>
                                        </div>
                                    </div>

                                    {!selectedTaskId ? (
                                        <div className="space-y-2">
                                            <button
                                                type="button"
                                                onClick={() => void submitExecution()}
                                                disabled={submitting || !preview}
                                                className="w-full py-3.5 rounded-xl bg-red-600 text-white text-xs font-bold hover:bg-red-700 shadow-md shadow-red-100 transition-all active:scale-[0.98] flex items-center justify-center gap-2"
                                            >
                                                {submitting ? (
                                                    <Loader2 size={16} className="animate-spin" />
                                                ) : (
                                                    <>推送到 QMT <Zap size={14} fill="currentColor" /></>
                                                )}
                                            </button>
                                            <button 
                                                type="button" 
                                                disabled={submitting}
                                                onClick={() => setCurrentStep(3)} 
                                                className="w-full py-2 text-[10px] font-bold text-gray-400 hover:text-gray-900 transition-colors uppercase tracking-widest text-center"
                                            >
                                                Review Preview
                                            </button>
                                        </div>
                                    ) : (
                                        <div className="space-y-3">
                                            <div className="p-4 rounded-xl bg-emerald-50 border border-emerald-100 flex flex-col items-center text-center">
                                                <div className="w-10 h-10 rounded-xl bg-emerald-100 text-emerald-600 flex items-center justify-center mb-3">
                                                    <CheckCircle2 size={24} />
                                                </div>
                                                <div className="text-xs font-bold text-emerald-900 leading-none">正在执行</div>
                                                <div className="text-[9px] text-emerald-600/70 mt-1 uppercase tracking-tighter">Connected to QMT</div>
                                            </div>
                                            {(selectedTask?.stage === 'completed' || selectedTask?.status === 'completed') && (
                                                <button
                                                    onClick={() => message.success('请在左边栏查看订单明细')}
                                                    className="w-full py-3 rounded-xl border border-gray-200 bg-white text-gray-900 text-[11px] font-bold hover:bg-gray-50 transition-all shadow-sm"
                                                >
                                                    查看成交结果
                                                </button>
                                            )}
                                        </div>
                                    )}

                                    <div className="p-4 rounded-xl border border-gray-50 bg-gray-50/20">
                                        <div className="text-[9px] font-bold text-gray-400 uppercase tracking-widest mb-1.5 opacity-60">风险披露</div>
                                        <div className="text-[9px] text-gray-400 leading-relaxed italic">
                                            指令通过 QMT 链路报送。请确保账户已就绪且资金充足。
                                        </div>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>
                ) : null}
            </div>
        </div>
    );
};

export default ManualTaskPage;
