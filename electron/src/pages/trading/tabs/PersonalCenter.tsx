/**
 * 个人中心（模拟交易）。
 *
 * 版面三段，从「我是谁 / 机器在不在跑」到「我的钱与我的纪律」再到「纪律的具体内容」：
 *
 * 1. **身份与运行态**——账户标识、注册时间、交易节点与数据同步的新鲜度；
 * 2. **资金与操作**——总资产 / 可用现金 / 统计基准，外加重置与持仓图片同步；
 * 3. **交易黑名单**——候选信号默认排除的名单，可增删（见 {@link TradingBlacklistPanel}）。
 *
 * 原先这一页把「账户数字」「重置按钮」「基准校准」全塞进一张叫「其他设置」的卡，
 * 三件事共用一组灰底块，读的人分不清哪个数是状态、哪个是按钮的结果。现在按
 * 「读的 / 做的 / 维护的」分开：状态在卡里只读、动作在卡底成组、名单单独成区。
 *
 * 页面自身不再依赖 `flex-1 min-h-0` 的嵌套撑高（黑名单表是可变高的），改成
 * 外层定高 + 内层滚动，底部留实体块给悬浮 Dock 让位。
 */

import React, { useEffect, useMemo, useRef, useState } from 'react';
import { useAppSelector } from '../../../store';
import { selectCurrentMarket } from '../../../store/slices/uiSlice';
import {
    Activity,
    Camera,
    CheckCircle2,
    Clock,
    Database,
    DownloadCloud,
    RefreshCw,
    Server,
    Settings2,
    Shield,
    Trash2,
    Upload,
    User,
    Wallet,
} from 'lucide-react';
import { message, Modal } from 'antd';
import type { AccountInfo, RealTradingStatus } from '../../../services/realTradingService';
import { strategyManagementService } from '../../../services/strategyManagementService';
import { userCenterService } from '../../../features/user-center/services/userCenterService';
import { resolveTradingAccountMode } from '../utils/accountAdapter';
import { SERVICE_URLS } from '../../../config/services';
import { TradingBlacklistPanel } from '../../../features/trading-blacklist/TradingBlacklistPanel';
import { HoldingAlertSettings } from '../../../features/holding-alerts/HoldingAlertSettings';

interface PersonalCenterProps {
    tenantId: string;
    userId: string;
    status: RealTradingStatus | null;
    tradingMode: 'real' | 'simulation';
}

const SIM_AMOUNT_STEP = 100000;

/** 数据同步「新鲜」的判据（秒）：一次采集周期约 5 分钟，超过就是陈旧 */
const CAPTURE_STALE_SEC = 300;

