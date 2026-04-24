import React, { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import { Play, Square, CheckCircle, Activity, Cpu, FileText, RefreshCw, AlertCircle, Settings2, Clock3, TerminalSquare } from 'lucide-react';
import { Input, Select, message } from 'antd';
import { strategyManagementService } from '../../../services/strategyManagementService';
import { modelTrainingService, UserModelRecord, LatestInferenceRunInfo } from '../../../services/modelTrainingService';
import { realTradingService, RealTradingStatus, PreflightCheckItem } from '../../../services/realTradingService';
import { websocketService, WebSocketStatus } from '../../../services/websocketService';
import { StrategyFile } from '../../../types/backtest/strategy';

const formatDateTime = (raw?: string | null) => {
    if (!raw) return '-';
    const value = new Date(raw);
    if (Number.isNaN(value.getTime())) return raw;
    return value.toLocaleString();
};

const formatTaskStatus = (value?: string | null) => {
    const status = String(value || '').toLowerCase();
    if (status === 'completed') return '已完成';
    if (status === 'running') return '执行中';
    if (status === 'dispatching') return '派发中';
    if (status === 'validating') return '校验中';
    if (status === 'queued') return '排队中';
    if (status === 'failed') return '已失败';
    if (status === 'cancelled') return '已取消';
    return value || '-';
};

const taskStatusTone = (value?: string | null) => {
    const status = String(value || '').toLowerCase();
    if (status === 'completed') return 'bg-emerald-50 text-emerald-700 border-emerald-200';
    if (status === 'running' || status === 'dispatching' || status === 'validating' || status === 'queued') {
        return 'bg-blue-50 text-blue-700 border-blue-200';
    }
    if (status === 'failed' || status === 'cancelled') return 'bg-rose-50 text-rose-700 border-rose-200';
    return 'bg-slate-50 text-slate-600 border-slate-200';
};

interface StrategyManagementProps {
    tenantId: string;
    userId: string;
    tradingMode?: 'real' | 'simulation';
    status?: RealTradingStatus | null;
    onDeploy: (
        strategyId: string,
        isShadow: boolean,
        strategy?: StrategyFile | null
    ) => Promise<void>;
    onStop: () => Promise<void>;
    isRunning: boolean;
    onOpenManualTask?: () => void;
    activeExecutionConfig?: { max_buy_drop?: number; stop_loss?: number } | null;
    activeLiveTradeConfig?: {
        schedule_type?: 'interval' | 'weekly';
        trade_weekdays?: string[];
        sell_time?: string;
        buy_time?: string;
        rebalance_days?: number;
        order_type?: 'LIMIT' | 'MARKET';
        max_price_deviation?: number;
        max_orders_per_cycle?: number;
    } | null;
}

const StrategyManagement: React.FC<StrategyManagementProps> = ({
    tenantId,
    userId,
    tradingMode,
    status,
    onDeploy,
    onStop,
    isRunning,
    onOpenManualTask,
    activeExecutionConfig,
    activeLiveTradeConfig,
}) => {
    const [strategies, setStrategies] = useState<StrategyFile[]>([]);
    const [selectedStrategyId, setSelectedStrategyId] = useState<string>('');
    const [isShadowMode, setIsShadowMode] = useState(false);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [monitorChecks, setMonitorChecks] = useState<PreflightCheckItem[]>([]);
    const [monitorCheckedAt, setMonitorCheckedAt] = useState<string | null>(null);
    const [websocketStatus, setWebsocketStatus] = useState<WebSocketStatus>(websocketService.getStatus());
    const [defaultModel, setDefaultModel] = useState<UserModelRecord | null>(null);
    const [latestInferenceRun, setLatestInferenceRun] = useState<LatestInferenceRunInfo | null>(null);
    const [latestInferenceRunLoading, setLatestInferenceRunLoading] = useState(false);
    const [hostedLogs, setHostedLogs] = useState<string[]>([]);
    const [hostedLogsVisible, setHostedLogsVisible] = useState<boolean>(false);
    const [latestInferenceRunIsNew, setLatestInferenceRunIsNew] = useState(false);
    const hostedCursorRef = useRef('0-0');
    const hostedLogsRef = useRef<string[]>([]);
    const latestInferenceRunIdRef = useRef<string | null>(null);
    const latestInferenceNewTimerRef = useRef<number | null>(null);

    const strategyOptions = useMemo(
        () => strategies.map((strategy) => ({
            value: strategy.id,
            label: strategy.is_system ? `(内置) ${strategy.name}` : strategy.name,
        })),
        [strategies],
    );

    const effectiveModelId = useMemo(
        () => defaultModel?.model_id || 'model_qlib',
        [defaultModel],
    );

    const effectiveModelDisplayName = useMemo(() => {
        const metadata = (defaultModel?.metadata_json || {}) as Record<string, unknown>;
        const displayName = typeof metadata.display_name === 'string' ? metadata.display_name.trim() : '';
        return displayName || defaultModel?.model_id || 'model_qlib';
    }, [defaultModel]);

    const loadStrategies = useCallback(async () => {
        setLoading(true);
        setError(null);
        try {
            const list = await strategyManagementService.loadStrategies(userId);
            setStrategies(list);
        } catch (e) {
            console.error("Failed to load strategies", e);
            setError("无法加载策略列表，请稍后重试");
        } finally {
            setLoading(false);
        }
    }, [userId]);

    const loadDefaultModel = useCallback(async () => {
        try {
            const model = await modelTrainingService.getDefaultModel();
            setDefaultModel(model || null);
        } catch (e) {
            const statusErr = (e as any)?.response?.status;
            if (statusErr !== 404) {
                console.warn('Failed to load default model', e);
            }
            setDefaultModel(null);
        }
    }, []);

    const loadLatestInferenceRun = useCallback(async () => {
        setLatestInferenceRunLoading(true);
        try {
            const latest = await modelTrainingService.getLatestInferenceRun(effectiveModelId);
            setLatestInferenceRun(latest || null);
        } catch (e) {
            console.warn('Failed to load latest inference run', e);
            setLatestInferenceRun(null);
        } finally {
            setLatestInferenceRunLoading(false);
        }
    }, [effectiveModelId]);

    useEffect(() => {
        loadStrategies();
        loadDefaultModel();
    }, [loadStrategies, loadDefaultModel]);

    useEffect(() => {
        const timer = setInterval(() => {
            void loadDefaultModel();
        }, 15000);
        return () => clearInterval(timer);
    }, [loadDefaultModel]);

    useEffect(() => {
        void loadLatestInferenceRun();
        const timer = setInterval(() => {
            void loadLatestInferenceRun();
        }, 15000);
        return () => clearInterval(timer);
    }, [loadLatestInferenceRun]);

    useEffect(() => {
        const currentRunId = latestInferenceRun?.run_id?.trim() || '';
        if (!currentRunId) {
            setLatestInferenceRunIsNew(false);
            latestInferenceRunIdRef.current = null;
            if (latestInferenceNewTimerRef.current) {
                window.clearTimeout(latestInferenceNewTimerRef.current);
                latestInferenceNewTimerRef.current = null;
            }
            return;
        }

        const previousRunId = latestInferenceRunIdRef.current;
        if (previousRunId && previousRunId !== currentRunId) {
            setLatestInferenceRunIsNew(true);
            if (latestInferenceNewTimerRef.current) {
                window.clearTimeout(latestInferenceNewTimerRef.current);
            }
            latestInferenceNewTimerRef.current = window.setTimeout(() => {
                setLatestInferenceRunIsNew(false);
                latestInferenceNewTimerRef.current = null;
            }, 8000);
        } else if (!previousRunId) {
            setLatestInferenceRunIsNew(false);
        }
        latestInferenceRunIdRef.current = currentRunId;

        return () => {
            if (latestInferenceNewTimerRef.current) {
                window.clearTimeout(latestInferenceNewTimerRef.current);
                latestInferenceNewTimerRef.current = null;
            }
        };
    }, [latestInferenceRun?.run_id]);

    useEffect(() => {
        const currentMode = tradingMode === 'simulation' ? 'SIMULATION' : 'REAL';
        let cancelled = false;

        const loadMonitor = async () => {
            try {
                const result = await realTradingService.preflight(currentMode, userId, tenantId);
                if (cancelled) return;
                setMonitorChecks(Array.isArray(result?.checks) ? result.checks : []);
                setMonitorCheckedAt(result?.checked_at || null);
            } catch (e) {
                if (cancelled) return;
                console.warn('Failed to load strategy monitor snapshot', e);
                setMonitorChecks([]);
                setMonitorCheckedAt(null);
            }
        };

        loadMonitor();
        const timer = setInterval(loadMonitor, 15000);
        return () => {
            cancelled = true;
            clearInterval(timer);
        };
    }, [tenantId, tradingMode, userId]);

    useEffect(() => {
        const syncStatus = (nextStatus: WebSocketStatus) => {
            setWebsocketStatus(nextStatus);
        };
        setWebsocketStatus(websocketService.getStatus());
        websocketService.addStatusHandler(syncStatus);
        return () => {
            websocketService.removeStatusHandler(syncStatus);
        };
    }, []);

    const handleDeploy = () => {
        const strategy = strategies.find((s) => s.id === selectedStrategyId);
        if (selectedStrategyId) {
            onDeploy(selectedStrategyId, isShadowMode, strategy || null);
        }
    };

    const selectedStrategy = strategies.find(s => s.id === selectedStrategyId);
    const isGlobalSim = tradingMode === 'simulation';
    const isDeployDisabled = !selectedStrategyId || !selectedStrategy?.is_verified;
    const runtimeMode = status?.mode;
    const runtimeStatus = status?.status;
    const latestHostedTask = status?.latest_hosted_task || null;
    const latestHostedRequest = (latestHostedTask?.request_json || {}) as Record<string, any>;
    const latestHostedTriggerContext = (latestHostedTask?.trigger_context_json || {}) as Record<string, any>;
    const latestHostedTaskSummary = (latestHostedTask?.result_json || {}) as Record<string, any>;
    const latestHostedTaskPreviewSummary = (latestHostedTaskSummary?.preview_summary || latestHostedRequest?.execution_plan?.summary || {}) as Record<string, any>;
    const hostedSuccessCount = Number(latestHostedTask?.success_count ?? latestHostedTaskSummary.success_count ?? 0);
    const hostedFailedCount = Number(latestHostedTask?.failed_count ?? latestHostedTaskSummary.failed_count ?? 0);
    const hostedSkippedCount = Number(latestHostedTaskPreviewSummary.skipped_count ?? 0);
    const hostedSignalCount = Number(latestHostedTask?.signal_count ?? latestHostedTaskPreviewSummary.signal_count ?? (hostedSuccessCount + hostedFailedCount + hostedSkippedCount));
    const hostedProgress = Number(latestHostedTask?.progress ?? latestHostedTaskSummary.progress ?? 0);
    const signalSource = status?.signal_source_status;
    const signalSourceLabel = signalSource?.available
        ? 'MODEL READY'
        : (signalSource?.source === 'missing'
            ? 'MODEL MISSING'
            : (signalSource?.source === 'window_pending'
                ? 'WINDOW PENDING'
                : (signalSource?.source === 'expired'
                    ? 'WINDOW EXPIRED'
                    : (signalSource?.source === 'fallback'
                        ? 'FALLBACK BLOCKED'
                        : (signalSource?.source === 'mismatch'
                            ? 'SOURCE MISMATCH'
                            : 'MODEL UNAVAILABLE')))));
    const signalSourceBadgeTone = signalSource?.available
        ? 'bg-emerald-50 text-emerald-700 border-emerald-200'
        : (signalSource?.source === 'missing'
            ? 'bg-amber-50 text-amber-700 border-amber-200'
            : (signalSource?.source === 'window_pending'
                ? 'bg-sky-50 text-sky-700 border-sky-200'
                : (signalSource?.source === 'expired'
                    ? 'bg-rose-50 text-rose-700 border-rose-200'
                    : 'bg-amber-50 text-amber-700 border-amber-200')));
    const signalSourceStateText = signalSource?.available
        ? '已就绪'
        : (signalSource?.source === 'window_pending'
            ? '未到窗口'
            : (signalSource?.source === 'expired'
                ? '已过期'
                : (signalSource?.source === 'fallback'
                    ? '兜底拦截'
                    : (signalSource?.source === 'mismatch'
                        ? '来源不匹配'
                        : '未就绪'))));
    const signalSourceExplanation = signalSource?.available
        ? '模型推理信号已就绪，可在执行窗口内自动触发托管交易'
        : (signalSource?.source === 'missing'
            ? '请先设置默认模型并完成至少一次推理，系统才能获取交易信号'
            : (signalSource?.source === 'fallback'
                ? '当前推理数据为兜底结果，为保障安全已暂停自动托管'
                : (signalSource?.source === 'mismatch'
                    ? '推理数据来源与默认模型不匹配，请重新运行推理'
                    : (signalSource?.source === 'window_pending'
                        ? '推理结果已就绪，但当前日期未进入可执行时间窗口'
                        : (signalSource?.source === 'expired'
                            ? '推理结果已超过执行有效期，请重新运行推理获取最新信号'
                            : '等待模型推理结果...')))));

    const runtimeModeLabel = runtimeStatus === 'running'
        ? (runtimeMode === 'SHADOW'
            ? '影子运行中'
            : (runtimeMode === 'REAL'
                ? '实盘运行中'
                : (runtimeMode === 'SIMULATION' ? '模拟运行中' : '运行中')))
        : (runtimeStatus === 'starting'
            ? '启动中'
            : (runtimeMode === 'SHADOW'
                ? '影子已接入'
                : (runtimeMode === 'REAL'
                    ? '实盘已接入'
                    : (runtimeMode === 'SIMULATION' ? '模拟已接入' : '未启动'))));

    const runtimeModeTone = runtimeStatus === 'running'
        ? (runtimeMode === 'SHADOW'
            ? 'bg-violet-100 text-violet-800 border-violet-200'
            : (runtimeMode === 'REAL'
                ? 'bg-blue-100 text-blue-800 border-blue-200'
                : (runtimeMode === 'SIMULATION' ? 'bg-amber-100 text-amber-800 border-amber-200' : 'bg-emerald-50 text-emerald-700 border-emerald-200')))
        : (runtimeStatus === 'starting'
            ? 'bg-amber-100 text-amber-800 border-amber-200'
            : 'bg-gray-50 text-gray-500 border-gray-100');

    const scheduleText = activeLiveTradeConfig?.schedule_type === 'weekly'
        ? ((activeLiveTradeConfig.trade_weekdays && activeLiveTradeConfig.trade_weekdays.length > 0)
            ? `每周 ${activeLiveTradeConfig.trade_weekdays.join(' / ')}`
            : '每周执行')
        : (activeLiveTradeConfig?.rebalance_days ? `每 ${activeLiveTradeConfig.rebalance_days} 个交易日` : '启动后显示');

    const orderTypeText = activeLiveTradeConfig?.order_type
        ? `${activeLiveTradeConfig.order_type === 'MARKET' ? '市价' : '限价'}${typeof activeLiveTradeConfig.max_price_deviation === 'number'
            ? ` / 偏离 ${(activeLiveTradeConfig.max_price_deviation * 100).toFixed(2)}%`
            : ''}`
        : '启动后显示';

    const maxOrdersText = typeof activeLiveTradeConfig?.max_orders_per_cycle === 'number'
        ? `${activeLiveTradeConfig.max_orders_per_cycle} 单/轮`
        : '启动后显示';

    const monitorItemMap = useMemo(() => new Map(monitorChecks.map((item) => [item.key, item])), [monitorChecks]);
    
    const getMonitorItem = useCallback(
        (key: string): PreflightCheckItem | undefined => monitorItemMap.get(key),
        [monitorItemMap],
    );

    const getCheckMessage = useCallback((key: string, fallback = '未获取') => {
        const item = getMonitorItem(key);
        return item?.message || fallback;
    }, [getMonitorItem]);

    const getCheckTone = useCallback((key: string): boolean | undefined => {
        const item = getMonitorItem(key);
        if (!item) return undefined;
        // 检查 message 是否包含 [WARNING]，如果有则返回 false（异常状态）
        const messageStr = String(item.message || '');
        if (messageStr.includes('[WARNING]')) {
            return false;
        }
        return item.ok;
    }, [getMonitorItem]);

    const getQmtAgentDisplayMessage = useCallback((item?: PreflightCheckItem, fallback = '未获取'): string => {
        if (!item) return fallback;
        if (item.ok) return '已上报';
        const messageStr = String(item.message || '');
        if (messageStr.includes('过期') || messageStr.includes('stale')) return '已过期';
        if (messageStr.includes('检测失败') || messageStr.includes('异常')) return '检测异常';
        if (messageStr.includes('未检测到')) return '未上报';
        return '未上报';
    }, []);

    const getEnvDisplayMessage = useCallback((key: string, fallback: string): string => {
        const item = getMonitorItem(key);
        if (!item) return fallback;
        if (key === 'qmt_agent_online') return getQmtAgentDisplayMessage(item, fallback);
        return item.ok ? '已就绪' : (item.message || '未就绪');
    }, [getMonitorItem, getQmtAgentDisplayMessage]);

    const formatWebSocketStatus = useCallback((value: WebSocketStatus): { label: string; ok: boolean } => {
        switch (value) {
            case WebSocketStatus.CONNECTED: return { label: '已连接', ok: true };
            case WebSocketStatus.CONNECTING: return { label: '连接中', ok: false };
            case WebSocketStatus.RECONNECTING: return { label: '重连中', ok: false };
            case WebSocketStatus.ERROR: return { label: '异常', ok: false };
            default: return { label: '已断开', ok: false };
        }
    }, []);

    const wsStatusText = formatWebSocketStatus(websocketStatus);
    const getConnectionSignalLevel = useCallback((label: string, value: string, ok?: boolean): 'red' | 'yellow' | 'green' => {
        // 优先使用后端返回的 ok 状态判断
        // ok=true: 绿色（链路正常）
        // ok=false: 红色（链路异常）
        // ok=undefined: 黄色（未获取/未知状态）
        if (ok === true) {
            return 'green';
        }
        if (ok === false) {
            return 'red';
        }
        // ok 为 undefined/null 时，检查是否为连接中/重连中等过渡状态
        const normalized = `${label} ${value}`.toLowerCase();
        const isTransitioning = (
            normalized.includes('连接中')
            || normalized.includes('重连中')
            || normalized.includes('pending')
            || normalized.includes('初始化')
        );
        return isTransitioning ? 'yellow' : 'red';
    }, []);

    const envChecks = isGlobalSim
        ? [
            { label: '推理模型', value: getEnvDisplayMessage('inference_database_ready', '未获取'), ok: getCheckTone('inference_database_ready'), title: getCheckMessage('inference_database_ready') },
            { label: '沙箱进程池', value: getEnvDisplayMessage('simulation_sandbox_pool', '未获取'), ok: getCheckTone('simulation_sandbox_pool'), title: getCheckMessage('simulation_sandbox_pool') },
            { label: '实时行情', value: getEnvDisplayMessage('stream_series_freshness', '未获取'), ok: getCheckTone('stream_series_freshness'), title: getCheckMessage('stream_series_freshness') },
        ]
        : [
            { label: 'Runner 镜像', value: getEnvDisplayMessage('strategy_runner_image', '未获取'), ok: getCheckTone('strategy_runner_image'), title: getCheckMessage('strategy_runner_image') },
            { label: '运行容器状态', value: status?.k8s_status ? `Ready ${status.k8s_status.ready_replicas}/${status.k8s_status.replicas}` : getEnvDisplayMessage('orchestration', '未获取'), ok: status?.status === 'running' || getCheckTone('orchestration'), title: getCheckMessage('orchestration') },
            { label: 'QMT Agent', value: getEnvDisplayMessage('qmt_agent_online', '未获取'), ok: getCheckTone('qmt_agent_online'), title: getCheckMessage('qmt_agent_online') },
        ];

    const connectionChecks = [
        { label: 'Redis Signal', value: getCheckMessage('redis'), ok: getCheckTone('redis') },
        { label: 'PostgreSQL', value: getCheckMessage('db'), ok: getCheckTone('db') },
        { label: 'Data Feed', value: getCheckMessage('stream_series_freshness'), ok: getCheckTone('stream_series_freshness') },
        { label: 'WebSocket', value: wsStatusText.label, ok: wsStatusText.ok },
    ].map((item) => ({
        ...item,
        level: getConnectionSignalLevel(item.label, item.value, item.ok),
    }));
    const connectionAttentionCount = connectionChecks.filter((item) => item.level !== 'green').length;
    const connectionHealthyCount = connectionChecks.length - connectionAttentionCount;

    const loadHostedLogs = useCallback(async (taskId: string, reset = false) => {
        if (!taskId) return;
        try {
            const result = await realTradingService.getManualExecutionLogs(
                taskId,
                reset ? '0-0' : hostedCursorRef.current,
                200,
            );
            hostedCursorRef.current = result.next_id || hostedCursorRef.current;
            const lines = (result.entries || []).map((entry) => {
                const ts = entry.ts ? new Date(entry.ts).toLocaleTimeString() : '--:--:--';
                return `[${ts}] ${entry.line}`;
            });
            const next = reset ? lines : [...hostedLogsRef.current];
            if (!reset) {
                for (const line of lines) {
                    if (!next.includes(line)) next.push(line);
                }
            }
            hostedLogsRef.current = next;
            setHostedLogs(next);
        } catch (e) {
            console.warn('Failed to load hosted task logs', e);
        }
    }, []);

    useEffect(() => {
        if (!hostedLogsVisible || !latestHostedTask?.task_id) return;
        hostedCursorRef.current = '0-0';
        hostedLogsRef.current = [];
        setHostedLogs([]);
        let cancelled = false;
        let timer: number | undefined;
        const poll = async (reset = false) => {
            if (cancelled) return;
            await loadHostedLogs(latestHostedTask.task_id, reset);
            if (!cancelled) {
                timer = window.setTimeout(() => void poll(false), 2000);
            }
        };
        void poll(true);
        return () => {
            cancelled = true;
            if (timer) window.clearTimeout(timer);
        };
    }, [hostedLogsVisible, latestHostedTask?.task_id, loadHostedLogs]);

    return (
        <div className="h-full overflow-y-auto custom-scrollbar">
            <div className={`p-6 flex flex-col gap-6 ${selectedStrategy ? 'pb-32' : 'pb-16'}`}>
                {/* Header Control Bar */}
                <div className="bg-white rounded-2xl shadow-sm border border-slate-100 p-8 flex flex-col md:flex-row items-center justify-between gap-6">
                    <div className="flex-1">
                        <div className="flex items-center gap-3 mb-2">
                            <div className={`w-2.5 h-2.5 rounded-full ${isRunning ? 'bg-green-500 animate-pulse' : 'bg-slate-300'}`}></div>
                            <h2 className="text-xl font-bold text-slate-800">
                                {isGlobalSim ? '全自动模拟控制台' : '实盘策略控制台'}
                            </h2>
                        </div>
                        <div className="flex items-center gap-4 text-slate-500 text-sm">
                            <span className="flex items-center gap-1.5">
                                <Activity size={14} className={isGlobalSim ? 'text-indigo-500' : 'text-rose-500'} />
                                模式: <span className={`font-bold ${isGlobalSim ? 'text-indigo-600' : 'text-rose-600'}`}>
                                    {isGlobalSim ? '模拟运行' : '实盘交易'}
                                </span>
                            </span>
                            <span className="text-slate-200">|</span>
                            <span className="flex items-center gap-1.5 font-mono">USER: {userId}</span>
                            {status?.mode && (
                                <>
                                    <span className="text-slate-200">|</span>
                                    <span className={`px-2 py-0.5 rounded-full text-[10px] font-black border ${runtimeModeTone}`}>
                                        {runtimeModeLabel}
                                    </span>
                                </>
                            )}
                        </div>
                    </div>

                    <div className="flex flex-wrap items-center gap-4 justify-end">
                        {!isRunning ? (
                            <div className="flex flex-col items-end gap-2">
                                <div className="flex items-center gap-3">
                                    <div className="w-64">
                                        <Select
                                            value={selectedStrategyId || undefined}
                                            onChange={(value) => setSelectedStrategyId(String(value))}
                                            options={strategyOptions}
                                            placeholder="选择已验证策略..."
                                            className="w-full custom-antd-select-v2"
                                            size="large"
                                            showSearch
                                        />
                                    </div>
                                    <button onClick={loadStrategies} className="p-2.5 text-slate-400 hover:text-blue-600 border border-slate-200 rounded-xl">
                                        <RefreshCw size={18} className={loading ? 'animate-spin' : ''} />
                                    </button>
                                    {!isGlobalSim && (
                                        <div className="flex items-center gap-2 px-3 py-2 bg-slate-50 rounded-xl border border-slate-100">
                                            <span className="text-[10px] font-bold text-slate-600 tracking-wide whitespace-nowrap">影子模式</span>
                                            <button
                                                type="button"
                                                role="switch"
                                                aria-checked={isShadowMode}
                                                onClick={() => setIsShadowMode(!isShadowMode)}
                                                className={`relative inline-flex h-6 w-10 shrink-0 items-center rounded-full border transition-all focus:outline-none focus:ring-2 focus:ring-indigo-200 focus:ring-offset-0 ${
                                                    isShadowMode
                                                        ? 'bg-indigo-500 border-indigo-400'
                                                        : 'bg-slate-200 border-slate-300'
                                                }`}
                                            >
                                                <span
                                                    className={`inline-block h-4 w-4 transform rounded-full bg-white shadow-sm transition-transform ${
                                                        isShadowMode ? 'translate-x-5' : 'translate-x-1'
                                                    }`}
                                                />
                                            </button>
                                        </div>
                                    )}
                                    <button
                                        onClick={handleDeploy}
                                        disabled={isDeployDisabled}
                                        className={`px-8 py-2.5 rounded-xl text-sm font-bold text-white transition-all ${isDeployDisabled ? 'bg-slate-300' : (isShadowMode || isGlobalSim ? 'bg-indigo-500 hover:bg-indigo-600' : 'bg-blue-600 hover:bg-blue-700')}`}
                                    >
                                        <Play size={18} className="inline mr-2" />
                                        {selectedStrategy?.is_verified ? (isGlobalSim ? '开启实时模拟' : (isShadowMode ? '开启影子运行' : '启动实盘交易')) : '未经验证'}
                                    </button>
                                </div>
                                {error && <div className="text-[11px] text-rose-500 flex items-center gap-1"><AlertCircle size={10} /> {error}</div>}
                            </div>
                        ) : (
                            <button onClick={onStop} className="px-10 py-3 bg-rose-500 hover:bg-rose-600 text-white rounded-xl font-bold shadow-lg shadow-rose-100 flex items-center gap-2">
                                <Square size={20} fill="currentColor" /> 停止运行
                            </button>
                        )}
                    </div>
                </div>

                {/* 6-Module Grid */}
                <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
                    {/* Module 1: Execution Params */}
                    <div className="bg-white rounded-2xl p-6 border border-slate-100 shadow-sm flex flex-col">
                        <div className="flex items-center gap-3 mb-6 h-9">
                            <div className="p-2 bg-indigo-50 rounded-lg">
                                <Settings2 className="text-indigo-600" size={18} />
                            </div>
                            <h3 className="font-bold text-slate-800">策略执行参数</h3>
                        </div>
                        <div className="flex-1 space-y-4">
                            <div className="grid grid-cols-2 gap-3 text-sm">
                                <div className="rounded-xl bg-slate-50/70 p-3 border border-slate-100/50">
                                    <div className="text-[10px] font-black text-slate-400 uppercase mb-1">当前调仓周期</div>
                                    <div className="font-bold text-slate-700 text-xs truncate" title={scheduleText}>{scheduleText}</div>
                                </div>
                                <div className="rounded-xl bg-slate-50/70 p-3 border border-slate-100/50">
                                    <div className="text-[10px] font-black text-slate-400 uppercase mb-1">当前买卖时点</div>
                                    <div className="font-bold text-slate-700 text-xs truncate">
                                        {activeLiveTradeConfig?.sell_time && activeLiveTradeConfig?.buy_time
                                            ? `${activeLiveTradeConfig.sell_time} / ${activeLiveTradeConfig.buy_time}`
                                            : '启动后显示'}
                                    </div>
                                </div>
                                <div className="rounded-xl bg-slate-50/70 p-3 border border-slate-100/50">
                                    <div className="text-[10px] font-black text-slate-400 uppercase mb-1">当前委托方式</div>
                                    <div className="font-bold text-slate-700 text-xs truncate">{orderTypeText}</div>
                                </div>
                                <div className="rounded-xl bg-slate-50/70 p-3 border border-slate-100/50">
                                    <div className="text-[10px] font-black text-slate-400 uppercase mb-1">单轮最大委托</div>
                                    <div className="font-bold text-slate-700 text-xs truncate">{maxOrdersText}</div>
                                </div>
                            </div>
                            {activeExecutionConfig && (
                                <div className="rounded-xl border border-indigo-100 bg-indigo-50/30 p-3">
                                    <div className="text-[10px] font-black text-indigo-400 uppercase mb-1 tracking-wider">生效风控参数预览</div>
                                    <div className="flex items-center gap-3 text-[11px] font-bold text-indigo-700">
                                        <span>Max Buy Drop: {typeof activeExecutionConfig.max_buy_drop === 'number' ? `${(activeExecutionConfig.max_buy_drop * 100).toFixed(2)}%` : 'N/A'}</span>
                                        <span className="w-1 h-1 rounded-full bg-indigo-200"></span>
                                        <span>Stop Loss: {typeof activeExecutionConfig.stop_loss === 'number' ? `${(activeExecutionConfig.stop_loss * 100).toFixed(2)}%` : 'N/A'}</span>
                                    </div>
                                </div>
                            )}
                        </div>
                    </div>

                    {/* Module 2: Engine Status */}
                    <div className="bg-white rounded-2xl p-6 border border-slate-100 shadow-sm flex flex-col">
                        <div className="flex items-center justify-between gap-3 mb-6 h-9">
                            <div className="flex items-center gap-3">
                                <div className="p-2 bg-slate-50 rounded-lg">
                                    <Cpu className="text-slate-600" size={18} />
                                </div>
                                <div className="flex flex-col">
                                    <h3 className="font-bold text-slate-800">默认模型最新推理</h3>
                                    <span className="text-[10px] font-medium text-slate-400">
                                        展示当前默认模型最近一次可用于托管的完成推理批次
                                    </span>
                                </div>
                            </div>
                            <div className="flex items-center gap-2">
                                {latestInferenceRunIsNew && (
                                    <span className="text-[10px] font-black px-2 py-0.5 rounded-full border bg-amber-50 text-amber-700 border-amber-200">
                                        NEW
                                    </span>
                                )}
                                {latestInferenceRun?.run_id && (
                                    <span className={`text-[10px] font-black px-2 py-0.5 rounded-full border ${latestInferenceRun.matched_model === false ? 'bg-amber-50 text-amber-600 border-amber-200' : 'bg-emerald-50 text-emerald-600 border-emerald-200'}`}>
                                        {latestInferenceRun.matched_model === false ? '模型不匹配' : '模型已就绪'}
                                    </span>
                                )}
                            </div>
                        </div>
                        <div className="flex-1">
                            {latestInferenceRun?.run_id ? (
                                <div className="grid grid-cols-2 gap-3">
                                    <div className="col-span-2 rounded-xl bg-slate-50/70 p-3 border border-slate-100/50">
                                        <div className="text-[10px] font-black text-slate-400 uppercase mb-1">最新推理 RUN_ID</div>
                                        <div className="font-mono text-[10px] font-bold text-slate-700 truncate" title={latestInferenceRun.run_id}>
                                            {latestInferenceRun.run_id}
                                        </div>
                                    </div>
                                    <div className="rounded-xl bg-slate-50/70 p-3 border border-slate-100/50">
                                        <div className="text-[10px] font-black text-slate-400 uppercase mb-1">预测交易日</div>
                                        <div className="font-bold text-slate-700 text-xs truncate">{latestInferenceRun.prediction_trade_date || '-'}</div>
                                    </div>
                                    <div className="rounded-xl bg-slate-50/70 p-3 border border-slate-100/50">
                                        <div className="text-[10px] font-black text-slate-400 uppercase mb-1">预测状态</div>
                                        <div className="font-bold text-slate-700 text-xs flex items-center gap-2 truncate">
                                            <div className="w-1.5 h-1.5 rounded-full bg-emerald-500"></div>
                                            {latestInferenceRun.status || '-'}
                                        </div>
                                    </div>
                                    <div className="col-span-2 rounded-xl bg-slate-50/70 p-3 border border-slate-100/50">
                                        <div className="text-[10px] font-black text-slate-400 uppercase mb-1">当前默认模型</div>
                                        <div className="font-bold text-slate-600 text-xs truncate" title={effectiveModelDisplayName}>
                                            {effectiveModelDisplayName}
                                        </div>
                                    </div>
                                </div>
                            ) : (
                                <div className="h-full flex flex-col items-center justify-center text-slate-400 text-xs gap-2 border border-dashed border-slate-100 rounded-xl min-h-[160px]">
                                    <Cpu size={24} className="opacity-20" />
                                    <span>{latestInferenceRunLoading ? '正在检索最新推理...' : '暂无可用推理批次'}</span>
                                    <span className="text-[11px] text-slate-300">
                                        {latestInferenceRunLoading
                                            ? '请等待模型批次查询完成'
                                            : (signalSourceExplanation || '当前默认模型未返回可执行推理结果')}
                                    </span>
                                </div>
                            )}
                        </div>
                    </div>

                    {/* Module 3: Environment */}
                    <div className="bg-white rounded-2xl p-6 border border-slate-100 shadow-sm flex flex-col">
                        <div className="flex items-center gap-3 mb-6 h-9">
                            <div className="p-2 bg-blue-50 rounded-lg">
                                <Activity className="text-blue-600" size={18} />
                            </div>
                            <h3 className="font-bold text-slate-800">环境监控</h3>
                        </div>
                        <div className="flex-1 space-y-3">
                            <div className="grid grid-cols-2 gap-3">
                                {envChecks.map((item) => (
                                    <div key={item.label} className="col-span-2 rounded-xl bg-slate-50/70 p-3 border border-slate-100/50">
                                        <div className="flex items-center justify-between mb-1.5">
                                            <span className="text-[10px] font-black text-slate-400 uppercase">{item.label}</span>
                                            <span className={`text-[11px] font-black flex items-center gap-1.5 ${item.ok ? 'text-green-600' : (item.ok === false ? 'text-rose-600' : 'text-gray-500')}`}>
                                                <div className={`w-1.5 h-1.5 rounded-full ${item.ok ? 'bg-green-500' : (item.ok === false ? 'bg-rose-500' : 'bg-gray-400')}`}></div>
                                                {item.ok ? '正常' : (item.ok === false ? '异常' : '未获取')}
                                            </span>
                                        </div>
                                        <div className={`text-xs font-bold font-mono truncate ${item.ok === false ? 'text-rose-600' : 'text-slate-700'}`} title={item.value}>
                                            {item.value || '-'}
                                        </div>
                                    </div>
                                ))}
                            </div>
                        </div>
                    </div>

                    {/* Module 4: Connectivity */}
                    <div className="bg-white rounded-2xl p-6 border border-slate-100 shadow-sm flex flex-col min-h-[320px]">
                        <div className="flex items-center justify-between gap-3 mb-6 h-9">
                            <div className="flex items-center gap-3">
                                <div className="p-2 bg-purple-50 rounded-lg">
                                    <RefreshCw className="text-purple-600" size={18} />
                                </div>
                                <h3 className="font-bold text-slate-800">链路质量看板</h3>
                            </div>
                            <span className={`px-2.5 py-0.5 rounded-full text-[10px] font-black border ${
                                connectionAttentionCount === 0
                                    ? 'bg-emerald-50 text-emerald-700 border-emerald-200'
                                    : 'bg-amber-50 text-amber-700 border-amber-200'
                            }`}>
                                {connectionAttentionCount === 0 ? '链路稳定' : `关注 ${connectionAttentionCount} 项`}
                            </span>
                        </div>
                        <div className="flex-1 space-y-4">
                            <div className={`rounded-2xl border p-4 shadow-sm ${
                                connectionAttentionCount === 0
                                    ? 'bg-emerald-50/40 border-emerald-100'
                                    : 'bg-amber-50/30 border-amber-100'
                            }`}>
                                <div className="flex items-center justify-between gap-4">
                                    <div className="min-w-0">
                                        <div className="text-[10px] font-black text-slate-400 uppercase tracking-widest mb-1">链路总览</div>
                                        <div className="text-sm font-bold text-slate-700">
                                            {connectionAttentionCount === 0
                                                ? '当前核心链路均处于可用状态'
                                                : `当前有 ${connectionAttentionCount} 项链路需要优先关注`}
                                        </div>
                                    </div>
                                    <div className="text-right shrink-0">
                                        <div className="text-[10px] font-black text-slate-400 uppercase tracking-widest mb-1">健康数</div>
                                        <div className="text-2xl font-black leading-none text-slate-900">
                                            {connectionHealthyCount}
                                            <span className="text-sm font-bold text-slate-400">/{connectionChecks.length}</span>
                                        </div>
                                    </div>
                                </div>
                            </div>
                            <div className="grid grid-cols-2 gap-3">
                                {connectionChecks.map((item) => (
                                    <div
                                        key={item.label}
                                        className={`rounded-xl p-3 border shadow-sm ${
                                            item.level === 'red'
                                                ? 'bg-rose-50/70 border-rose-100'
                                                : item.level === 'yellow'
                                                    ? 'bg-amber-50/70 border-amber-100'
                                                    : 'bg-emerald-50/70 border-emerald-100'
                                        }`}
                                    >
                                        <div className="flex items-center justify-between gap-2 mb-2">
                                            <span className="min-w-0 text-[11px] font-black text-slate-400 tracking-[0.12em] leading-none whitespace-nowrap truncate">
                                                {item.label}
                                            </span>
                                            <span
                                                className={`shrink-0 w-3 h-3 rounded-full border ${
                                                    item.level === 'red'
                                                        ? 'bg-rose-500 border-rose-300 shadow-[0_0_0_4px_rgba(244,63,94,0.10)]'
                                                        : item.level === 'yellow'
                                                            ? 'bg-amber-400 border-amber-300 shadow-[0_0_0_4px_rgba(251,191,36,0.14)]'
                                                            : 'bg-emerald-500 border-emerald-300 shadow-[0_0_0_4px_rgba(16,185,129,0.10)]'
                                                }`}
                                                title={item.level === 'red' ? '红灯' : item.level === 'yellow' ? '黄灯' : '绿灯'}
                                                aria-label={item.level === 'red' ? '红灯' : item.level === 'yellow' ? '黄灯' : '绿灯'}
                                            />
                                        </div>
                                        <div className={`text-xs font-bold leading-tight ${
                                            item.level === 'red'
                                                ? 'text-rose-700'
                                                : item.level === 'yellow'
                                                    ? 'text-amber-700'
                                                    : 'text-slate-700'
                                        }`} title={item.value}>
                                            {item.value}
                                        </div>
                                    </div>
                                ))}
                            </div>
                        </div>
                    </div>

                    {/* Module 5: Automation Status */}
                    <div className="bg-white rounded-2xl p-6 border border-slate-100 shadow-sm flex flex-col min-h-[320px]">
                        <div className="flex items-center justify-between gap-3 mb-6 h-9">
                            <div className="flex items-center gap-3">
                                <div className="p-2 bg-emerald-50 rounded-lg">
                                    <Clock3 className="text-emerald-600" size={18} />
                                </div>
                                <h3 className="font-bold text-slate-800">自动化状态</h3>
                            </div>
                            <span className={`px-2 py-0.5 rounded-full text-[10px] font-black border ${
                                signalSourceBadgeTone
                            }`}>
                                {signalSourceLabel}
                            </span>
                        </div>
                        <div className="flex-1 space-y-4">
                            <div className={`rounded-2xl border p-4 shadow-sm ${
                                signalSource?.available
                                    ? 'bg-emerald-50/35 border-emerald-100'
                                    : 'bg-slate-50/70 border-slate-100/50'
                            }`}>
                                <div className="flex items-start justify-between gap-3 mb-2.5">
                                    <div className="min-w-0">
                                        <div className="text-[10px] font-black text-emerald-500 uppercase tracking-widest">默认模型推理反馈与诊断</div>
                                        <div className="mt-1 text-sm font-bold leading-relaxed text-slate-700">
                                            {signalSourceExplanation}
                                        </div>
                                    </div>
                                    <div className={`shrink-0 text-[10px] font-bold px-2 py-0.5 rounded-full border ${signalSourceBadgeTone}`}>
                                        {signalSourceStateText}
                                    </div>
                                </div>
                            </div>
                            <div className="grid grid-cols-2 gap-3">
                                <div className="rounded-xl bg-slate-50/70 p-3 border border-slate-100/50 shadow-sm">
                                    <div className="text-[10px] font-black text-slate-400 uppercase tracking-widest mb-1">最新推理批次</div>
                                    <div className="font-mono text-[10px] font-bold text-slate-700 truncate" title={status?.latest_signal_run_id || 'UNKNOWN'}>
                                        {status?.latest_signal_run_id || 'UNKNOWN'}
                                    </div>
                                </div>
                                <div className="rounded-xl bg-slate-50/70 p-3 border border-slate-100/50 shadow-sm">
                                    <div className="text-[10px] font-black text-slate-400 uppercase tracking-widest mb-1">托管运行状态</div>
                                    <div className="font-bold text-slate-700 text-xs truncate">
                                        {status?.status === 'running' ? '运行中' : '静态就绪'}
                                    </div>
                                </div>
                                <div className="rounded-xl bg-slate-50/70 p-3 border border-slate-100/50 shadow-sm">
                                    <div className="text-[10px] font-black text-slate-400 uppercase tracking-widest mb-1">当前默认模型</div>
                                    <div className="font-bold text-slate-600 text-xs truncate" title={effectiveModelDisplayName}>
                                        {effectiveModelDisplayName}
                                    </div>
                                </div>
                                <div className="rounded-xl bg-slate-50/70 p-3 border border-slate-100/50 shadow-sm">
                                    <div className="text-[10px] font-black text-slate-400 uppercase tracking-widest mb-1">执行窗口</div>
                                    <div className="font-bold text-slate-700 text-xs truncate">
                                        {signalSource?.available
                                            ? `${signalSource.execution_window_start?.slice(5) || '-'} ~ ${signalSource.execution_window_end?.slice(5) || '-'}`
                                            : '不可用'}
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>

                    {/* Module 6: Task Reporting */}
                    <div className="bg-white rounded-2xl p-6 border border-slate-100 shadow-sm ring-1 ring-blue-500/5 flex flex-col">
                        <div className="flex items-center justify-between gap-3 mb-6 h-9">
                            <div className="flex items-center gap-3">
                                <div className="p-2 bg-amber-50 rounded-lg">
                                    <FileText className="text-amber-600" size={18} />
                                </div>
                                <div>
                                    <h3 className="font-bold text-slate-800">最新任务汇报</h3>
                                </div>
                            </div>
                            <button
                                type="button"
                                onClick={() => setHostedLogsVisible(!hostedLogsVisible)}
                                className={`p-1.5 rounded-lg border transition-all ${hostedLogsVisible ? 'bg-slate-800 border-slate-700 text-white' : 'bg-white border-slate-200 text-slate-400 hover:text-blue-600'}`}
                                title={hostedLogsVisible ? '关闭日志流' : '查看实时日志'}
                            >
                                <TerminalSquare size={16} />
                            </button>
                        </div>

                        <div className="flex-1">
                            {!latestHostedTask ? (
                                <div className="h-full flex flex-col items-center justify-center text-slate-400 text-xs gap-3 border border-dashed border-slate-100 rounded-xl bg-slate-50/30 min-h-[200px]">
                                    <div className="w-10 h-10 rounded-full bg-slate-100/50 flex items-center justify-center">
                                        <Activity size={20} className="opacity-30" />
                                    </div>
                                    <span className="font-bold">今日暂未触发自动化托管任务</span>
                                </div>
                            ) : (
                                <div className="space-y-4">
                                    <div className="rounded-2xl border border-slate-200 bg-white text-slate-900 p-4 shadow-sm">
                                        <div className="flex items-center justify-between gap-4">
                                            <div className="min-w-0 flex-1">
                                                <div className="text-[10px] font-black text-slate-400 uppercase mb-1">任务状态</div>
                                                <span className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-[10px] font-black border ${taskStatusTone(latestHostedTask.status)}`}>
                                                    {formatTaskStatus(latestHostedTask.status)}
                                                </span>
                                            </div>
                                            <div className="text-right">
                                                <div className="text-[10px] font-black text-slate-400 uppercase mb-1">完成度</div>
                                                <div className="text-2xl font-black leading-none text-slate-900">{Number.isFinite(hostedProgress) ? hostedProgress : 0}%</div>
                                            </div>
                                        </div>
                                        <div className="mt-3 h-2 rounded-full bg-slate-100 overflow-hidden">
                                            <div
                                                className="h-full rounded-full bg-gradient-to-r from-blue-500 via-cyan-400 to-emerald-400 transition-all"
                                                style={{ width: `${Math.max(0, Math.min(100, Number.isFinite(hostedProgress) ? hostedProgress : 0))}%` }}
                                            />
                                        </div>
                                    </div>

                                    <div className="grid grid-cols-3 gap-2">
                                        <div className="p-3 bg-emerald-50 rounded-lg border border-emerald-100 text-center shadow-sm">
                                            <div className="text-[9px] font-black text-emerald-600/70 uppercase">成功数</div>
                                            <div className="text-sm font-black text-emerald-700">{hostedSuccessCount}</div>
                                        </div>
                                        <div className="p-3 bg-rose-50 rounded-lg border border-rose-100 text-center shadow-sm">
                                            <div className="text-[9px] font-black text-rose-600/70 uppercase">失败数</div>
                                            <div className="text-sm font-black text-rose-700">{hostedFailedCount}</div>
                                        </div>
                                        <div className="p-3 bg-slate-50 rounded-lg border border-slate-200 text-center shadow-sm">
                                            <div className="text-[9px] font-black text-slate-400 uppercase">跳过数</div>
                                            <div className="text-sm font-black text-slate-700">{hostedSkippedCount}</div>
                                        </div>
                                    </div>

                                    <div className="grid grid-cols-2 gap-2">
                                        <div className="rounded-xl bg-slate-50 p-3 border border-slate-100 shadow-sm">
                                            <div className="text-[10px] font-black text-slate-400 uppercase mb-1">目标跨度</div>
                                            <div className="font-bold text-slate-700 text-xs truncate">
                                                {latestHostedTaskPreviewSummary.target_horizon_days ? `${latestHostedTaskPreviewSummary.target_horizon_days} 个交易日` : '-'}
                                            </div>
                                        </div>
                                        <div className="rounded-xl bg-slate-50 p-3 border border-slate-100 shadow-sm">
                                            <div className="text-[10px] font-black text-slate-400 uppercase mb-1">执行窗口</div>
                                            <div
                                                className="font-bold text-slate-700 text-[11px] leading-tight truncate"
                                                title={`${latestHostedRequest.execution_window?.start || '-'} ~ ${latestHostedRequest.execution_window?.end || '-'}`}
                                            >
                                                {latestHostedRequest.execution_window?.start || '-'} ~ {latestHostedRequest.execution_window?.end || '-'}
                                            </div>
                                        </div>
                                    </div>

                                    <div className="flex items-center gap-2">
                                        <button
                                            type="button"
                                            onClick={() => setHostedLogsVisible(!hostedLogsVisible)}
                                            className="flex-1 py-2.5 rounded-xl bg-slate-50 text-slate-800 text-[11px] font-black hover:bg-slate-100 transition-all flex items-center justify-center gap-2 border border-slate-200 shadow-sm"
                                        >
                                            <TerminalSquare size={14} />
                                            {hostedLogsVisible ? '收起任务日志' : '查看任务日志'}
                                        </button>
                                        {onOpenManualTask && (
                                            <button
                                                type="button"
                                                onClick={onOpenManualTask}
                                                className="flex-1 py-2.5 rounded-xl bg-blue-50 text-blue-700 text-[11px] font-black hover:bg-blue-100 transition-all flex items-center justify-center gap-2 border border-blue-100 shadow-sm"
                                            >
                                                <Activity size={14} /> 查看详情
                                            </button>
                                        )}
                                    </div>
                                </div>
                            )}
                        </div>
                    </div>
                </div>

                {/* Logs Stream */}
                {hostedLogsVisible && latestHostedTask?.task_id && (
                    <div className="bg-white rounded-2xl border border-slate-100 shadow-sm overflow-hidden mt-6">
                        <div className="px-6 py-4 bg-slate-50 border-b border-slate-100 flex items-center justify-between">
                            <span className="text-[10px] font-black text-slate-400 uppercase tracking-widest">Execution Logstream</span>
                            <button onClick={() => setHostedLogsVisible(false)} className="text-slate-400 hover:text-slate-700"><Square size={14} /></button>
                        </div>
                        <div className="p-4 h-64 overflow-y-auto font-mono text-[10px] text-slate-600 custom-scrollbar bg-white">
                            {hostedLogs.length === 0 ? <div className="text-slate-400 animate-pulse text-center mt-20">Waiting for logs...</div> : hostedLogs.map((line, i) => (
                                <div key={i} className="hover:bg-slate-50 px-2 py-0.5 whitespace-pre-wrap">{line}</div>
                            ))}
                        </div>
                    </div>
                )}
            </div>
        </div>
    );
};

export default StrategyManagement;
