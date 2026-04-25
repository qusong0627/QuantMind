import React, { useEffect, useMemo, useRef, useState } from 'react';
import { User, Activity, Server, Clock, Shield, Database, Settings2, RefreshCw, DownloadCloud } from 'lucide-react';
import { message } from 'antd';
import { AccountInfo, RealTradingStatus, realTradingService } from '../../../services/realTradingService';
import { strategyManagementService } from '../../../services/strategyManagementService';
import { resolveTradingAccountMode } from '../utils/accountAdapter';

interface PersonalCenterProps {
    tenantId: string;
    userId: string;
    status: RealTradingStatus | null;
    tradingMode: 'real' | 'simulation';
}

const PersonalCenter: React.FC<PersonalCenterProps> = ({ tenantId, userId, status, tradingMode }) => {
    const isRunning = status?.status === 'running';
    const activeStrategy = status?.strategy;
    
    // ... (现有状态)
    const [isSyncing, setIsSyncing] = useState(false);

    const handleSyncTemplates = async () => {
        setIsSyncing(true);
        try {
            const res = await strategyManagementService.syncTemplates();
            message.success(res.message || `同步成功，新增 ${res.synced_count} 个模板`);
            // 提示用户刷新列表
            window.dispatchEvent(new CustomEvent('refresh-strategy-list'));
        } catch (err: any) {
            message.error(`同步失败: ${err.message}`);
        } finally {
            setIsSyncing(false);
        }
    };
    const SIM_AMOUNT_STEP = 100000;
    const COOLDOWN_DAYS = 30;

    const [selectedAccount, setSelectedAccount] = useState<AccountInfo | null>(null);
    const [independentCash, setIndependentCash] = useState<number | null>(null);
    const [configuredInitialCash, setConfiguredInitialCash] = useState<number>(1_000_000);
    const [draftInitialCash, setDraftInitialCash] = useState<number>(1_000_000);
    const [lastModifiedAt, setLastModifiedAt] = useState<string | null>(null);
    const [nextAllowedModifiedAt, setNextAllowedModifiedAt] = useState<string | null>(null);
    const [canModifyAmount, setCanModifyAmount] = useState<boolean>(true);
    const [amountStep, setAmountStep] = useState<number>(SIM_AMOUNT_STEP);
    const [cooldownDays, setCooldownDays] = useState<number>(COOLDOWN_DAYS);
    const [loadingSettings, setLoadingSettings] = useState(false);
    const [savingSettings, setSavingSettings] = useState(false);
    const [resettingSimulation, setResettingSimulation] = useState(false);
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

    const loadAccountSettings = React.useCallback(async (mounted: boolean = true) => {
        setLoadingSettings(true);
        try {
            const runtimeMode = resolveTradingAccountMode(status?.mode, tradingMode);
            const accountResp = await realTradingService.getRuntimeAccount(userId, tenantId, runtimeMode).catch(() => null);

            if (!mounted) return;

            if (tradingMode === 'simulation') {
                const settings = await realTradingService.getSimulationSettings();
                if (settings) {
                    const value = Number(settings.initial_cash || 1_000_000);
                    if (value > 0) {
                        setConfiguredInitialCash(value);
                        setDraftInitialCash(value);
                    }
                    setLastModifiedAt(settings.last_modified_at || null);
                    setNextAllowedModifiedAt(settings.next_allowed_modified_at || null);
                    setCanModifyAmount(Boolean(settings.can_modify));
                    setAmountStep(Number(settings.amount_step || SIM_AMOUNT_STEP));
                    setCooldownDays(Number(settings.cooldown_days || COOLDOWN_DAYS));
                }
            } else {
                // Real Mode: Restore settings
                const settings = await realTradingService.getRealAccountSettings();
                if (settings) {
                    const value = Number(settings.initial_equity || 0);
                    setConfiguredInitialCash(value);
                    setDraftInitialCash(value);
                    setLastModifiedAt(settings.last_modified_at || null);
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
    }, [status?.mode, tenantId, tradingMode, userId]);

    useEffect(() => {
        let mounted = true;
        loadAccountSettings(mounted);
        return () => {
            mounted = false;
        };
    }, [loadAccountSettings, tenantId, tradingMode, userId]);

    const nextModifyTime = useMemo(() => {
        const source = nextAllowedModifiedAt || lastModifiedAt;
        if (!source) return null;
        const t = new Date(source).getTime();
        if (!Number.isFinite(t)) return null;
        if (nextAllowedModifiedAt) return new Date(t);
        return new Date(t + cooldownDays * 24 * 60 * 60 * 1000);
    }, [cooldownDays, lastModifiedAt, nextAllowedModifiedAt]);

    const validateAmount = (value: number) =>
        Number.isFinite(value) && value >= amountStep && value % amountStep === 0;
    const modeAccount = selectedAccount;

    const handleSaveInitialCash = async () => {
        if (tradingMode === 'simulation') {
            if (!validateAmount(draftInitialCash)) {
                message.error(`初始金额必须为${Math.floor(amountStep / 10000)}万的整数倍，且不低于${Math.floor(amountStep / 10000)}万`);
                return;
            }
            if (!canModifyAmount) {
                message.warning('初始金额每30天仅可修改一次，请稍后再试');
                return;
            }
        } else {
            if (draftInitialCash <= 0) {
                message.error('实盘初始权益必须大于 0');
                return;
            }
        }

        setSavingSettings(true);
        try {
            if (tradingMode === 'simulation') {
                const updated = await realTradingService.updateSimulationSettings(draftInitialCash);
                if (!updated) {
                    throw new Error('empty settings response');
                }
                setConfiguredInitialCash(Number(updated.initial_cash || draftInitialCash));
                setDraftInitialCash(Number(updated.initial_cash || draftInitialCash));
                setLastModifiedAt(updated.last_modified_at || null);
                setNextAllowedModifiedAt(updated.next_allowed_modified_at || null);
                setCanModifyAmount(Boolean(updated.can_modify));
                
                // Simulation behavior: reset account
                const resetAccount = await realTradingService.resetSimulationAccount(
                    userId,
                    Number(updated.initial_cash || draftInitialCash),
                    tenantId
                );
                if (resetAccount) {
                    setSelectedAccount(resetAccount);
                }
                message.success('模拟盘初始金额已保存并重置，资金快照已更新');
            } else {
                // Real mode behavior
                const success = await realTradingService.updateRealAccountSettings(draftInitialCash);
                if (success) {
                    setConfiguredInitialCash(draftInitialCash);
                    message.success('实盘初始权益已同步，下次计算将使用此基准');
                    // Refresh account to see immediate effect if possible
                    loadAccountSettings();
                } else {
                    throw new Error('Update failed');
                }
            }
            showSnapshotNotice('今日基准已更新');
            // 调度全局刷新事件，让后台仪表盘等组件即时更新
            window.dispatchEvent(new CustomEvent('refresh-account-data'));
            window.dispatchEvent(new CustomEvent('refresh-strategy-status'));
        } catch (err) {
            console.error('Failed to save settings', err);
            message.error('保存失败，请稍后重试');
        } finally {
            setSavingSettings(false);
        }
    };

    const handleResetSimulation = async () => {
        if (!validateAmount(configuredInitialCash)) {
            message.error('当前配置金额无效，请先设置合法的初始金额');
            return;
        }
        setResettingSimulation(true);
        try {
            const account = await realTradingService.resetSimulationAccount(
                userId,
                configuredInitialCash,
                tenantId
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

    return (
        <div className="h-full p-3 flex flex-col gap-3">
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                {/* User Profile Card */}
                <div className="bg-white rounded-2xl border border-gray-200 p-3 shadow-sm">
                    <div className="flex items-center gap-4 mb-3">
                        <div className="w-12 h-12 bg-blue-100 rounded-full flex items-center justify-center text-blue-600">
                            <User size={24} />
                        </div>
                        <div>
                            <h2 className="text-lg font-bold text-gray-800">实盘账户中心</h2>
                            <p className="text-gray-500 font-mono text-sm">{tenantId}:{userId}</p>
                            <button 
                                onClick={handleSyncTemplates}
                                disabled={isSyncing}
                                className="mt-2 flex items-center gap-1.5 px-3 py-1.5 bg-indigo-50 text-indigo-600 rounded-lg text-[11px] font-bold border border-indigo-100 hover:bg-indigo-100 transition-all disabled:opacity-50"
                            >
                                {isSyncing ? <RefreshCw size={12} className="animate-spin" /> : <DownloadCloud size={12} />}
                                同步系统策略
                            </button>
                        </div>
                        <div className="ml-auto">
                            <span className="px-2 py-0.5 bg-green-100 text-green-700 text-xs rounded-full font-medium border border-green-200 flex items-center gap-1">
                                <Shield size={12} /> 已实名认证
                            </span>
                        </div>
                    </div>

                    <div className="grid grid-cols-2 gap-2">
                        <div className="bg-gray-50 p-2 rounded-xl">
                            <div className="text-gray-500 text-xs mb-1">账户权限</div>
                            <div className="font-medium text-sm text-gray-800">高级交易员</div>
                        </div>
                        <div className="bg-gray-50 p-2 rounded-lg">
                            <div className="text-gray-500 text-xs mb-1">注册时间</div>
                            <div className="font-medium text-sm text-gray-800">2025-01-15</div>
                        </div>
                    </div>
                </div>

                {/* System Status Card */}
                <div className="bg-white rounded-2xl border border-gray-200 p-3 shadow-sm">
                    <h3 className="text-base font-bold text-gray-800 mb-2 flex items-center gap-2">
                        <Activity className="text-purple-500" size={16} />
                        系统运行状态
                    </h3>

                    <div className="space-y-1.5">
                        <div className="flex items-center justify-between p-1.5 bg-gray-50 rounded-xl">
                            <div className="flex items-center gap-2">
                                <Server size={14} className="text-gray-500" />
                                <span className="text-gray-700 text-sm">交易节点</span>
                            </div>
                            <span className={`px-2 py-0.5 rounded-full text-[10px] font-bold ${isRunning ? 'bg-green-100 text-green-700' : 'bg-red-100 text-red-700'}`}>
                                {isRunning ? 'Running' : 'Stopped'}
                            </span>
                        </div>

                        <div className="flex items-center justify-between p-1.5 bg-gray-50 rounded-lg">
                            <div className="flex items-center gap-2">
                                <Database size={14} className="text-gray-500" />
                                <span className="text-gray-700 text-sm">数据同步</span>
                            </div>
                            <span className="text-gray-800 font-mono text-xs">Real-time</span>
                        </div>

                        <div className="flex items-center justify-between p-1.5 bg-gray-50 rounded-lg">
                            <div className="flex items-center gap-2">
                                <Clock size={14} className="text-gray-500" />
                                <span className="text-gray-700 text-sm">运行时长</span>
                            </div>
                            <span className="text-gray-800 font-mono text-xs">
                                {isRunning ? '48h 12m' : '--'}
                            </span>
                        </div>
                    </div>
                </div>
            </div>

            <div className="flex-1 grid grid-cols-1 xl:grid-cols-2 gap-3 min-h-0">
                {/* Active Strategy Card */}
                <div className="bg-white rounded-2xl border border-gray-200 p-3 shadow-sm flex flex-col min-h-0">
                    <h3 className="text-base font-bold text-gray-800 mb-2 flex items-center gap-2">
                        <Activity className="text-blue-500" size={16} />
                        当前运行策略
                    </h3>

                    {activeStrategy ? (
                        <div className="flex-1 flex flex-col justify-center items-center text-center p-4 bg-blue-50/50 rounded-2xl border border-blue-100 border-dashed">
                            <div className="w-16 h-16 bg-blue-100 rounded-full flex items-center justify-center text-blue-600 mb-3 animate-pulse">
                                <Database size={32} />
                            </div>
                            <h2 className="text-xl font-bold text-gray-800 mb-1">{activeStrategy.name}</h2>
                            <p className="text-gray-600 max-w-md mb-4 text-sm">{activeStrategy.description}</p>

                            <div className="flex gap-3">
                                <div className="text-left px-4 py-2 bg-white rounded-xl shadow-sm border border-gray-100">
                                    <div className="text-[10px] text-gray-400">策略 ID</div>
                                    <div className="font-mono text-gray-800 font-bold text-sm">{activeStrategy.id}</div>
                                </div>
                                <div className="text-left px-4 py-2 bg-white rounded-lg shadow-sm border border-gray-100">
                                    <div className="text-[10px] text-gray-400">运行环境</div>
                                    <div className="font-mono text-gray-800 font-bold text-sm">Python 3.8 / Qlib</div>
                                </div>
                            </div>
                        </div>
                    ) : (
                        <div className="flex-1 flex flex-col justify-center items-center text-gray-400">
                            <Server size={32} className="mb-2 opacity-50" />
                            <p className="text-sm">当前无运行中的策略</p>
                            <p className="text-xs mt-1">请前往 "策略管理" 页面启动策略</p>
                        </div>
                    )}
                </div>

                {/* Other Settings Card */}
                <div className="bg-white rounded-2xl border border-gray-200 p-3 shadow-sm flex flex-col gap-2.5">
                    <h3 className="text-base font-bold text-gray-800 flex items-center gap-2">
                        <Settings2 className="text-indigo-500" size={16} />
                        其他设置
                    </h3>

                    <div className="grid grid-cols-2 gap-3 p-2.5 rounded-xl bg-gray-50 border border-gray-100">
                        <div>
                            <div className="text-[10px] text-gray-500 mb-0.5">
                                {tradingMode === 'simulation' ? '当前模拟盘总资产' : '当前实盘总资产'}
                            </div>
                            <div className="text-base font-bold text-gray-800">
                                ¥{(modeAccount?.total_asset || 0).toLocaleString()}
                            </div>
                        </div>
                        <div>
                            <div className="text-[10px] text-gray-500 mb-0.5">
                                {tradingMode === 'simulation' ? '当前现金（模拟账户）' : '当前现金（实盘账户）'}
                            </div>
                            <div className="text-base font-bold text-gray-800">
                                {independentCash === null ? '账户未上报' : `¥${independentCash.toLocaleString()}`}
                            </div>
                        </div>
                    </div>

                    {tradingMode === 'simulation' ? (
                            <div className="p-2.5 rounded-xl border border-gray-200 bg-white">
                                <div className="flex items-center justify-between mb-2">
                                    <div className="text-xs font-semibold text-gray-700">
                                        模拟盘初始金额
                                    </div>
                                    <span className="text-[10px] text-gray-400">
                                        步长 {Math.floor(amountStep / 10000)}万
                                    </span>
                                </div>
                                <div className="relative">
                                    <span className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400 text-xs">¥</span>
                                    <input
                                        type="number"
                                        step={amountStep}
                                        min={0}
                                        value={draftInitialCash}
                                        onChange={(e) => setDraftInitialCash(Number(e.target.value || 0))}
                                        className="w-full pl-7 pr-3 py-1.5 h-9 rounded-lg border border-gray-200 focus:border-blue-500 focus:ring-1 focus:ring-blue-500 focus:outline-none text-sm font-mono bg-gray-50 transition-all"
                                        placeholder="请输入初始金额"
                                    />
                                </div>
                                <div className="mt-1.5 flex items-center justify-between text-[10px] text-gray-400">
                                    <span>每 {cooldownDays} 天仅可修改一次</span>
                                    {lastModifiedAt && !canModifyAmount && (
                                        <span className="text-amber-500">
                                            下次可修改：{nextModifyTime?.toLocaleString()}
                                        </span>
                                    )}
                                </div>
                            </div>
                        ) : (
                            <div className="p-2.5 rounded-xl border border-gray-200 bg-white">
                                <div className="text-xs font-semibold text-gray-700 mb-2">
                                    统计基准校准 (PnL Baseline)
                                </div>
                                <div className="flex gap-2">
                                    <div className="relative flex-1">
                                        <span className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400 text-xs">¥</span>
                                        <input
                                            type="number"
                                            step={1000}
                                            min={0}
                                            value={draftInitialCash}
                                            onChange={(e) => setDraftInitialCash(Number(e.target.value || 0))}
                                            className="w-full pl-7 pr-3 py-1.5 h-9 rounded-lg border border-gray-200 focus:border-blue-500 focus:ring-1 focus:ring-blue-500 focus:outline-none text-sm font-mono bg-gray-50 transition-all"
                                            placeholder="请输入实盘初始资金基准"
                                        />
                                    </div>
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
                                        className="px-3 py-1.5 h-9 bg-gray-100 hover:bg-gray-200 text-gray-600 rounded-lg text-xs font-medium transition-colors whitespace-nowrap"
                                        title="根据券商上报的总盈亏反推基准"
                                    >
                                        对齐券商
                                    </button>
                                </div>
                                <div className="mt-1.5 text-[10px] text-gray-400">
                                    修改此金额会即时改变"总盈亏"的统计起点
                                </div>
                            </div>
                        )}

                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                        <button
                            onClick={handleSaveInitialCash}
                            disabled={savingSettings || loadingSettings || (tradingMode === 'simulation' && !canModifyAmount)}
                            className="px-3 py-2 rounded-xl bg-blue-600 text-white text-sm font-medium disabled:bg-gray-300 disabled:cursor-not-allowed"
                        >
                            {savingSettings ? '保存中...' : (tradingMode === 'simulation' ? '修改初始金额' : '保存基准修正')}
                        </button>
                        {tradingMode === 'simulation' && (
                            <button
                                onClick={handleResetSimulation}
                                disabled={resettingSimulation || loadingSettings || savingSettings}
                                className="px-3 py-2 rounded-xl border border-gray-300 text-gray-700 text-sm font-medium hover:bg-gray-50 disabled:bg-gray-100 disabled:text-gray-400 disabled:cursor-not-allowed flex items-center justify-center gap-2"
                            >
                                <RefreshCw size={14} />
                                {resettingSimulation ? '重置中...' : '重置模拟盘'}
                            </button>
                        )}
                    </div>

                    <div className="text-xs text-gray-500">
                        当前统计基准：¥{configuredInitialCash.toLocaleString()}
                    </div>
                    {snapshotNotice && (
                        <div className="mt-1 text-xs font-medium text-emerald-600">
                            {snapshotNotice}
                        </div>
                    )}
                </div>
            </div>
        </div>
    );
};

export default PersonalCenter;