const PersonalCenter: React.FC<PersonalCenterProps> = ({ tenantId, userId, status, tradingMode }) => {
    const currentMarket = useAppSelector(selectCurrentMarket);
    const isRunning = status?.status === 'running';
    const activeStrategy = status?.strategy;

    const [isSyncing, setIsSyncing] = useState(false);
    const [createdAt, setCreatedAt] = useState<string | null>(null);
    // L2 实时任务状态（交易节点 / 数据同步 / 运行时长）
    const [l2Status, setL2Status] = useState<{ capture?: any; realtime?: any } | null>(null);
    const apiGatewayBase = SERVICE_URLS.API_GATEWAY.replace(/\/+$/, '');

    useEffect(() => {
        let alive = true;
        if (currentMarket !== 'CN') return;  // L2/TDX 仅 A 股
        const fetchL2Status = async () => {
            try {
                const res = await fetch(`${apiGatewayBase}/api/v1/tdx/l2/status`, {
                    headers: { Authorization: `Bearer ${localStorage.getItem('access_token') || ''}` },
                });
                if (!alive) return;
                if (res.ok) setL2Status(await res.json());
            } catch {
                /* 桥/服务不可达时保持上一状态 */
            }
        };
        fetchL2Status();
        const timer = setInterval(fetchL2Status, 20000);
        return () => {
            alive = false;
            clearInterval(timer);
        };
    }, [apiGatewayBase, currentMarket]);

    const formatUptime = (startedAt?: string | null) => {
        if (!startedAt) return '--';
        const start = new Date(startedAt).getTime();
        if (Number.isNaN(start)) return '--';
        const diffSec = Math.max(0, Math.floor((Date.now() - start) / 1000));
        const h = Math.floor(diffSec / 3600);
        const m = Math.floor((diffSec % 3600) / 60);
        const s = diffSec % 60;
        return h > 0 ? `${h}h ${m}m` : m > 0 ? `${m}m ${s}s` : `${s}s`;
    };

    const captureStale = useMemo(() => {
        const last = l2Status?.capture?.last_cycle_at as string | undefined;
        if (!last) return true;
        const ageSec = (Date.now() - new Date(last).getTime()) / 1000;
        return !Number.isFinite(ageSec) || ageSec > CAPTURE_STALE_SEC;
    }, [l2Status]);

    const nodeRunning = (l2Status?.realtime?.running === true || l2Status?.capture?.running === true);

    const handleSyncTemplates = async () => {
        setIsSyncing(true);
        try {
            const res = await strategyManagementService.syncTemplates();
            message.success(res.message || `同步成功，新增 ${res.synced_count} 个模板`);
            window.dispatchEvent(new CustomEvent('refresh-strategy-list'));
        } catch (err: any) {
            message.error(`同步失败: ${err.message}`);
        } finally {
            setIsSyncing(false);
        }
    };

    const [selectedAccount, setSelectedAccount] = useState<AccountInfo | null>(null);
    const [independentCash, setIndependentCash] = useState<number | null>(null);
    const [configuredInitialCash, setConfiguredInitialCash] = useState<number>(1_000_000);
    const [draftInitialCash, setDraftInitialCash] = useState<number>(1_000_000);
    const [loadingSettings, setLoadingSettings] = useState(false);
    const [resettingSimulation, setResettingSimulation] = useState(false);
    const [ocrModalOpen, setOcrModalOpen] = useState(false);
    const [ocrLoading, setOcrLoading] = useState(false);
    const [ocrFiles, setOcrFiles] = useState<File[]>([]);
    const [ocrResults, setOcrResults] = useState<any[]>([]);
    const [ocrAvailableCash, setOcrAvailableCash] = useState<number | undefined>(undefined);
    const [isSyncingHoldings, setIsSyncingHoldings] = useState(false);
    const [snapshotNotice, setSnapshotNotice] = useState<string | null>(null);
    const snapshotNoticeTimerRef = useRef<number | null>(null);

    const showSnapshotNotice = (text: string) => {
        setSnapshotNotice(text);
        if (snapshotNoticeTimerRef.current) {
            window.clearTimeout(snapshotNoticeTimerRef.current);
        }
        snapshotNoticeTimerRef.current = window.setTimeout(() => {
            setSnapshotNotice(null);
            snapshotNoticeTimerRef.current = null;
        }, 3000);
    };

    useEffect(() => {
        return () => {
            if (snapshotNoticeTimerRef.current) {
                window.clearTimeout(snapshotNoticeTimerRef.current);
            }
        };
    }, []);

    useEffect(() => {
        userCenterService.getUserProfile(userId).then(profile => {
            if (profile?.created_at) {
                setCreatedAt(profile.created_at);
            }
        }).catch(() => {});
    }, [userId]);

    const loadAccountSettings = React.useCallback(async (mounted: boolean = true) => {
        setLoadingSettings(true);
        try {
            const runtimeMode = resolveTradingAccountMode(status?.mode, tradingMode);
            const { realTradingService } = await import('../../../services/realTradingService');
            const accountResp = await realTradingService.getRuntimeAccount(userId, tenantId, runtimeMode, currentMarket).catch(() => null);

            if (!mounted) return;

            if (tradingMode === 'simulation') {
                const settings = await realTradingService.getSimulationSettings();
                if (settings) {
                    const value = Number(settings.initial_cash || 1_000_000);
                    if (value > 0) {
                        setConfiguredInitialCash(value);
                    }
                }
            } else {
                // 实盘：统计基准来自「账户设置」里的初始权益
                const settings = await realTradingService.getRealAccountSettings();
                if (settings) {
                    setConfiguredInitialCash(Number(settings.initial_equity || 0));
                }
            }

            if (accountResp) {
                setSelectedAccount(accountResp);
            }

            const accountLike = accountResp as ({ available_cash?: number; cash?: number } | null);
            const cashValue = Number(accountLike?.available_cash ?? accountLike?.cash ?? NaN);
            setIndependentCash(Number.isFinite(cashValue) ? cashValue : null);
        } catch (err) {
            console.error('Failed to load account settings', err);
        } finally {
            if (mounted) setLoadingSettings(false);
        }
    }, [status?.mode, tenantId, tradingMode, userId, currentMarket]);

    useEffect(() => {
        let mounted = true;
        loadAccountSettings(mounted);
        return () => {
            mounted = false;
        };
    }, [loadAccountSettings, tenantId, tradingMode, userId]);

    const modeAccount = selectedAccount;

    const handleResetSimulation = async () => {
        setResettingSimulation(true);
        try {
            const { realTradingService } = await import('../../../services/realTradingService');
            const account = await realTradingService.resetSimulationAccount(
                userId,
                configuredInitialCash,
                tenantId,
                currentMarket
            );
            setSelectedAccount(account);
            showSnapshotNotice('今日快照已更新');
            message.success('模拟盘已重置，资金快照已更新');
        } catch (err) {
            console.error('Failed to reset simulation account', err);
            message.error('重置失败，请稍后重试');
        } finally {
            setResettingSimulation(false);
        }
    };

    const handleSaveInitialCash = async () => {
        if (draftInitialCash <= 0) {
            message.warning('初始基准金额必须大于 0');
            return;
        }
        try {
            const { realTradingService } = await import('../../../services/realTradingService');
            const success = await realTradingService.updateRealAccountSettings(draftInitialCash);
            if (success) {
                setConfiguredInitialCash(draftInitialCash);
                message.success('统计基准更新成功');
                loadAccountSettings();
            }
        } catch (err: any) {
            message.error(`保存失败: ${err.message}`);
        }
    };

    const handleOcrAnalyze = async () => {
        if (ocrFiles.length === 0) {
            message.warning('请先上传持仓截图');
            return;
        }
        setOcrLoading(true);
        try {
            const formData = new FormData();
            ocrFiles.forEach(file => formData.append('images', file));

            const { realTradingService } = await import('../../../services/realTradingService');
            const res = await realTradingService.analyzeHoldingImages(formData);
            if (res.success) {
                setOcrResults(res.data || []);
                setOcrAvailableCash(typeof (res as any).available_cash === 'number' ? (res as any).available_cash : undefined);
                message.success(`识别成功，发现 ${res.data?.length || 0} 只股票`);
            } else {
                message.error(res.message || '识别失败');
            }
        } catch (err: any) {
            console.error('OCR analysis failed', err);
            message.error(err.message || '服务异常，识别失败');
        } finally {
            setOcrLoading(false);
        }
    };

    const handleConfirmOcrSync = async () => {
        if (ocrResults.length === 0) return;
        setIsSyncingHoldings(true);
        try {
            const { realTradingService } = await import('../../../services/realTradingService');
            const success = await realTradingService.syncSimulationHoldings(ocrResults, ocrAvailableCash);
            if (success) {
                message.success('持仓同步成功，模拟账户已更新');
                setOcrModalOpen(false);
                setOcrFiles([]);
                setOcrResults([]);
                setOcrAvailableCash(undefined);
                loadAccountSettings();
                window.dispatchEvent(new CustomEvent('refresh-account-data'));
            }
        } catch (err: any) {
            message.error(err.message || '同步失败');
        } finally {
            setIsSyncingHoldings(false);
        }
    };

    const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
        if (e.target.files) {
            const files = Array.from(e.target.files);
            setOcrFiles([...ocrFiles, ...files]);
        }
        // Reset the input value so the same file can be selected again
        e.target.value = '';
    };

    return (
        <div className="h-full flex flex-col overflow-hidden">
            <div className="flex-1 min-h-0 overflow-y-auto custom-scrollbar">
                <div className="p-4 flex flex-col gap-4 max-w-[1500px] mx-auto">

                    {/* 页头 */}
                    <header className="flex items-start justify-between gap-3 flex-wrap">
                        <div>
                            <h3 className="text-xl font-bold text-gray-800 flex items-center">
                                <User className="mr-3 text-indigo-600" size={22} />
                                个人中心
                            </h3>
                            <p className="text-xs text-gray-500 mt-1">
                                账户与运行状态、资金与统计基准、以及候选信号要排除的交易黑名单。
                            </p>
                        </div>
                        <span className="px-2.5 py-1 bg-green-50 text-green-700 text-xs rounded-lg font-bold border border-green-200 flex items-center gap-1.5 shrink-0">
                            <Shield size={12} /> 已实名认证
                        </span>
                    </header>

                    {/* 第一排：身份 / 运行状态 / 资金与操作 */}
                    <div className="grid grid-cols-1 lg:grid-cols-2 xl:grid-cols-3 gap-4">

                        {/* 账户卡 */}
                        <section className="bg-white rounded-2xl border border-gray-200 p-4 shadow-sm flex flex-col">
                            <CardHead icon={<User size={14} />} title="账户" tone="indigo" />
                            <div className="flex items-center gap-3">
                                <div className="w-11 h-11 bg-indigo-50 rounded-full flex items-center justify-center text-indigo-600 shrink-0">
                                    <User size={20} />
                                </div>
                                <div className="min-w-0">
                                    <div className="font-mono text-sm font-bold text-gray-800 truncate">
                                        {tenantId}:{userId}
                                    </div>
                                    <div className="text-[11px] text-gray-500">
                                        注册于 {createdAt ? new Date(createdAt).toLocaleDateString() : '--'}
                                    </div>
                                </div>
                            </div>
                            <div className="grid grid-cols-2 gap-2 mt-3">
                                <Metric label="账户权限" value="高级交易员" />
                                <Metric
                                    label="运行环境"
                                    value={activeStrategy ? 'Python 3.8 / Qlib' : '空闲'}
                                />
                            </div>

                            {/* 当前运行策略并入账户卡：它回答的正是「这个账户在跑什么」，
                                单独占一张整行卡会把下面的黑名单表挤到折叠线以下。 */}
                            <div className="mt-3 rounded-xl bg-gray-50 border border-gray-100 px-3 py-2">
                                <div className="flex items-center gap-1.5 text-[10px] font-bold text-gray-500">
                                    <Settings2 size={11} /> 当前运行策略
                                </div>
                                {activeStrategy ? (
                                    <>
                                        <div className="mt-1 flex items-center gap-2 min-w-0">
                                            <span className="w-1.5 h-1.5 rounded-full bg-emerald-500 shrink-0 animate-pulse" />
                                            <span className="text-[13px] font-bold text-gray-800 truncate">{activeStrategy.name}</span>
                                            <span
                                                className="ml-auto font-mono text-[10px] text-gray-400 truncate shrink-0 max-w-[45%]"
                                                title={activeStrategy.id}
                                            >
                                                {activeStrategy.id}
                                            </span>
                                        </div>
                                        {activeStrategy.description && (
                                            <div className="mt-0.5 text-[10px] text-gray-500 truncate" title={activeStrategy.description}>
                                                {activeStrategy.description}
                                            </div>
                                        )}
                                    </>
                                ) : (
                                    <div className="mt-1 text-[11px] text-gray-400">
                                        无运行中的策略 —— 前往「策略管理」启动
                                    </div>
                                )}
                            </div>

                            <button
                                onClick={handleSyncTemplates}
                                disabled={isSyncing}
                                className="mt-3 flex items-center justify-center gap-1.5 px-3 py-1.5 bg-indigo-50 text-indigo-600 rounded-lg text-[11px] font-bold border border-indigo-100 hover:bg-indigo-100 transition-all disabled:opacity-50"
                            >
                                {isSyncing ? <RefreshCw size={12} className="animate-spin" /> : <DownloadCloud size={12} />}
                                同步系统策略
                            </button>
                        </section>

                        {/* 系统运行状态 */}
                        <section className="bg-white rounded-2xl border border-gray-200 p-4 shadow-sm flex flex-col">
                            <CardHead icon={<Activity size={14} />} title="系统运行状态" tone="violet" />
                            <div className="flex flex-col gap-1.5">
                                <StatusRow
                                    icon={<Server size={13} className="text-gray-500" />}
                                    label="交易节点"
                                    value={
                                        <Pill
                                            ok={isRunning || nodeRunning}
                                            text={isRunning || nodeRunning ? 'Running' : 'Stopped'}
                                        />
                                    }
                                />
                                <StatusRow
                                    icon={<Database size={13} className="text-gray-500" />}
                                    label="数据同步"
                                    value={
                                        l2Status?.capture?.last_cycle_at ? (
                                            <Pill
                                                ok={!captureStale}
                                                text={`${new Date(l2Status.capture.last_cycle_at).toLocaleTimeString('zh-CN', { hour12: false })} · ${captureStale ? '陈旧' : '新鲜'}`}
                                            />
                                        ) : (
                                            <span className="text-gray-800 font-mono text-xs">--</span>
                                        )
                                    }
                                />
                                <StatusRow
                                    icon={<Clock size={13} className="text-gray-500" />}
                                    label="运行时长"
                                    value={
                                        <span className="text-gray-800 font-mono text-xs">
                                            {isRunning || nodeRunning ? formatUptime(l2Status?.realtime?.started_at) : '--'}
                                        </span>
                                    }
                                />
                            </div>
                            <p className="text-[10px] text-gray-400 mt-2 leading-relaxed">
                                数据同步每 20 秒自检一次；超过 5 分钟未更新会标红，说明采集节点已停。
                            </p>
                        </section>

                        {/* 资金与操作 */}
                        <section className="bg-white rounded-2xl border border-gray-200 p-4 shadow-sm flex flex-col lg:col-span-2 xl:col-span-1">
                            <CardHead icon={<Wallet size={14} />} title="资金与统计基准" tone="blue" />
                            <div className="grid grid-cols-2 gap-2">
                                <Metric
                                    label="总资产"
                                    value={`¥${(modeAccount?.total_asset || 0).toLocaleString()}`}
                                    mono
                                />
                                <Metric
                                    label="可用现金"
                                    value={independentCash === null ? '账户未上报' : `¥${independentCash.toLocaleString()}`}
                                    mono
                                />
                            </div>
                            <div className="mt-2 rounded-xl bg-gray-50 border border-gray-100 px-3 py-2 flex items-baseline justify-between">
                                <span className="text-[10px] text-gray-500">
                                    {tradingMode === 'simulation' ? '模拟盘统计基准' : '实盘统计基准（初始权益）'}
                                </span>
                                <span className="text-sm font-bold text-gray-800 font-mono">
                                    ¥{configuredInitialCash.toLocaleString()}
                                </span>
                            </div>

                            {tradingMode === 'simulation' ? (
                                <>
                                    <p className="text-[11px] text-gray-500 leading-relaxed mt-2">
                                        重置会清空全部持仓并恢复初始现金，用于重新开始一轮模拟。
                                    </p>
                                    <div className="grid grid-cols-2 gap-2 mt-auto pt-3">
                                        <button
                                            onClick={handleResetSimulation}
                                            disabled={resettingSimulation || loadingSettings}
                                            className="px-3 py-2 rounded-xl bg-blue-600 text-white text-[13px] font-medium hover:bg-blue-700 transition-colors disabled:bg-gray-300 disabled:cursor-not-allowed flex items-center justify-center gap-1.5"
                                        >
                                            <RefreshCw size={13} className={resettingSimulation ? 'animate-spin' : ''} />
                                            {resettingSimulation ? '重置中…' : '重置模拟盘'}
                                        </button>
                                        <button
                                            onClick={() => setOcrModalOpen(true)}
                                            disabled={loadingSettings}
                                            className="px-3 py-2 rounded-xl border border-indigo-200 text-indigo-600 bg-indigo-50/50 text-[13px] font-medium hover:bg-indigo-50 transition-colors flex items-center justify-center gap-1.5"
                                        >
                                            <Camera size={13} />
                                            持仓图片同步
                                        </button>
                                    </div>
                                </>
                            ) : (
                                <div className="mt-2 rounded-xl border border-gray-200 p-2.5">
                                    <div className="text-[11px] font-bold text-gray-700 mb-1.5">
                                        校准统计基准（PnL Baseline）
                                    </div>
                                    <div className="flex gap-2">
                                        <input
                                            type="number"
                                            step={1000}
                                            min={0}
                                            value={draftInitialCash}
                                            onChange={(e) => setDraftInitialCash(Number(e.target.value || 0))}
                                            className="flex-1 px-2.5 py-1.5 rounded-lg border border-gray-300 focus:outline-none focus:ring-2 focus:ring-blue-500 text-[13px] font-mono"
                                            placeholder="初始资金基准"
                                        />
                                        <button
                                            onClick={handleSaveInitialCash}
                                            className="px-4 py-1.5 bg-blue-600 hover:bg-blue-700 text-white rounded-lg text-[13px] font-bold transition-all active:scale-95"
                                        >
                                            保存
                                        </button>
                                        <button
                                            onClick={() => {
                                                const brokerPnl = (status?.portfolio as any)?.broker_total_pnl || (status?.portfolio as any)?.total_pnl_raw || 0;
                                                const totalAsset = status?.portfolio?.total_value || 0;
                                                const inferredBaseline = totalAsset - brokerPnl;
                                                if (inferredBaseline > 0) {
                                                    setDraftInitialCash(Math.round(inferredBaseline));
                                                    message.info('已填充：根据券商盈亏推算的成本基准');
                                                }
                                            }}
                                            className="px-3 py-1.5 bg-emerald-50 hover:bg-emerald-100 text-emerald-600 rounded-lg text-[12px] font-medium transition-colors border border-emerald-100 shrink-0"
                                            title="根据券商上报的总盈亏反推基准"
                                        >
                                            对齐券商
                                        </button>
                                    </div>
                                    <p className="text-[10px] text-gray-400 mt-1.5">
                                        修改此金额会即时改变「总盈亏」的统计起点。
                                    </p>
                                </div>
                            )}

                            {snapshotNotice && (
                                <div className="mt-2 text-[11px] font-medium text-emerald-600">{snapshotNotice}</div>
                            )}
                        </section>
                    </div>

                    {/* 第二排：持仓预警设置（左「持仓监控与提醒」/ 右「提醒通道」，两列对称） */}
                    <HoldingAlertSettings />

                    {/* 第三排：交易黑名单（本页的主内容） */}
                    <TradingBlacklistPanel />

                    {/* 给悬浮 Dock 让位。.bottom-dock 是 absolute 覆盖层（z-index 1050，不占布局），
                        必须是**占位的实体块**：给滚动容器加 padding-bottom 不会延伸可滚动区，
                        滚到底时最下面一行的操作按钮仍会被 Dock 接走事件。高度算式沿用
                        SignalsExplorerPage / ResearchPlatformPage 的 --dock-height 约定，
                        max() 兜住无 Dock 的场景（否则 calc(0px-12px) 是负值，整条声明失效）。 */}
                    <div aria-hidden className="h-[max(12px,calc(var(--dock-height)-12px))] shrink-0" />
                </div>
            </div>

            {/* OCR Sync Modal */}
            <Modal
                title={
                    <div className="flex items-center gap-2 text-indigo-600">
                        <Camera size={18} />
                        <span>图片同步持仓 (Qwen-VL)</span>
                    </div>
                }
                open={ocrModalOpen}
                onCancel={() => {
                    if (!ocrLoading && !isSyncingHoldings) {
                        setOcrModalOpen(false);
                        setOcrFiles([]);
                        setOcrResults([]);
                    }
                }}
                footer={null}
                width={680}
                centered
                mask={false}
            >
                <div className="py-2 space-y-4">
                    {/* Upload Area */}
                    <div className="relative group">
                        <input
                            type="file"
                            multiple
                            accept="image/*"
                            onChange={handleFileChange}
                            className="absolute inset-0 w-full h-full opacity-0 cursor-pointer z-10"
                        />
                        <div className="border-2 border-dashed border-indigo-100 rounded-2xl p-8 bg-indigo-50/30 group-hover:bg-indigo-50 group-hover:border-indigo-300 transition-all flex flex-col items-center justify-center gap-3">
                            <div className="w-12 h-12 bg-white rounded-full shadow-sm flex items-center justify-center text-indigo-500">
                                <Upload size={24} />
                            </div>
                            <div className="text-center">
                                <p className="text-sm font-bold text-gray-700">点击或拖拽上传持仓截图</p>
                                <p className="text-xs text-gray-500 mt-1">支持多张图片同时识别，请确保股票代码和数量清晰可见</p>
                            </div>
                        </div>
                    </div>

                    {/* File List */}
                    {ocrFiles.length > 0 && (
                        <div className="flex flex-wrap gap-2">
                            {ocrFiles.map((file, idx) => (
                                <div key={idx} className="relative w-20 h-20 rounded-lg overflow-hidden border border-gray-200 shadow-sm">
                                    <img src={URL.createObjectURL(file)} className="w-full h-full object-cover" alt="upload" />
                                    <button
                                        onClick={() => {
                                            setOcrFiles(ocrFiles.filter((_, i) => i !== idx));
                                        }}
                                        className="absolute top-1 right-1 p-1 bg-red-500 text-white rounded-full hover:bg-red-600 transition-colors"
                                    >
                                        <Trash2 size={10} />
                                    </button>
                                </div>
                            ))}
                        </div>
                    )}

                    {/* Results Table */}
                    {ocrResults.length > 0 && (
                        <div className="border border-indigo-100 rounded-2xl overflow-hidden shadow-sm">
                            <div className="bg-indigo-50/50 px-4 py-2 text-xs font-bold text-indigo-600 flex items-center justify-between">
                                <span>识别结果预览</span>
                                <span>共 {ocrResults.length} 只股票</span>
                            </div>
                            <div className="max-h-[280px] overflow-y-auto">
                                <table className="w-full text-sm">
                                    <thead className="bg-gray-50 text-gray-500 text-[11px] sticky top-0">
                                        <tr>
                                            <th className="px-4 py-2 text-left">代码/名称</th>
                                            <th className="px-4 py-2 text-right">持仓数量</th>
                                            <th className="px-4 py-2 text-right">当前市价</th>
                                            <th className="px-4 py-2 text-right">参考市值</th>
                                        </tr>
                                    </thead>
                                    <tbody className="divide-y divide-gray-100">
                                        {ocrResults.map((item, idx) => (
                                            <tr key={idx} className="hover:bg-gray-50 transition-colors">
                                                <td className="px-4 py-3">
                                                    <div className="font-bold text-gray-800">{item.symbol}</div>
                                                    <div className="text-[11px] text-gray-400">{item.name || '未知股票'}</div>
                                                </td>
                                                <td className="px-4 py-3 text-right font-mono text-blue-600 font-bold">
                                                    {Number(item.quantity).toLocaleString()}
                                                </td>
                                                <td className="px-4 py-3 text-right font-mono">
                                                    ¥{Number(item.current_price || 0).toLocaleString(undefined, {
                                                        minimumFractionDigits: 3,
                                                        maximumFractionDigits: 3,
                                                    })}
                                                </td>
                                                <td className="px-4 py-3 text-right font-mono font-bold text-gray-700">
                                                    ¥{Number(item.market_value || 0).toLocaleString()}
                                                </td>
                                            </tr>
                                        ))}
                                    </tbody>
                                </table>
                            </div>
                        </div>
                    )}

                    {/* Action Buttons */}
                    <div className="flex gap-3 pt-2">
                        <button
                            onClick={handleOcrAnalyze}
                            disabled={ocrLoading || ocrFiles.length === 0}
                            className="flex-1 py-2.5 rounded-xl bg-indigo-600 text-white text-sm font-bold shadow-lg shadow-indigo-100 hover:bg-indigo-700 transition-all disabled:bg-gray-300 disabled:shadow-none flex items-center justify-center gap-2"
                        >
                            {ocrLoading ? <RefreshCw size={16} className="animate-spin" /> : <Camera size={16} />}
                            {ocrLoading ? '正在通过 Qwen-VL 识别中...' : '开始解析图片'}
                        </button>

                        {ocrResults.length > 0 && (
                            <button
                                onClick={handleConfirmOcrSync}
                                disabled={isSyncingHoldings}
                                className="px-8 py-2.5 rounded-xl bg-emerald-600 text-white text-sm font-bold shadow-lg shadow-emerald-100 hover:bg-emerald-700 transition-all flex items-center justify-center gap-2"
                            >
                                {isSyncingHoldings ? <RefreshCw size={16} className="animate-spin" /> : <CheckCircle2 size={16} />}
                                {isSyncingHoldings ? '正在同步...' : '确认同步持仓'}
                            </button>
                        )}
                    </div>

                    <p className="text-[10px] text-gray-400 text-center">
                        提示：同步操作将覆盖模拟盘现有持仓，请谨慎操作。识别结果仅供参考，请核对后再确认。
                    </p>
                </div>
            </Modal>
        </div>
    );
};

