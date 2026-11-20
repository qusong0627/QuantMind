import React, { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import { LayoutDashboard, PieChart, FileText, Settings, User, ClipboardList, Clock, ShieldCheck } from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import { Button, Collapse, Modal, Spin, Tag, message } from 'antd';
import TopBar from './components/TopBar';
import { TradingShell, TradingSidebar, TradingMain } from '../../components/layout/TradingShell';
import TopologyConsole from './tabs/StrategyConsole/TopologyConsole';
import ManualTaskPage from './tabs/ManualTaskPage';
import PersonalCenter from './tabs/PersonalCenter';
import PositionMonitor from './tabs/PositionMonitor';
import TradingHistory from './tabs/TradingHistory';
import SettingsCenter from './tabs/SettingsCenter';
import RiskControlTab from './tabs/RiskControl';
import ReplayPage from './tabs/ReplayPage';
import type { RealTradingStatus, AccountInfo, PreflightCheckResponse, PreflightCheckItem } from '../../services/realTradingService';
import { authService } from '../../features/auth/services/authService';
import type { StrategyFile } from '../../types/backtest/strategy';
import { useAppSelector } from '../../store';
import { selectCurrentMarket } from '../../store/slices/uiSlice';
import { useTradeWebSocket } from '../../hooks/useTradeWebSocket';
import { buildTradingTopBarAccountInfo, resolveTradingAccountMode } from './utils/accountAdapter';
import LiveTradeConfigWizard from './components/LiveTradeConfigWizard';
import type { DeployMode, ExecutionConfig, LiveTradeConfig } from '../../types/liveTrading';

type TradingMode = 'real' | 'simulation'; // 仅保留模拟交易：实盘入口已隐藏，store 被强制为 simulation（恢复见 git 历史）
type ActiveTab = 'manage' | 'manual-task' | 'personal' | 'position' | 'history' | 'settings' | 'replay' | 'risk';
type PreflightStage = 'trading-readiness' | 'preflight';
type PendingDeploy = {
    strategyId: string;
    mode: DeployMode;
    executionConfig: ExecutionConfig;
    liveTradeConfig: LiveTradeConfig;
};
type TradingReadinessCheckItem = {
    key: string;
    label: string;
    passed: boolean;
    detail: string;
};
type TradingReadinessResult = {
    passed: boolean;
    checked_at: string;
    items: TradingReadinessCheckItem[];
    trading_permission?: string;
    signal_readiness?: {
        message?: string;
        latest_run_id?: string | null;
        prediction_trade_date?: string | null;
        signal_count?: number;
        trading_permission?: string;
    } | null;
};
const permissionTag = (permission?: string) => {
    if (permission === 'observe_only') {
        return <Tag color="processing" className="ml-2">观察态</Tag>;
    }
    if (permission === 'blocked') {
        return <Tag color="error" className="ml-2">阻断</Tag>;
    }
    return <Tag color="success" className="ml-2">可交易</Tag>;
};

const getEnvTenantId = (): string => {
    const env = (import.meta as ImportMeta & { env?: Record<string, string | undefined> }).env;
    return String(env?.VITE_TENANT_ID || 'default').trim() || 'default';
};

const getErrorHttpStatus = (err: unknown): number | undefined => {
    if (typeof err !== 'object' || err === null) return undefined;
    const response = (err as { response?: { status?: number } }).response;
    return response?.status;
};

// 实盘通道文案已随模拟专用化移除（恢复见 git 历史）。

const RealTradingPage: React.FC = () => {
    const currentMarket = useAppSelector(selectCurrentMarket);
    const [activeTab, setActiveTab] = useState<ActiveTab>('manage');

    // 券商通道卡「去配置凭证」跳转：切到设置页签
    useEffect(() => {
        const handler = () => setActiveTab('settings');
        window.addEventListener('goto-trading-settings', handler);
        return () => window.removeEventListener('goto-trading-settings', handler);
    }, []);
    const [tenantId] = useState<string>(getEnvTenantId);
    const [userId] = useState(() => {
        try {
            const raw = localStorage.getItem('user');
            if (raw) {
                const u = JSON.parse(raw);
                return String(u.user_id || u.id || u.username || 'user_1001');
            }
        } catch {
            // ignore
        }
        return 'user_1001';
    });
    // 仅保留模拟交易：页内无切换入口，强制 simulation（store 由 useTradingModeInitialization 兜底）
    const tradingMode: TradingMode = 'simulation';
    const [status, setStatus] = useState<RealTradingStatus | null>(null);
    const [accountInfo, setAccountInfo] = useState<AccountInfo | null>(null);
    const [preflightResult, setPreflightResult] = useState<PreflightCheckResponse | null>(null);
    const [preflightModalOpen, setPreflightModalOpen] = useState(false);
    const [preflightLoading, setPreflightLoading] = useState(false);
    const [preflightLoadError, setPreflightLoadError] = useState<string | null>(null);
    const [preflightMode, setPreflightMode] = useState<DeployMode | null>(null);
    const [preflightStage, setPreflightStage] = useState<PreflightStage>('trading-readiness');
    const [pendingDeploy, setPendingDeploy] = useState<PendingDeploy | null>(null);
    const [effectiveExecutionConfig, setEffectiveExecutionConfig] = useState<ExecutionConfig | null>(null);
    const [effectiveLiveTradeConfig, setEffectiveLiveTradeConfig] = useState<LiveTradeConfig | null>(null);
    const [pollingPausedByAuth, setPollingPausedByAuth] = useState(false);
    const [tradingReadinessResult, setTradingReadinessResult] = useState<TradingReadinessResult | null>(null);
    const [wizardOpen, setWizardOpen] = useState(false);
    const [wizardStrategy, setWizardStrategy] = useState<StrategyFile | null>(null);
    const [wizardMode, setWizardMode] = useState<DeployMode>('SIMULATION');
    const [confirmStarting, setConfirmStarting] = useState(false);
    const [revealedItemCount, setRevealedItemCount] = useState(0);
    const [isRevealing, setIsRevealing] = useState(false);
    const preflightRequestSeqRef = useRef(0);
    const isFetchingRef = useRef(false);

    const fetchData = useCallback(async () => {
        if (isFetchingRef.current) return;

        const token = authService.getAccessToken();
        if (!token) {
            setPollingPausedByAuth(true);
            setStatus(null);
            setAccountInfo(null);
            setEffectiveExecutionConfig(null);
            return;
        }

        isFetchingRef.current = true;
        try {
            const { realTradingService } = await import('../../services/realTradingService');
            const statusData = await realTradingService.getStatus(userId, tradingMode, tenantId);
            const runtimeMode = resolveTradingAccountMode(statusData?.mode, tradingMode);
            const accountData = await realTradingService.getRuntimeAccount(userId, tenantId, runtimeMode, currentMarket).catch(() => null);

            setStatus(statusData);
            setAccountInfo(accountData);
            setEffectiveExecutionConfig(statusData?.execution_config || null);
            setEffectiveLiveTradeConfig(statusData?.live_trade_config || null);
            setPollingPausedByAuth(false);
        } catch (e: unknown) {
            const httpStatus = getErrorHttpStatus(e);
            if (httpStatus === 401) {
                setPollingPausedByAuth(true);
                setStatus(null);
                setAccountInfo(null);
                setEffectiveExecutionConfig(null);
                setEffectiveLiveTradeConfig(null);
                return;
            }

            // 处理 503 服务不可用 (如 Celery Worker 宕机)
            if (httpStatus === 503) {
                console.warn("Trading service temporarily unavailable (503)");
            } else {
                console.error("Failed to fetch data", e);
            }

            setStatus(null);
            setAccountInfo(null);
            setEffectiveExecutionConfig(null);
            setEffectiveLiveTradeConfig(null);
        } finally {
            isFetchingRef.current = false;
        }
    }, [tenantId, userId, tradingMode]);

    useEffect(() => {
        if (pollingPausedByAuth) {
            return;
        }
        fetchData();
        const interval = setInterval(fetchData, 5000);
        
        // Listen for manual refresh events
        const handleManualRefresh = () => {
            console.log('Manual refresh event triggered');
            fetchData();
        };
        window.addEventListener('refresh-account-data', handleManualRefresh);
        window.addEventListener('refresh-strategy-status', handleManualRefresh);

        return () => {
            clearInterval(interval);
            window.removeEventListener('refresh-account-data', handleManualRefresh);
            window.removeEventListener('refresh-strategy-status', handleManualRefresh);
        };
    }, [fetchData, pollingPausedByAuth]);

    useEffect(() => {
        if (!pollingPausedByAuth) return;
        const tryResume = () => {
            if (authService.getAccessToken()) {
                setPollingPausedByAuth(false);
            }
        };
        const timer = setInterval(tryResume, 3000);
        window.addEventListener('focus', tryResume);
        window.addEventListener('storage', tryResume);
        return () => {
            clearInterval(timer);
            window.removeEventListener('focus', tryResume);
            window.removeEventListener('storage', tryResume);
        };
    }, [pollingPausedByAuth]);

    // 实时交易推送：收到成交事件后立即刷新账户/订单数据
    useTradeWebSocket({
        userId,
        enabled: !pollingPausedByAuth,
        onTradeEvent: useCallback(() => {
            fetchData();
        }, [fetchData]),
    });

    const runtimeStatus = status?.status;
    const isRuntimeActive = runtimeStatus === 'running' || runtimeStatus === 'starting';
    const strategyStatus: 'running' | 'starting' | 'stopped' = runtimeStatus === 'running'
        ? 'running'
        : (runtimeStatus === 'starting' ? 'starting' : 'stopped');
    const resolvedRunMode: DeployMode | undefined = isRuntimeActive
        ? 'SIMULATION'
        : undefined;
    const resolvedOrchestrationMode: 'docker' | 'k8s' | undefined = isRuntimeActive
        ? status?.orchestration_mode
        : undefined;

    const executeDeploy = useCallback(async (
        strategyId: string,
        mode: DeployMode,
        executionConfig: ExecutionConfig,
        liveTradeConfig: LiveTradeConfig,
    ): Promise<boolean> => {
        try {
            const { realTradingService } = await import('../../services/realTradingService');
            const startResp = await realTradingService.start(
                userId,
                strategyId,
                mode,
                tenantId,
                executionConfig,
                liveTradeConfig,
            );

            // 10万并发架构核心：激活策略至 Redis 匹配池
            try {
                const { strategyManagementService } = await import('../../services/strategyManagementService');
                await strategyManagementService.activateStrategy(strategyId);
                console.info('Strategy configuration activated in Redis pool');
            } catch (actErr: unknown) {
                console.warn('Strategy activation in Redis failed:', actErr);
            }

            if (startResp?.effective_execution_config) {
                setEffectiveExecutionConfig(startResp.effective_execution_config);
            }
            if (startResp?.effective_live_trade_config) {
                setEffectiveLiveTradeConfig(startResp.effective_live_trade_config);
            }

            const modeText = '模拟盘';
            const permissionText = startResp?.trading_permission === 'observe_only'
                ? '（观察态，不自动下单）'
                : '';
            message.success(`${modeText}部署请求已提交${permissionText}`);
            fetchData();
            return true;
        } catch (err: unknown) {
            const { realTradingService } = await import('../../services/realTradingService');
            const precheckFailure = realTradingService.extractTradingPrecheckFailure(err);
            if (precheckFailure) {
                setPreflightStage('trading-readiness');
                setPreflightModalOpen(true);
                setPreflightLoading(false);
                setPreflightLoadError(null);
                setTradingReadinessResult({
                    passed: false,
                    checked_at: precheckFailure.checked_at || new Date().toISOString(),
                    items: precheckFailure.items,
                    trading_permission: precheckFailure.trading_permission,
                    signal_readiness: precheckFailure.signal_readiness,
                });
            }
            message.error(realTradingService.getFriendlyError(err));
            return false;
        }
    }, [fetchData, tenantId, userId]);

    const handleDeploy = async (
        strategyId: string,
        isShadow: boolean,
        strategy?: StrategyFile | null,
    ) => {
        const mode: DeployMode = 'SIMULATION';
        setWizardStrategy(strategy || { id: strategyId, name: strategyId, source: 'personal', code: '' });
        setWizardMode(mode);
        setWizardOpen(true);
    };

    const handleWizardConfirm = useCallback(async (payload: {
        execution_config: ExecutionConfig;
        live_trade_config: LiveTradeConfig;
    }) => {
        if (!wizardStrategy) return;
        const mode = wizardMode;
        const requestSeq = ++preflightRequestSeqRef.current;
        setPreflightMode(mode);
        setPreflightStage('trading-readiness');
        setPreflightModalOpen(true);
        setPreflightLoading(true);
        setPreflightLoadError(null);
        setPreflightResult(null);
        setTradingReadinessResult(null);
        setPendingDeploy({
            strategyId: wizardStrategy.id,
            mode,
            executionConfig: payload.execution_config,
            liveTradeConfig: payload.live_trade_config,
        });
        setWizardOpen(false);

        try {
            const { realTradingService } = await import('../../services/realTradingService');
            const tradingReadiness = await Promise.race([
                realTradingService.getTradingPrecheck(mode),
                new Promise<never>((_, reject) =>
                    setTimeout(() => reject(new Error('交易准备度检测超时')), 10000)
                ),
            ]);
            if (requestSeq !== preflightRequestSeqRef.current) return;
            setTradingReadinessResult(tradingReadiness);
            setPreflightLoading(false);

            if (!tradingReadiness.passed) {
                const blockers = tradingReadiness.items.filter((item) => !item.passed);
                const blockerText = blockers.map((item) => item.label).join('、') || '交易准备度未通过';
                message.error(`交易准备度检测未通过：${blockerText}`);
                return;
            }
            if (tradingReadiness.trading_permission === 'observe_only') {
                message.info('当前没有可交易信号，将以观察态启动，不会自动下单');
            }

            // 模拟盘：单阶段精简自检，不再二次调用 /preflight
            if (mode === 'SIMULATION') {
                setPreflightStage('preflight');
                setPreflightResult({
                    ready: true,
                    mode: 'SIMULATION',
                    user_id: userId,
                    tenant_id: tenantId,
                    trading_permission: tradingReadiness.trading_permission,
                    signal_readiness: tradingReadiness.signal_readiness,
                    checks: tradingReadiness.items.map((item) => ({
                        key: item.key,
                        label: item.label,
                        ok: item.passed,
                        required: true,
                        message: item.detail,
                        details: {},
                    })),
                });
                message.success('自检通过，请确认后启动模拟盘');
                return;
            }

            setPreflightStage('preflight');
            setPreflightLoading(true);
            const preflight = await Promise.race([
                realTradingService.preflight(mode, userId, tenantId),
                new Promise<never>((_, reject) =>
                    setTimeout(() => reject(new Error('启动前自检超时')), 10000)
                ),
            ]);
            if (requestSeq !== preflightRequestSeqRef.current) return;
            setPreflightResult(preflight);
            setPreflightLoading(false);

            if (!preflight.ready) {
                const blockers = preflight.checks.filter((item) => item.required && !item.ok);
                const blockerText = blockers.map((item) => item.label).join('、') || '关键依赖未就绪';
                message.error(`启动前自检未通过：${blockerText}`);
                return;
            }

            const nonBlockingWarnings = preflight.checks.filter((item) => !item.required && !item.ok);
            if (nonBlockingWarnings.length > 0) {
                message.warning(
                    `启动前提示：${nonBlockingWarnings.map((item) => item.label).join('、')}`
                );
            }
            message.success('自检通过，请确认后启动运行容器');
        } catch (err: unknown) {
            if (requestSeq !== preflightRequestSeqRef.current) return;
            const { realTradingService } = await import('../../services/realTradingService');
            const friendly = realTradingService.getFriendlyError(err);
            setPreflightLoadError(friendly);
            setPreflightLoading(false);
            message.error(friendly);
        }
    }, [executeDeploy, tenantId, tradingMode, userId, wizardMode, wizardStrategy]);

    const visiblePreflightChecks = useMemo(() => {
        if (preflightStage === 'trading-readiness') {
            return (tradingReadinessResult?.items || []).map((item) => ({
                key: item.key,
                label: item.label,
                ok: item.passed,
                required: true,
                message: item.detail,
                details: {},
            }));
        }
        if (!preflightResult) return [];
        return preflightResult.checks;
    }, [preflightResult, preflightStage, tradingReadinessResult]);

    const closePreflightModal = useCallback(() => {
        preflightRequestSeqRef.current += 1;
        setPreflightModalOpen(false);
        setPendingDeploy(null);
        setPreflightLoading(false);
        setPreflightLoadError(null);
        setTradingReadinessResult(null);
        setPreflightResult(null);
        setConfirmStarting(false);
        setRevealedItemCount(0);
        setIsRevealing(false);
    }, []);

    const confirmStartLabel = useMemo(() => {
        if (!pendingDeploy) return '确认并启动';
        return '确认并启动模拟盘';
    }, [pendingDeploy, tradingMode]);

    // 检测结果全部展示，不做逐项 reveal（加快加载速度）
    useEffect(() => {
        const items = preflightResult?.checks || tradingReadinessResult?.items || [];
        if (items.length > 0) {
            setRevealedItemCount(items.length);
            setIsRevealing(false);
        }
    }, [preflightResult, tradingReadinessResult]);

    const handleStop = async () => {
        // 允许在 running/starting 状态下停止，也允许在不确定状态下尝试停止（防止状态不同步）
        const isStoppable = status?.status === 'running' || status?.status === 'starting';
        if (!isStoppable && status?.status !== undefined) {
            // 如果明确知道状态且不是运行中，提示用户
            message.warning('当前策略未运行，无需停止');
            return;
        }
        try {
            const currentStrategyId = status?.strategy?.id;
            const { realTradingService } = await import('../../services/realTradingService');
            await realTradingService.stop(userId, tenantId);

            // 10万并发架构核心：从 Redis 匹配池移除策略
            if (currentStrategyId) {
                try {
                    const { strategyManagementService } = await import('../../services/strategyManagementService');
                    await strategyManagementService.deactivateStrategy(currentStrategyId);
                } catch (deactErr) {
                    console.warn('Strategy deactivation in Redis failed:', deactErr);
                }
            }

            message.success('停止指令已下达');
            setEffectiveExecutionConfig(null);
            setEffectiveLiveTradeConfig(null);
            fetchData();
        } catch (err: unknown) {
            const { realTradingService } = await import('../../services/realTradingService');
            const errorMsg = realTradingService.getFriendlyError(err);
            // 如果是404或策略未运行，给出更友好的提示
            if (errorMsg.includes('404') || errorMsg.includes('未运行') || errorMsg.includes('not running')) {
                message.info('策略当前未运行，已清理相关资源');
                setEffectiveExecutionConfig(null);
                setEffectiveLiveTradeConfig(null);
                fetchData();
                return;
            }
            message.error(errorMsg);
        }
    };

    const tabs: Array<{ id: ActiveTab; label: string; icon: LucideIcon }> = [
        { id: 'manage', label: '策略管理', icon: LayoutDashboard },
        // 时光回放功能尚存多处问题，暂时隐藏入口，完善后取消注释即可恢复（ReplayPage 渲染分支保留）
        // { id: 'replay', label: '时光回放', icon: Clock },
        { id: 'manual-task', label: '手动任务', icon: ClipboardList },
        { id: 'position', label: '持仓监控', icon: PieChart },
        { id: 'history', label: '交易记录', icon: FileText },
        { id: 'risk', label: '风险控制', icon: ShieldCheck },
        { id: 'personal', label: '个人中心', icon: User },
        { id: 'settings', label: '设置', icon: Settings },
    ];

    // 侧边栏分组（顺序与上方 tabs 对应；增删 tab 时同步维护）
    const tabSections: Array<{ label: string; ids: ActiveTab[] }> = [
        { label: '交易', ids: ['manage', 'manual-task'] },
        { label: '组合与风控', ids: ['position', 'history', 'risk'] },
        { label: '账户', ids: ['personal', 'settings'] },
    ];

    return (
        <TradingShell>
            <div className="flex h-full w-full flex-col overflow-hidden font-sans">
                {/* 账户概览：TopBar 自带背景与下边框，按内容自然高度占位 */}
                <div className="relative z-10 shrink-0">
                    <TopBar
                        isConnected={!!status}
                        strategyStatus={strategyStatus}
                        tradingMode={tradingMode}
                        runMode={resolvedRunMode}
                        orchestrationMode={resolvedOrchestrationMode}
                        accountInfo={(() => {
                            return accountInfo ? buildTradingTopBarAccountInfo(accountInfo, status) : undefined;
                        })()}
                    />
                </div>

                <div className="flex min-h-0 flex-1 overflow-hidden">
                    {/* 左侧导航：品牌头 + 分组菜单 + 运行状态卡 */}
                    <TradingSidebar>
                        <div className="border-b border-slate-100 px-5 py-4">
                            <div className="flex items-center gap-2.5">
                                <div className="flex h-8 w-8 items-center justify-center rounded-xl bg-[#172238] text-white shadow-sm">
                                    <LayoutDashboard size={15} />
                                </div>
                                <div>
                                    <div className="text-[13px] font-extrabold tracking-tight text-slate-800">交易工作台</div>
                                    <div className="mt-0.5 text-[12px] font-bold uppercase tracking-[0.18em] text-slate-400">Workspace</div>
                                </div>
                            </div>
                        </div>

                        <nav className="custom-scrollbar flex-1 space-y-3 overflow-y-auto px-3 py-2">
                            {tabSections.map((section) => {
                                const items = tabs.filter((tab) => section.ids.includes(tab.id));
                                if (items.length === 0) return null;
                                return (
                                    <div key={section.label}>
                                        <div className="mb-1.5 px-3 text-[12px] font-extrabold uppercase tracking-[0.18em] text-slate-400">{section.label}</div>
                                        <div className="space-y-1">
                                            {items.map((tab) => {
                                                const selected = activeTab === tab.id;
                                                return (
                                                    <button
                                                        key={tab.id}
                                                        onClick={() => setActiveTab(tab.id)}
                                                        className={`group relative flex w-full items-center gap-3 rounded-xl px-3 py-2.5 text-[13px] transition-all duration-200 ${
                                                            selected
                                                                ? 'bg-white font-bold text-indigo-600 shadow-[0_5px_18px_-8px_rgba(79,70,229,0.35)] ring-1 ring-slate-200/70'
                                                                : 'font-medium text-slate-500 hover:bg-white/70 hover:text-slate-800'
                                                        }`}
                                                    >
                                                        {selected && <span className="absolute -left-3 h-5 w-[3px] rounded-r-full bg-indigo-500" />}
                                                        <tab.icon size={17} strokeWidth={selected ? 2.2 : 1.8} className={selected ? 'text-indigo-500' : 'text-slate-400 group-hover:text-slate-600'} />
                                                        <span>{tab.label}</span>
                                                        {tab.id === 'manage' && isRuntimeActive && (
                                                            <span className="ml-auto h-1.5 w-1.5 rounded-full bg-emerald-500 shadow-[0_0_0_3px_rgba(16,185,129,0.12)]" />
                                                        )}
                                                    </button>
                                                );
                                            })}
                                        </div>
                                    </div>
                                );
                            })}
                        </nav>

                        <div className="mx-3 mb-4 rounded-[22px] border border-white/80 bg-[#f6f8fa] p-5 shadow-sm">
                            <div className="flex items-center justify-between">
                                <span className="text-[12px] font-bold text-slate-500">SIMULATION</span>
                                <span className={`h-1.5 w-1.5 rounded-full ${status ? 'bg-emerald-500' : 'bg-slate-300'}`} />
                            </div>
                            <div className="mt-1.5 truncate font-mono text-[12px] text-slate-400" title={userId}>{userId}</div>
                            <div className="mt-2 flex items-center gap-1.5 text-[12px] font-medium text-slate-400">
                                <span className="rounded bg-slate-100 px-1.5 py-0.5">A 股</span>
                                <span>本地安全沙箱</span>
                            </div>
                        </div>
                    </TradingSidebar>

                    {/* 右侧内容区：带细点网格底纹 */}
                    <TradingMain>
                        {activeTab === 'manage' && (
                            <TopologyConsole
                                tenantId={tenantId}
                                userId={userId}
                                tradingMode={tradingMode}
                                onDeploy={handleDeploy}
                                onStop={handleStop}
                                onOpenManualTask={() => setActiveTab('manual-task')}
                                onOpenHistory={() => setActiveTab('history')}
                            />
                        )}
                        {activeTab === 'manual-task' && (
                            <ManualTaskPage tenantId={tenantId} userId={userId} tradingMode={tradingMode} onBack={() => setActiveTab('manage')} />
                        )}
                        {activeTab === 'personal' && (
                            <PersonalCenter
                                tenantId={tenantId}
                                userId={userId}
                                status={status}
                                tradingMode={tradingMode}
                            />
                        )}
                        {activeTab === 'position' && (
                            <PositionMonitor
                                userId={userId}
                                isActive={activeTab === 'position'}
                                accountInfo={accountInfo}
                            />
                        )}
                        {activeTab === 'history' && (
                            <TradingHistory
                                userId={userId}
                                isActive={activeTab === 'history'}
                                tradingMode={tradingMode}
                            />
                        )}
                        {activeTab === 'risk' && <RiskControlTab tenantId={tenantId} />}
                        {activeTab === 'settings' && <SettingsCenter userId={userId} isActive={activeTab === 'settings'} />}
                        {activeTab === 'replay' && <ReplayPage />}
                    </TradingMain>
                </div>

            <Modal
                title={
                    preflightStage === 'trading-readiness'
                        ? (preflightMode === 'SIMULATION' ? '模拟盘启动自检' : '交易准备度检测')
                        : (preflightMode === 'SIMULATION' ? '模拟盘启动自检' : '启动前自检详情')
                }
                open={preflightModalOpen}
                onCancel={closePreflightModal}
                centered
                footer={[
                    <Button
                        key="close"
                        onClick={closePreflightModal}
                        disabled={confirmStarting}
                    >
                        关闭
                    </Button>,
                    ...(preflightStage === 'preflight' && preflightResult?.ready && pendingDeploy
                        ? [
                            <Button
                                key="confirm-start"
                                type="primary"
                                loading={confirmStarting}
                                onClick={async () => {
                                    const current = pendingDeploy;
                                    setConfirmStarting(true);
                                    const ok = await executeDeploy(
                                        current.strategyId,
                                        current.mode,
                                        current.executionConfig,
                                        current.liveTradeConfig,
                                    );
                                    setConfirmStarting(false);
                                    if (ok) {
                                        closePreflightModal();
                                    }
                                }}
                            >
                                {confirmStartLabel}
                            </Button>,
                        ]
                        : []),
                ]}
                width={760}
                styles={{
                    body: { maxHeight: '70vh', overflowY: 'auto' },
                }}
            >
                {preflightLoading ? (
                    <div className="space-y-3">
                        <div className="text-sm text-gray-600">
                            模式：<span className="font-mono">{preflightMode || '-'}</span>，
                            结论：<Tag color="processing" className="ml-2">检测中</Tag>
                        </div>
                        <div className="rounded-lg border border-gray-200 bg-gray-50 px-4 py-3">
                            <Spin size="small" />
                            <span className="ml-2 text-sm text-gray-600">
                                {preflightMode === 'SIMULATION'
                                    ? '正在检查数据库、默认模型、进程池、行情与信号...'
                                    : preflightStage === 'trading-readiness'
                                    ? '正在逐项检查交易准备度...'
                                    : '交易准备度已通过，正在逐项检查启动条件...'}
                            </span>
                        </div>
                    </div>
                ) : preflightLoadError ? (
                    <div className="space-y-3">
                        <div className="text-sm text-gray-600">
                            模式：<span className="font-mono">{preflightMode || '-'}</span>，
                            结论：<Tag color="error" className="ml-2">检测失败</Tag>
                        </div>
                        <div className="rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-600">
                            {preflightLoadError}
                        </div>
                    </div>
                ) : preflightStage === 'trading-readiness' && tradingReadinessResult ? (
                    <div className="space-y-3">
                        <div className="text-sm text-gray-600">
                            模式：<span className="font-mono">{preflightMode || '-'}</span>，
                            结论：
                            <Tag color={tradingReadinessResult.passed ? 'success' : 'error'} className="ml-2">
                                {tradingReadinessResult.passed ? '可继续启动' : '不可启动'}
                            </Tag>
                            {permissionTag(tradingReadinessResult.trading_permission)}
                        </div>
                        {tradingReadinessResult.trading_permission === 'observe_only' && (
                            <div className="rounded-md border border-blue-200 bg-blue-50 p-3 text-sm text-blue-700">
                                {tradingReadinessResult.signal_readiness?.message || '当前缺少可交易信号，本次启动将只运行观察链路，不会自动下单。'}
                            </div>
                        )}
                        <div className="space-y-2">
                            {visiblePreflightChecks.slice(0, revealedItemCount || visiblePreflightChecks.length).map((item) => (
                                <Collapse
                                    key={item.key}
                                    size="small"
                                    items={[{
                                        key: item.key,
                                        label: (
                                            <div className="flex items-center gap-2">
                                                <span className="text-sm font-medium">{item.label}</span>
                                                <Tag color={(() => {
                                                    if (!item.ok) return 'error';
                                                    if (preflightMode === 'SIMULATION' && (
                                                        item.message?.includes('观察态') || 
                                                        item.message?.includes('observe_only') ||
                                                        item.details?.trading_permission === 'observe_only'
                                                    )) return 'warning';
                                                    return 'success';
                                                })()}>
                                                    {(() => {
                                                        if (!item.ok) return '阻断';
                                                        if (preflightMode === 'SIMULATION' && (
                                                            item.message?.includes('观察态') || 
                                                            item.message?.includes('observe_only') ||
                                                            item.details?.trading_permission === 'observe_only'
                                                        )) return '警告';
                                                        return '通过';
                                                    })()}
                                                </Tag>
                                            </div>
                                        ),
                                        children: (
                                            <div className="space-y-2">
                                                <div className="text-sm text-gray-600">{item.message}</div>
                                                {item.details && Object.keys(item.details).length > 0 && (
                                                    <div className="rounded-md border border-gray-200 bg-gray-50 p-2">
                                                        {Object.entries(item.details).map(([k, v]) => (
                                                            <div key={k} className="text-xs text-gray-500 break-all">
                                                                <span className="font-mono text-gray-700">{k}</span>: {typeof v === 'object' ? JSON.stringify(v) : String(v)}
                                                            </div>
                                                        ))}
                                                    </div>
                                                )}
                                            </div>
                                        ),
                                    }]}
                                />
                            ))}
                            {isRevealing && revealedItemCount < visiblePreflightChecks.length && (
                                <div className="rounded-lg border border-gray-200 bg-gray-50 px-4 py-3">
                                    <Spin size="small" />
                                    <span className="ml-2 text-sm text-gray-500">正在逐一确认检测项...</span>
                                </div>
                            )}
                        </div>
                    </div>
                ) : preflightResult ? (
                    <div className="space-y-3">
                        <div className="text-sm text-gray-600">
                            模式：<span className="font-mono">{preflightResult.mode}</span>，
                            结论：
                            <Tag color={preflightResult.ready ? 'success' : 'error'} className="ml-2">
                                {preflightResult.ready ? '可启动' : '不可启动'}
                            </Tag>
                            {permissionTag(preflightResult.trading_permission)}
                        </div>
                        {preflightResult.trading_permission === 'observe_only' && (
                            <div className="rounded-md border border-blue-200 bg-blue-50 p-3 text-sm text-blue-700">
                                {preflightResult.signal_readiness?.message || '当前缺少可交易信号，确认启动后将进入观察态，不会自动下单。'}
                            </div>
                        )}
                        {preflightResult.ready && pendingDeploy && (
                            <div className="rounded-md border border-blue-200 bg-blue-50 p-3 text-sm text-blue-700">
                                全部检测项已通过，请在底部点击确认启动。
                            </div>
                        )}
                        <div className="space-y-2">
                            {visiblePreflightChecks.slice(0, revealedItemCount || visiblePreflightChecks.length).map((item) => (
                                <Collapse
                                    key={item.key}
                                    size="small"
                                    items={[{
                                        key: item.key,
                                        label: (
                                            <div className="flex items-center gap-2">
                                                <span className="text-sm">{item.label}</span>
                                                <Tag color={(() => {
                                                    if (!item.ok) return item.required ? 'error' : 'warning';
                                                    if (preflightMode === 'SIMULATION' && (
                                                        item.message?.includes('观察态') || 
                                                        item.message?.includes('observe_only') ||
                                                        item.details?.trading_permission === 'observe_only'
                                                    )) return 'warning';
                                                    return 'success';
                                                })()}>
                                                    {(() => {
                                                        if (!item.ok) return item.required ? '阻断' : '警告';
                                                        if (preflightMode === 'SIMULATION' && (
                                                            item.message?.includes('观察态') || 
                                                            item.message?.includes('observe_only') ||
                                                            item.details?.trading_permission === 'observe_only'
                                                        )) return '警告';
                                                        return '通过';
                                                    })()}
                                                </Tag>
                                            </div>
                                        ),
                                        children: (
                                            <div className="space-y-2">
                                                <div className="text-sm text-gray-600">{item.message}</div>
                                                {item.details && Object.keys(item.details).length > 0 && (
                                                    <div className="rounded-md border border-gray-200 bg-gray-50 p-2">
                                                        {Object.entries(item.details).map(([k, v]) => (
                                                            <div key={k} className="text-xs text-gray-500 break-all">
                                                                <span className="font-mono text-gray-700">{k}</span>: {typeof v === 'object' ? JSON.stringify(v) : String(v)}
                                                            </div>
                                                        ))}
                                                    </div>
                                                )}
                                            </div>
                                        ),
                                    }]}
                                />
                            ))}
                            {isRevealing && revealedItemCount < visiblePreflightChecks.length && (
                                <div className="rounded-lg border border-gray-200 bg-gray-50 px-4 py-3">
                                    <Spin size="small" />
                                    <span className="ml-2 text-sm text-gray-500">正在逐一确认检测项...</span>
                                </div>
                            )}
                        </div>
                    </div>
                ) : (
                    <div className="text-sm text-gray-500">暂无自检结果</div>
                )}
            </Modal>
            <LiveTradeConfigWizard
                open={wizardOpen}
                mode={wizardMode}
                strategyId={wizardStrategy?.id || ''}
                strategyName={wizardStrategy?.name || ''}
                strategyDefaults={wizardStrategy ? {
                    execution_defaults: wizardStrategy.execution_defaults || wizardStrategy.execution_config || undefined,
                    live_defaults: wizardStrategy.live_defaults || wizardStrategy.live_trade_config || undefined,
                    live_config_tips: wizardStrategy.live_config_tips || [],
                } : null}
                initialExecutionConfig={effectiveExecutionConfig || undefined}
                initialLiveTradeConfig={effectiveLiveTradeConfig || undefined}
                onCancel={() => setWizardOpen(false)}
                onConfirm={handleWizardConfirm}
            />
            </div>
        </TradingShell>
    );
};

export default RealTradingPage;
