import React, { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import { LayoutDashboard, PieChart, FileText, Settings, User, HelpCircle, Settings2, Check, ClipboardList } from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import { Button, Collapse, Modal, Spin, Tag, message } from 'antd';
import TopBar from './components/TopBar';
import StrategyManagement from './tabs/StrategyManagement';
import ManualTaskPage from './tabs/ManualTaskPage';
import PersonalCenter from './tabs/PersonalCenter';
import PositionMonitor from './tabs/PositionMonitor';
import TradingHistory from './tabs/TradingHistory';
import SettingsCenter from './tabs/SettingsCenter';
import { realTradingService, RealTradingStatus, AccountInfo, PreflightCheckResponse } from '../../services/realTradingService';
import { strategyManagementService } from '../../services/strategyManagementService';
import { authService } from '../../features/auth/services/authService';
import type { PreflightCheckItem } from '../../services/realTradingService';
import type { StrategyFile } from '../../types/backtest/strategy';
import { useAppDispatch, useAppSelector } from '../../store';
import { setTradingMode } from '../../store/slices/uiSlice';
import { useTradeWebSocket } from '../../hooks/useTradeWebSocket';
import { buildTradingTopBarAccountInfo, resolveTradingAccountMode } from './utils/accountAdapter';
import LiveTradeConfigWizard from './components/LiveTradeConfigWizard';
import type { DeployMode, ExecutionConfig, LiveTradeConfig } from '../../types/liveTrading';

type TradingMode = 'real' | 'simulation';
type ActiveTab = 'manage' | 'manual-task' | 'personal' | 'position' | 'history' | 'settings';
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
const TRADING_MODE_PREF_KEY = 'qm:trading_mode_pref';

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

const RealTradingPage: React.FC = () => {
    const [activeTab, setActiveTab] = useState<ActiveTab>('manage');
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
    const dispatch = useAppDispatch();
    const tradingMode = useAppSelector((state) => state.ui.tradingMode);
    const [status, setStatus] = useState<RealTradingStatus | null>(null);
    const [accountInfo, setAccountInfo] = useState<AccountInfo | null>(null);
    const [showModeSettings, setShowModeSettings] = useState(false);
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
    const [wizardMode, setWizardMode] = useState<DeployMode>('REAL');
    const [confirmStarting, setConfirmStarting] = useState(false);
    const [revealedItemCount, setRevealedItemCount] = useState(0);
    const [isRevealing, setIsRevealing] = useState(false);
    const preflightRequestSeqRef = useRef(0);
    const isFetchingRef = useRef(false);

    useEffect(() => {
        const remembered = String(localStorage.getItem(TRADING_MODE_PREF_KEY) || '').trim();
        if (remembered === 'real' || remembered === 'simulation') {
            dispatch(setTradingMode(remembered));
        }
    }, [dispatch]);

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
            const statusData = await realTradingService.getStatus(userId, tradingMode, tenantId);
            const runtimeMode = resolveTradingAccountMode(statusData?.mode, tradingMode);
            const accountData = await realTradingService.getRuntimeAccount(userId, tenantId, runtimeMode).catch(() => null);

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
    const resolvedRunMode: 'REAL' | 'SHADOW' | 'SIMULATION' | undefined = isRuntimeActive
        ? (status?.mode || (tradingMode === 'simulation' ? 'SIMULATION' : 'REAL'))
        : undefined;
    const resolvedOrchestrationMode: 'docker' | 'k8s' | undefined = isRuntimeActive
        ? status?.orchestration_mode
        : undefined;
    const recommendedMode: TradingMode = resolveTradingAccountMode(status?.mode, tradingMode);

    const executeDeploy = useCallback(async (
        strategyId: string,
        mode: DeployMode,
        executionConfig: ExecutionConfig,
        liveTradeConfig: LiveTradeConfig,
    ): Promise<boolean> => {
        try {
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

            const modeText = mode === 'REAL' ? '实盘' : (mode === 'SHADOW' ? '影子' : '模拟');
            const permissionText = startResp?.trading_permission === 'observe_only'
                ? '（观察态，不自动下单）'
                : '';
            message.success(`${modeText}部署请求已提交${permissionText}`);
            fetchData();
            return true;
        } catch (err: unknown) {
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
        const mode: DeployMode = tradingMode === 'simulation' ? 'SIMULATION' : (isShadow ? 'SHADOW' : 'REAL');
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
            const tradingReadiness = await realTradingService.getTradingPrecheck(mode);
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

            setPreflightStage('preflight');
            setPreflightLoading(true);
            const preflight = await realTradingService.preflight(mode, userId, tenantId);
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
        if (pendingDeploy.mode === 'SIMULATION') return '确认并启动模拟盘';
        if (pendingDeploy.mode === 'SHADOW') return '确认并启动影子运行';
        return '确认并启动实盘';
    }, [pendingDeploy]);

    // 逐项展示检测结果：API 返回后逐个 reveal，而非一次性全部渲染
    useEffect(() => {
        const items = preflightResult?.checks || tradingReadinessResult?.items || [];
        if (items.length === 0) return;

        setRevealedItemCount(1);
        setIsRevealing(true);

        let count = 1;
        const timer = setInterval(() => {
            count++;
            setRevealedItemCount(count);
            if (count >= items.length) {
                clearInterval(timer);
                setIsRevealing(false);
            }
        }, 350);

        return () => {
            clearInterval(timer);
            setIsRevealing(false);
        };
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
            await realTradingService.stop(userId, tenantId);

            // 10万并发架构核心：从 Redis 匹配池移除策略
            if (currentStrategyId) {
                try {
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

    const handleModeSwitch = (mode: TradingMode): void => {
        localStorage.setItem(TRADING_MODE_PREF_KEY, mode);
        dispatch(setTradingMode(mode));
        setShowModeSettings(false);
    };

    const tabs: Array<{ id: ActiveTab; label: string; icon: LucideIcon }> = [
        { id: 'manage', label: '策略管理', icon: LayoutDashboard },
        { id: 'manual-task', label: '手动任务', icon: ClipboardList },
        { id: 'personal', label: '个人中心', icon: User },
        { id: 'position', label: '持仓监控', icon: PieChart },
        { id: 'history', label: '交易记录', icon: FileText },
        { id: 'settings', label: '设置', icon: Settings },
    ];

    return (
        <div className="flex flex-col h-full bg-[#f8fafc] p-6 gap-6 font-sans">
            {/* Top Section - Account Overview (38% 黄金分割) */}
            <div className="h-[38%] bg-white rounded-[32px] shadow-sm border border-gray-100 overflow-hidden">
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

            {/* Bottom Section - Sidebar & Content (62% 黄金分割) */}
            <div className="h-[62%] flex bg-white rounded-[32px] shadow-sm border border-gray-100 overflow-hidden">
                {/* Left Sidebar - Navigation */}
                <div className="w-[240px] flex flex-col border-r border-gray-100 bg-gray-50/30">
                    <div className="flex-1 overflow-y-auto py-4 px-3 space-y-1 custom-scrollbar">
                        <div className="px-3 mb-2">
                            <span className="text-xs font-semibold text-gray-400 uppercase tracking-wider">功能导航</span>
                        </div>
                        {tabs.map(tab => (
                            <button
                                key={tab.id}
                                onClick={() => setActiveTab(tab.id)}
                                className={`w-full flex items-center gap-3 px-4 py-3 rounded-xl text-sm font-medium transition-all duration-200
                                    ${activeTab === tab.id
                                        ? 'bg-white text-blue-600 border border-blue-100'
                                        : 'text-gray-600 hover:text-gray-900 hover:bg-gray-100/50'
                                    }
                                `}
                            >
                                <tab.icon size={18} className={activeTab === tab.id ? 'text-blue-500' : 'text-gray-400'} />
                                {tab.label}
                            </button>
                        ))}
                    </div>

                    {/* Bottom Help Center + Mode Switch */}
                    <div className="p-4 border-t border-gray-200 relative">
                        <div className="flex items-center gap-2">
                            <a
                                href="https://www.quantmindai.cn/help"
                                target="_blank"
                                rel="noopener noreferrer"
                                className="flex-1 flex items-center gap-3 px-4 py-3 rounded-2xl text-gray-600 hover:text-blue-600 hover:bg-blue-50 transition-colors"
                            >
                                <HelpCircle className="w-5 h-5" />
                                <span className="text-sm">帮助中心</span>
                            </a>
                            <button
                                onClick={() => setShowModeSettings(!showModeSettings)}
                                className="p-3 rounded-2xl text-gray-600 hover:text-blue-600 hover:bg-blue-50 transition-colors"
                                title="交易模式设置"
                            >
                                <Settings2 className="w-5 h-5" />
                            </button>
                        </div>

                        {showModeSettings && (
                            <div className="absolute bottom-[72px] left-4 right-4 rounded-2xl border border-gray-200 bg-white shadow-xl p-3 z-20">
                                <div className="text-xs text-gray-500 mb-2">
                                    智能推荐: {recommendedMode === 'real' ? '实盘' : '模拟盘'}
                                </div>
                                <div className="grid grid-cols-2 gap-2">
                                    <button
                                        onClick={() => handleModeSwitch('real')}
                                        className={`flex items-center justify-center gap-1 px-3 py-2 rounded-xl text-sm font-medium border transition-colors ${tradingMode === 'real'
                                            ? 'border-blue-500 bg-blue-50 text-blue-600'
                                            : 'border-gray-200 text-gray-600 hover:bg-gray-50'
                                            }`}
                                    >
                                        {tradingMode === 'real' && <Check size={14} />}
                                        实盘
                                    </button>
                                    <button
                                        onClick={() => handleModeSwitch('simulation')}
                                        className={`flex items-center justify-center gap-1 px-3 py-2 rounded-xl text-sm font-medium border transition-colors ${tradingMode === 'simulation'
                                            ? 'border-blue-500 bg-blue-50 text-blue-600'
                                            : 'border-gray-200 text-gray-600 hover:bg-gray-50'
                                            }`}
                                    >
                                        {tradingMode === 'simulation' && <Check size={14} />}
                                        模拟盘
                                    </button>
                                </div>
                                <div className="mt-2 text-[11px] text-gray-400">
                                    实盘与模拟盘互斥，仅可二选一
                                </div>
                            </div>
                        )}
                    </div>
                </div>

                {/* Right Content Area */}
                <div className="flex-1 overflow-hidden relative bg-white">
                    {activeTab === 'manage' && (
                        <StrategyManagement
                            tenantId={tenantId}
                            userId={userId}
                            tradingMode={tradingMode}
                            status={status}
                            onDeploy={handleDeploy}
                            onStop={handleStop}
                            onOpenManualTask={() => setActiveTab('manual-task')}
                            isRunning={strategyStatus === 'running' || strategyStatus === 'starting'}
                            activeExecutionConfig={effectiveExecutionConfig}
                            activeLiveTradeConfig={effectiveLiveTradeConfig}
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
                    {activeTab === 'settings' && <SettingsCenter userId={userId} isActive={activeTab === 'settings'} />}
                </div>
            </div>

            <Modal
                title={preflightStage === 'trading-readiness' ? '交易准备度检测' : '启动前自检详情'}
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
                                {preflightStage === 'trading-readiness'
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
                                                <Tag color={item.ok ? 'success' : 'error'}>
                                                    {item.ok ? '通过' : '阻断'}
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
                                                <Tag color={item.ok ? 'success' : (item.required ? 'error' : 'warning')}>
                                                    {item.ok ? '通过' : (item.required ? '阻断' : '警告')}
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
    );
};

export default RealTradingPage;