/** 卡头：图标色块 + 标题（本页四张卡共用一套，避免每张卡各写一遍间距与字号） */
const TONE_CLS = {
    indigo: 'bg-indigo-50 text-indigo-600',
    violet: 'bg-violet-50 text-violet-600',
    blue: 'bg-blue-50 text-blue-600',
    slate: 'bg-slate-100 text-slate-600',
} as const;

const CardHead: React.FC<{ icon: React.ReactNode; title: string; tone: keyof typeof TONE_CLS }> = ({
    icon,
    title,
    tone,
}) => (
    <header className="flex items-center gap-2 mb-3">
        <span className={`flex h-6 w-6 items-center justify-center rounded-lg shrink-0 ${TONE_CLS[tone]}`}>
            {icon}
        </span>
        <h4 className="text-[13px] font-bold text-gray-800">{title}</h4>
    </header>
);

/** 只读数值块（状态用；动作一律做成按钮，避免「这个数是不是能点」的歧义） */
const Metric: React.FC<{ label: string; value: React.ReactNode; mono?: boolean }> = ({ label, value, mono }) => (
    <div className="bg-gray-50 rounded-lg px-2.5 py-1.5 min-w-0">
        <div className="text-[10px] text-gray-500 mb-0.5">{label}</div>
        <div className={`text-[13px] font-bold text-gray-800 truncate ${mono ? 'font-mono tabular-nums' : ''}`}>
            {value}
        </div>
    </div>
);

const StatusRow: React.FC<{ icon: React.ReactNode; label: string; value: React.ReactNode }> = ({
    icon,
    label,
    value,
}) => (
    <div className="flex items-center justify-between p-1.5 bg-gray-50 rounded-lg">
        <div className="flex items-center gap-2">
            {icon}
            <span className="text-gray-700 text-[13px]">{label}</span>
        </div>
        {value}
    </div>
);

/** 运行状态胶囊：ok=true 绿、false 红/琥珀由调用方决定（这里只给两态） */
const Pill: React.FC<{ ok: boolean; text: string }> = ({ ok, text }) => (
    <span
        className={`px-2 py-0.5 rounded-full text-[10px] font-bold border ${
            ok
                ? 'bg-emerald-50 text-emerald-600 border-emerald-200'
                : 'bg-amber-50 text-amber-600 border-amber-200'
        }`}
    >
        {text}
    </span>
);

export default PersonalCenter;
