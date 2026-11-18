import React, { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import HelpCenterLink from '../../components/common/HelpCenterLink';
import { LIVE_NODE_ONLY } from '../../config/liveNodeFlags';
import { Button, Collapse, Modal, Spin, Tag, message } from 'antd';
import TopBar from './components/TopBar';
import TopologyConsole from './tabs/StrategyConsole/TopologyConsole';
import ManualTaskPage from './tabs/ManualTaskPage';
import PersonalCenter from './tabs/PersonalCenter';
import PositionMonitor from './tabs/PositionMonitor';
import TradingHistory from './tabs/TradingHistory';
import SettingsCenter, { type SettingsExtraPanel } from './tabs/SettingsCenter';
import ReplayPage from './tabs/ReplayPage';
import DeskTodayPage from '../../features/desk/DeskTodayPage';
import SignalsExplorerPage from './tabs/SignalsExplorerPage';
import type { RealTradingStatus, AccountInfo, PreflightCheckResponse, PreflightCheckItem } from '../../services/realTradingService';
import type { StartGateFailure } from '../../services/realTradingService';
import {
    getPreferredAccountSource,
    subscribeAccountSourceChange,
} from './utils/accountSourcePreference';
import { authService } from '../../features/auth/services/authService';
import type { StrategyFile } from '../../types/backtest/strategy';
import { isLiveTradingEnabled } from '../../config/tradingFlags';
import { useAppSelector } from '../../store';
import { selectCurrentMarket } from '../../store/slices/uiSlice';
import { useTradingModeSwitch } from '../../features/shared/useTradingModeSwitch';
import { useTradeWebSocket } from '../../hooks/useTradeWebSocket';
import { buildTradingTopBarAccountInfo, resolveTradingAccountMode } from './utils/accountAdapter';
import { useSearchParams } from 'react-router-dom';
import { DEFAULT_ACTIVE_TAB, isDeepLinkableTab, resolveInitialSymbol, resolveInitialTab, type ActiveTab } from './utils/activeTab';
import {
    composeConsoleTabs,
    resolveConsoleTab,
    resolveConsoleTradingMode,
    type ConsoleTab,
} from './utils/consoleTabs';
import LiveTradeConfigWizard from './components/LiveTradeConfigWizard';
import StartGateFailedModal from './components/StartGateFailedModal';
import type { DeployMode, ExecutionConfig, LiveTradeConfig } from '../../types/liveTrading';

/** 支持实盘(通达信桥)与模拟盘。口径见 `utils/consoleTabs.ts::ConsoleTradingMode`。 */
type TradingMode = 'real' | 'simulation';
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


// 实盘通道文案按市场：CN=通达信桥/大QMT执行端，HK=富途/老虎/IB，US=老虎/IB/富途
const BROKER_LABELS: Record<string, string> = {
  CN: '通达信/大QMT',
  HK: '富途/老虎/IB',
  US: '老虎/IB/富途',
  FUTURES: 'IB',
  CRYPTO: '暂无',
};

/**
 * 追加页签渲染时拿得到的运行期上下文。
 *
 * 值全部由本页提供（不额外发请求）：追加页签与基础页签看到的是**同一份**账户、
 * 同一个刷新入口，所以两边的数字不可能有相位差。
 */
export interface RealTradingTabContext {
    userId: string;
    tenantId: string;
    /** 当前市场（CN/HK/US/FUTURES/CRYPTO），追加页签据此判可用性 */
    market: string;
    status: RealTradingStatus | null;
    accountInfo: AccountInfo | null;
    /** 触发一次账户/状态重取，与页内 5s 轮询同一个入口 */
    refresh: () => void;
}

/** 追加到侧栏末尾的页签；`render` 每次渲染都会被调用，返回该栏内容。 */
export interface RealTradingExtraTab extends ConsoleTab {
    render: (ctx: RealTradingTabContext) => React.ReactNode;
    /**
     * 切走后**只隐藏不卸载**（缺省 false ＝切走即卸载，与基础栏同规矩）。
     *
     * 给内嵌长连接的栏用：iframe 里的 SPA 靠自己的 WebSocket 推流，卸了再挂回来
     * 等于每次切栏都断一次流、重连一次。代价是**开页即挂载**（不等第一次点击），
     * 所以只有确实要保流的栏才开；普通面板留缺省，草稿/轮询不留在后台空转。
     */
    keepMounted?: boolean;
}

export interface RealTradingPageProps {
    /**
     * 固定交易模式：不挂「模拟/实盘」开关、不弹切换确认，恒按给定模式取数与部署。
     * 缺省（不传）= 跟随全局模式，即公开发行版的既有行为。
     */
    forcedTradingMode?: TradingMode;
    /**
     * 顶栏右侧状态徽章那一行的**附加节点**。缺省不渲染。
     *
     * 与 `extraTabs` 同一约定：**公开树不感知调用方是谁** —— 无调用方时 prop 为
     * undefined，`{undefined}` 不产生任何 DOM，公开仓形态与本机制引入前逐位相同。
     * 本机「实盘交易」栏用它把市场切换器（A股/港股/美股，`components/layout/MarketSelector`）
     * 挂到顶栏：这一栏经常停在一个市场上看账户，切市场不该先退回主页再切回来。
     * 收**节点**而非渲染函数：切换器读写全局 redux 状态，不需要 `ctx`。
     */
    topBarExtras?: React.ReactNode;
    /**
     * 追加到侧栏末尾的页签。缺省不追加。
     *
     * 本机独有「实盘交易」栏目（`features/local-live/`，不入库）用它把实盘专属面板
     * 挂到同一个控制台上 —— **公开树不感知调用方是谁**：无调用方时这个 prop 为
     * undefined，追加分支整段不参与渲染，公开仓形态与本机制引入前逐位相同。
     */
    extraTabs?: readonly RealTradingExtraTab[];
    /**
     * 顶栏下方的横幅槽位，拿到与追加页签同一份运行期上下文。缺省不渲染。
     *
     * 与 `extraTabs` 同一约定：**公开树不感知调用方是谁** —— 无调用方时整个
     * 分支不参与渲染，公开仓形态与本机制引入前逐位相同。用途是「账户/交易端
     * 不可用」这类**跨页签**的状态提示：它不是某一栏的内容，挂进任何一栏都只能
     * 在那一栏被看见。
     */
    banner?: (ctx: RealTradingTabContext) => React.ReactNode;
    /**
     * 追加到「设置」页顶部按钮条的面板。缺省不追加。
     *
     * 与 `extraTabs` 同一约定：**公开树不感知调用方是谁** —— 无调用方时 prop 为
     * undefined，设置页与本机制引入前逐位相同。用途是「属于设置范畴、但只有本机
     * 实盘栏才有」的配置面（本机把 arena 的总控/数据嵌在设置里，用户口径
     * 「总控放设置里面、数据也放设置里面」，不在侧栏另起入口）。
     */
    settingsPanels?: readonly SettingsExtraPanel[];
    /**
     * 追加到「手动任务」栏向导下方的区块，拿到与追加页签同一份运行期上下文。缺省不渲染。
     *
     * 与 `extraTabs` 同一约定：**公开树不感知调用方是谁** —— 无调用方时整个分支不参与
     * 渲染，公开仓形态与本机制引入前逐位相同。用途是「属于手动任务范畴、但只有本机
     * 实盘栏才有」的面板（本机把「推送下单」并进手动任务，用户 2026-09-23 口径
     * 「推送下单放手动任务」，不在侧栏另起入口）。
     */
    manualTaskExtras?: (ctx: RealTradingTabContext) => React.ReactNode;
    /**
     * 追加到「持仓监控」栏右栏（持仓风险与情报）的区块，拿到与追加页签同一份运行期
     * 上下文。缺省不渲染。
     *
     * 与 `extraTabs` 同一约定：**公开树不感知调用方是谁** —— 无调用方时整个分支不参与
     * 渲染，公开仓形态与本机制引入前逐位相同。用途同上（本机把「风控止损」并进持仓
     * 监控，用户 2026-09-23 口径「风控止损放持仓监控那」）。
     */
    positionRailExtras?: (ctx: RealTradingTabContext) => React.ReactNode;
}

const RealTradingPage: React.FC<RealTradingPageProps> = ({ forcedTradingMode, topBarExtras, extraTabs, banner, settingsPanels, manualTaskExtras, positionRailExtras }) => {
    const currentMarket = useAppSelector(selectCurrentMarket);
    // 默认「系统健康」，深链 ?tab=signals|position 直达 —— 规则见 utils/activeTab.ts（有测试锁定）
    const initialTab: ActiveTab = resolveInitialTab(
        typeof window === 'undefined' ? null : window.location.hash,
    );
    // 深链带标的（哨兵「去持仓」）：惰性解析一次，落到持仓监控高亮那行
    const [initialSymbol] = useState<string | null>(() =>
        typeof window === 'undefined' ? null : resolveInitialSymbol(window.location.hash),
    );
    // 取值域含追加页签 id，故为 string；基础 8 栏的 id 仍由 consoleTabs 钉在 ActiveTab 上
    const [activeTab, setActiveTab] = useState<string>(initialTab);

    // 页签 ↔ URL 双向同步（replace，不留历史栈）：
    // - 切页签写 URL：刷新/转发能停在原栏（此前 URL 永远停在进入时那一栏）；
    // - URL 变化读页签：**页内**深链（如候选信号页里的持仓预警 navigate 到 ?tab=position）
    //   不重挂载本组件，useState 初值不会重算 —— 缺这条监听时「页内点跳转无效」。
    // 白名单与挂载口径一致（utils/activeTab.isDeepLinkableTab，有测试锁定）。
    const [searchParams, setSearchParams] = useSearchParams();
    const switchTab = useCallback(
        (t: string) => {
            setActiveTab(t);
            // 切栏只保留 tab 一个参数：残留的 symbol/object 等深链参数会把下一次
            // 进栏拽到旧对象上（评估中心迁走后 object 参数已不由本页消费，同样清掉）。
            setSearchParams(t === DEFAULT_ACTIVE_TAB ? {} : { tab: t }, { replace: true });
        },
        [setSearchParams],
    );

    // 依赖里**只放 searchParams**（同 FactorResearchPage 的口径注释）：switchTab 是
    // 「先改 state、后写 URL」，中间那版 searchParams 还是旧值，effect 若跟着 activeTab
    // 重跑就会把刚切过去的页签拽回去。
    useEffect(() => {
        const fromUrl = searchParams.get('tab');
        if (isDeepLinkableTab(fromUrl)) setActiveTab(fromUrl);
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [searchParams]);

    // 券商通道卡「去配置凭证」跳转：切到设置页签
    useEffect(() => {
        const handler = () => switchTab('settings');
        window.addEventListener('goto-trading-settings', handler);
        return () => window.removeEventListener('goto-trading-settings', handler);
    }, [switchTab]);
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
    // T-FE-18：交易模式切换统一入口（切实盘前置二次确认，与顶栏同源）
    const modeSwitch = useTradingModeSwitch();
    // 固定模式（本机「实盘交易」栏目）优先，未固定时跟随全局。归一规则与理由
    // 见 `utils/consoleTabs.ts::resolveConsoleTradingMode`。
    const tradingMode: TradingMode = resolveConsoleTradingMode(
        forcedTradingMode,
        modeSwitch.tradingMode,
    );
    const { requestSwitch } = modeSwitch;
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
    // 启动门禁拒绝（生命周期 / 晋级体检）：常驻弹窗，非 toast —— 见 StartGateFailedModal 抬头注释。
    const [startGateFailure, setStartGateFailure] = useState<StartGateFailure | null>(null);
    const [wizardOpen, setWizardOpen] = useState(false);
    const [wizardStrategy, setWizardStrategy] = useState<StrategyFile | null>(null);
    const [wizardMode, setWizardMode] = useState<DeployMode>('REAL');
    const [confirmStarting, setConfirmStarting] = useState(false);
    const [revealedItemCount, setRevealedItemCount] = useState(0);
    const [isRevealing, setIsRevealing] = useState(false);
    const preflightRequestSeqRef = useRef(0);
    const isFetchingRef = useRef(false);
    // 被「已有请求在飞」挡掉的重取（切源/切市场/成交事件）：等当前请求落地立刻补一次，
    // 否则点了持仓监控的账户源 chip 要等下一个 5s 周期才看到数字变（实测 ~7s）。
    const pendingRefetchRef = useRef(false);
    const fetchDataRef = useRef<(() => void) | null>(null);
    // 账户「按源看」偏好（持仓监控 chips 点选）：多券商的 QMT / 通达信是两个真实账户，
    // 不指定源时后端按「最新一行」取，数字会在两个账户之间跳。null = 跟随交易券商。
    const [accountSource, setAccountSource] = useState<string | null>(() => getPreferredAccountSource(currentMarket));

    useEffect(() => {
        setAccountSource(getPreferredAccountSource(currentMarket));
        return subscribeAccountSourceChange(() => {
            setAccountSource(getPreferredAccountSource(currentMarket));
        });
    }, [currentMarket]);

    const fetchData = useCallback(async () => {
        if (isFetchingRef.current) {
            pendingRefetchRef.current = true;
            return;
        }

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
            const accountData = await realTradingService.getRuntimeAccount(userId, tenantId, runtimeMode, currentMarket, accountSource).catch(() => null);

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
            if (pendingRefetchRef.current) {
                pendingRefetchRef.current = false;
                fetchDataRef.current?.(); // 用最新闭包补跑（含刚切到的 source）
            }
        }
        // currentMarket 必须在内：顶栏账户随市场切换（getRuntimeAccount 带 market），
        // 漏了它 → 切港股/美股后闭包仍是旧市场，页面继续显示 A 股账户。
        // accountSource 同理：切源要立刻重取，否则点了 chip 还得等下一个 5s 周期。
    }, [tenantId, userId, tradingMode, currentMarket, accountSource]);

    // 保持「最新一次渲染的 fetchData」可被异步补跑调用（见上面的 pendingRefetchRef）
    useEffect(() => {
        fetchDataRef.current = fetchData;
    }, [fetchData]);

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
        ? (tradingMode === 'real' ? 'REAL' : 'SIMULATION')
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

            const modeText = tradingMode === 'real' ? `实盘(${BROKER_LABELS[currentMarket] || '券商'})` : '模拟盘';
            const permissionText = startResp?.trading_permission === 'observe_only'
                ? '（观察态，不自动下单）'
                : '';
            message.success(`${modeText}部署请求已提交${permissionText}`);
            fetchData();
            return true;
        } catch (err: unknown) {
            const { realTradingService } = await import('../../services/realTradingService');
            // 启动门禁（生命周期 / 晋级体检）：常驻弹窗，不弹 toast —— 理由见 StartGateFailedModal。
            // 与下面的「交易准备度」自检是两个闸门，信封形状不同，先认门禁。
            const gateFailure = realTradingService.extractStartGateFailure(err);
            if (gateFailure) {
                setStartGateFailure(gateFailure);
                return false;
            }
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
        const mode: DeployMode = tradingMode === 'real' ? 'REAL' : 'SIMULATION';
        setWizardStrategy(strategy || { id: strategyId, name: strategyId, source: 'personal', code: '' });
        setWizardMode(mode);
        setWizardOpen(true);
    };

    const handleModeSwitch = useCallback((mode: TradingMode) => {
        requestSwitch(mode);
    }, [requestSwitch]);

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
        return tradingMode === 'real' ? `确认并启动实盘(${BROKER_LABELS[currentMarket] || '券商'})` : '确认并启动模拟盘';
    }, [pendingDeploy, tradingMode]);

    // 检测结果全部展示，不做逐项 reveal（加快加载速度）
    useEffect(() => {
        const items = preflightResult?.checks || tradingReadinessResult?.items || [];
        if (items.length > 0) {
            setRevealedItemCount(items.length);
            setIsRevealing(false);
        }
    }, [preflightResult, tradingReadinessResult]);

    const handleStop = async (reason?: string) => {
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
            // 停止原因随请求带给后端，落审计与运行日志（T-RC-19）
            await realTradingService.stop(userId, tenantId, reason);

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

    // 基础 8 栏 + 调用方追加栏（本机「实盘交易」栏目）。清单与顺序的唯一事实源在
    // `utils/consoleTabs.ts`——实盘与模拟共用同一张表，改一处两边同时生效。
    const tabs = useMemo(() => composeConsoleTabs(extraTabs), [extraTabs]);

    // 切市场后原页签可能已不存在（如停在美股下没有的「真单镜像」）→ 回落默认页，
    // 否则内容区渲染成整块空白。公开树无追加页签时 id 集合恒定，该分支恒不触发。
    useEffect(() => {
        const next = resolveConsoleTab(activeTab, tabs, DEFAULT_ACTIVE_TAB);
        if (next !== activeTab) switchTab(next);
    }, [activeTab, tabs, switchTab]);

    // 传给追加页签的运行期上下文：与基础页签同一份数据、同一个刷新入口
    const tabContext = useMemo<RealTradingTabContext>(
        () => ({
            userId,
            tenantId,
            market: currentMarket,
            status,
            accountInfo,
            refresh: () => {
                void fetchData();
            },
        }),
        [userId, tenantId, currentMarket, status, accountInfo, fetchData],
    );

    return (
        <div className="w-full h-full bg-[#f8fafc] p-6 flex flex-col overflow-hidden font-sans box-border">
            {/* Unified Frame Container with 32px Border Radius (BacktestCenter Style) */}
            <div className="bg-white border border-gray-200 shadow-sm w-full h-full rounded-[32px] flex flex-col overflow-hidden">
                {/* Integrated Top Header - Account Overview（自然高度：8 卡片单行后不再占 30% 版面） */}
                <div className="shrink-0 flex flex-col bg-white border-b border-gray-200 overflow-hidden z-10">
                    <TopBar
                        isConnected={!!status}
                        strategyStatus={strategyStatus}
                        tradingMode={tradingMode}
                        runMode={resolvedRunMode}
                        orchestrationMode={resolvedOrchestrationMode}
                        topBarExtras={topBarExtras}
                        accountInfo={(() => {
                            return accountInfo ? buildTradingTopBarAccountInfo(accountInfo, status) : undefined;
                        })()}
                    />
                    {/* 调用方横幅：无调用方时 `banner` 为 undefined，整段不渲染。
                        `ctx` 与追加页签同源，两边看到的是同一份账户快照。 */}
                    {banner && <div className="shrink-0">{banner(tabContext)}</div>}
                </div>

                {/* Bottom Section - Sidebar & Content（占满剩余高度） */}
                <div className="flex-1 min-h-0 flex overflow-hidden">
                    {/* Left Sidebar - Navigation */}
                    <div className="w-[200px] flex flex-col border-r border-gray-200 bg-white shrink-0">
                        <div className="flex-1 overflow-y-auto py-3.5 px-3 space-y-1.5 custom-scrollbar">
                            <div className="px-2.5 py-1 mb-1">
                                <span className="text-[12px] font-black text-slate-400 uppercase tracking-widest">功能导航</span>
                            </div>
                            {tabs.map(tab => (
                                <button
                                    key={tab.id}
                                    onClick={() => switchTab(tab.id)}
                                    className={`w-full flex items-center gap-3 px-3.5 py-2.5 rounded-xl text-[17px] tracking-wide transition-all duration-150
                                        ${activeTab === tab.id
                                            ? 'bg-blue-50 text-blue-600 border border-blue-200/80 shadow-2xs font-bold'
                                            : 'text-slate-600 hover:text-slate-900 hover:bg-slate-100/70 font-medium'
                                        }
                                    `}
                                >
                                    <tab.icon size={19} className={activeTab === tab.id ? 'text-blue-500' : 'text-slate-400'} />
                                    <span>{tab.label}</span>
                                </button>
                            ))}
                        </div>

                        {/* Bottom help, explicit mode selector, and trading disclaimer. */}
                        <div className="p-3 pb-6 border-t border-gray-200 shrink-0 bg-white space-y-1.5">
                            {/* 固定模式下不挂开关：本页模式由调用方定死，开关切不动它，
                                却会写全局偏好（localStorage + store），把「模拟交易」页
                                一起带过去——一个按不动的按钮比没有按钮更像坏了。 */}
                            {!forcedTradingMode && isLiveTradingEnabled() && (
                            <div className="flex items-center justify-between gap-2 px-1 pb-1">
                                <span className="text-[11px] font-semibold text-slate-400">交易模式</span>
                                <button
                                    type="button"
                                    role="switch"
                                    aria-checked={tradingMode === 'real'}
                                    aria-label={`当前交易模式：${tradingMode === 'real' ? '实盘' : '模拟盘'}，点击切换`}
                                    onClick={() => handleModeSwitch(tradingMode === 'real' ? 'simulation' : 'real')}
                                    className={`relative flex h-8 w-[88px] items-center rounded-full border p-1 transition-all ${
                                        tradingMode === 'real'
                                            ? 'border-emerald-300 bg-emerald-50'
                                            : 'border-amber-300 bg-amber-50'
                                    }`}
                                    title="切换实盘 / 模拟盘"
                                >
                                    <span className={`absolute top-1 bottom-1 w-[39px] rounded-full shadow-sm transition-transform ${
                                        tradingMode === 'real' ? 'translate-x-[40px] bg-emerald-500' : 'translate-x-0 bg-amber-500'
                                    }`} />
                                    <span className="relative z-10 flex w-full justify-between px-1.5 text-[11px] font-bold">
                                        <span className={tradingMode === 'real' ? 'text-slate-700' : 'text-white'}>模拟</span>
                                        <span className={tradingMode === 'real' ? 'text-white' : 'text-slate-700'}>实盘</span>
                                    </span>
                                </button>
                            </div>
                            )}
                            {/* 实盘节点形态不挂帮助中心：那台机器通常没有外网，
                                点开只有浏览器错误页。 */}
                            {!LIVE_NODE_ONLY && (
                                <HelpCenterLink className="w-full text-xs font-semibold tracking-wide" />
                            )}
                            {/* T-FE-17 免责页脚自左侧底部移除（2026-09-17）：改由顶栏「本地沙箱」后的顶部免责小字承载 */}
                        </div>
                    </div>

                    {/* Right Content Area */}
                    <div className="flex-1 overflow-hidden relative bg-gray-50/50">
                    {activeTab === 'desk' && <DeskTodayPage embedded tradingRunning={status?.status === 'running'} />}
                    {activeTab === 'signals' && (
                        /* 候选信号：quant-Trader 个股终端左栏复刻（列表/筛选/补推理）；
                           模型刷新（补推理）完成后跳「系统健康」 */
                        <SignalsExplorerPage onModelRefreshed={() => switchTab('desk')} />
                    )}
                    {activeTab === 'manage' && (
                            <TopologyConsole
                                tenantId={tenantId}
                                userId={userId}
                                tradingMode={tradingMode}
                                onDeploy={handleDeploy}
                                onStop={handleStop}
                                onOpenManualTask={() => switchTab('manual-task')}
                                onOpenHistory={() => switchTab('history')}
                            />
                    )}
                    {activeTab === 'manual-task' && (
                        <ManualTaskPage
                            tenantId={tenantId}
                            userId={userId}
                            tradingMode={tradingMode}
                            onBack={() => switchTab('manage')}
                            // 调用方注入的追加区块（无调用方时为 undefined，逐位不变）。
                            // 回调只返回**元素**、不直接调 hook —— 它是在本组件的渲染里被
                            // 调用的，hook 会挂到本组件上（同 extraTabs 的纪律）。
                            extraSections={manualTaskExtras?.(tabContext)}
                        />
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
                            accountMode={tradingMode}
                            highlightSymbol={initialSymbol}
                            // 同 manualTaskExtras：回调返回元素，不在这里调 hook
                            railPanels={positionRailExtras?.(tabContext)}
                        />
                    )}
                    {activeTab === 'history' && (
                        <TradingHistory
                            userId={userId}
                            isActive={activeTab === 'history'}
                            tradingMode={tradingMode}
                        />
                    )}
                    {activeTab === 'settings' && (
                        <SettingsCenter
                            userId={userId}
                            isActive={activeTab === 'settings'}
                            // 只看**固定模式**，不看生效模式：公开树只有一栏、模式由顶栏开关
                            // 切换，那里模拟态也必须能配券商凭证（配完才切得过去），
                            // 按生效模式藏会平白砍掉一条既有路径。
                            // 会被藏起来的只有一种情形：调用方把模式**定死**成模拟盘
                            // （`resolveSimColumnForcedMode`，即本机并存的「实盘交易」栏目），
                            // 那时实盘配置归另一栏管。
                            liveConfigVisible={forcedTradingMode !== 'simulation'}
                            // 标题取词用**生效模式**（与顶栏、侧栏同一个值）：
                            // 实盘栏的「实盘交易设置」、模拟栏的「模拟交易设置」。
                            tradingMode={tradingMode}
                            extraPanels={settingsPanels}
                        />
                    )}
                    {activeTab === 'replay' && <ReplayPage />}
                    {/* 追加页签内容。按 id 命中才挂载：与基础栏同规矩，切走即卸载，
                        面板内的草稿/轮询不留在后台空转；开了 `keepMounted` 的栏换成
                        `hidden`，保住里面的长连接（见 RealTradingExtraTab.keepMounted）。
                        包壳走 `contents`：不生成盒子，与逐位裸渲染等价，别改成 `block`。
                        无追加页签时这段不产出节点。 */}
                    {extraTabs?.map((tab) => {
                        const isActive = activeTab === tab.id;
                        if (!isActive && !tab.keepMounted) return null;
                        return (
                            <div key={tab.id} className={isActive ? 'contents' : 'hidden'}>
                                {tab.render(tabContext)}
                            </div>
                        );
                    })}
                </div>
            </div>
        </div>

            {/* 启动门禁拒绝：常驻说明（非 toast），列出逐项 ✗ 与下一步 */}
            <StartGateFailedModal
                failure={startGateFailure}
                onClose={() => setStartGateFailure(null)}
            />

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
                market={currentMarket}
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
            {/* 切换确认卡只在模式可切时存在；固定模式没有「待确认的切换」 */}
            {!forcedTradingMode && modeSwitch.confirmModal}
        </div>
    );
};

export default RealTradingPage;
