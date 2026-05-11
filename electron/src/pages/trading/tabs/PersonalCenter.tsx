import React, { useEffect, useMemo, useRef, useState } from 'react';
import { User, Activity, Server, Clock, Shield, Database, Settings2, RefreshCw, DownloadCloud, Link2Off, Camera, Upload, Trash2, CheckCircle2 } from 'lucide-react';
import { message, Modal } from 'antd';
import { AccountInfo, RealTradingStatus, realTradingService } from '../../../services/realTradingService';
import { strategyManagementService } from '../../../services/strategyManagementService';
import { userCenterService } from '../../../features/user-center/services/userCenterService';
import { authService } from '../../../features/auth/services/authService';
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
    const [isUnbinding, setIsUnbinding] = useState(false);
    const [unbindModalOpen, setUnbindModalOpen] = useState(false);
    const [createdAt, setCreatedAt] = useState<string | null>(null);

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
    const [loadingSettings, setLoadingSettings] = useState(false);
    const [resettingSimulation, setResettingSimulation] = useState(false);
    const [ocrModalOpen, setOcrModalOpen] = useState(false);
    const [ocrLoading, setOcrLoading] = useState(false);
    const [ocrFiles, setOcrFiles] = useState<File[]>([]);
    const [ocrResults, setOcrResults] = useState<any[]>([]);
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
            const accountResp = await realTradingService.getRuntimeAccount(userId, tenantId, runtimeMode).catch(() => null);

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
                // Real Mode: Restore settings
                const settings = await realTradingService.getRealAccountSettings();
                if (settings) {
                    const value = Number(settings.initial_equity || 0);
                    setConfiguredInitialCash(value);
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

    const modeAccount = selectedAccount;

    // handleSaveInitialCash removed as initial cash modification is deprecated.

    const handleResetSimulation = async () => {
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

    const handleUnbindQmt = async () => {
        setIsUnbinding(true);
        try {
            const result = await realTradingService.unbindQmtAgent();
            if (result.success) {
                message.success(result.message);
                // 触发全局刷新事件
                window.dispatchEvent(new CustomEvent('refresh-account-data'));
                window.dispatchEvent(new CustomEvent('refresh-strategy-status'));
            } else {
                message.error(result.message);
            }
        } catch (err: any) {
            console.error('Failed to unbind QMT agent', err);
            message.error(err.message || '解绑失败，请稍后重试');
        } finally {
            setIsUnbinding(false);
            setUnbindModalOpen(false);
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
            
            const res = await realTradingService.analyzeHoldingImages(formData);
            if (res.success) {
                setOcrResults(res.data || []);
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
            const success = await realTradingService.syncSimulationHoldings(ocrResults);
            if (success) {
                message.success('持仓同步成功，模拟账户已更新');
                setOcrModalOpen(false);
                setOcrFiles([]);
                setOcrResults([]);
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
                            <div className="font-medium text-sm text-gray-800">
                                {createdAt ? new Date(createdAt).toLocaleDateString() : '--'}
                            </div>
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
                            <div className="w-16 h-16 bg-blue-100 rounded-full flex items-center justify-center text-blue-600 mb-3">
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
                            <div className="p-2 rounded-xl border border-gray-200 bg-gray-50/50">
                                <div className="text-sm font-semibold text-gray-800 mb-1.5">
                                    模拟盘运行状态
                                </div>
                                <div className="text-xs text-gray-500 leading-relaxed">
                                    当前统计基准固定为 <span className="font-bold text-gray-700">¥{configuredInitialCash.toLocaleString()}</span>。
                                    如需重新开始，请点击下方重置按钮。重置将清空所有持仓并恢复初始现金。
                                </div>
                            </div>
                        ) : (
                            <div className="p-2 rounded-xl border border-gray-200">
                                <div className="text-sm font-semibold text-gray-800 mb-1.5">
                                    统计基准校准 (PnL Baseline)
                                </div>
                                <div className="flex gap-2">
                                    <input
                                        type="number"
                                        step={1000}
                                        min={0}
                                        value={draftInitialCash}
                                        onChange={(e) => setDraftInitialCash(Number(e.target.value || 0))}
                                        className="flex-1 px-3 py-2 rounded-xl border border-gray-300 focus:outline-none focus:ring-2 focus:ring-blue-500 text-sm font-mono"
                                        placeholder="请输入实盘初始资金基准"
                                    />
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
                                        className="px-3 py-2 bg-gray-100 hover:bg-gray-200 text-gray-600 rounded-xl text-xs font-medium transition-colors"
                                        title="根据券商上报的总盈亏反推基准"
                                    >
                                        对齐券商
                                    </button>
                                </div>
                                <div className="mt-2 text-xs text-gray-500">
                                    修改此金额会即时改变"总盈亏"的统计起点。
                                </div>
                            </div>
                        )}

                        {tradingMode === 'simulation' ? (
                            <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                                <button
                                    onClick={handleResetSimulation}
                                    disabled={resettingSimulation || loadingSettings}
                                    className="px-3 py-2 rounded-xl bg-blue-600 text-white text-sm font-medium hover:bg-blue-700 transition-colors disabled:bg-gray-300 disabled:cursor-not-allowed flex items-center justify-center gap-2"
                                >
                                    <RefreshCw size={14} className={resettingSimulation ? 'animate-spin' : ''} />
                                    {resettingSimulation ? '重置中...' : '重置模拟盘'}
                                </button>
                                <button
                                    onClick={() => setOcrModalOpen(true)}
                                    disabled={loadingSettings}
                                    className="px-3 py-2 rounded-xl border border-indigo-200 text-indigo-600 bg-indigo-50/50 text-sm font-medium hover:bg-indigo-50 transition-colors flex items-center justify-center gap-2"
                                >
                                    <Camera size={14} />
                                    持仓图片同步
                                </button>
                            </div>
                        ) : (
                            <button
                                onClick={() => setUnbindModalOpen(true)}
                                disabled={isUnbinding}
                                className="w-full px-3 py-2 rounded-xl border border-red-200 text-red-600 text-sm font-medium hover:bg-red-50 disabled:bg-gray-100 disabled:text-gray-400 disabled:cursor-not-allowed flex items-center justify-center gap-2"
                            >
                                <Link2Off size={14} />
                                一键解绑 QMT
                            </button>
                        )}

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

            {/* 解绑确认弹窗 */}
            <Modal
                title="确认解绑 QMT"
                open={unbindModalOpen}
                onCancel={() => setUnbindModalOpen(false)}
                footer={[
                    <button
                        key="cancel"
                        onClick={() => setUnbindModalOpen(false)}
                        className="px-4 py-2 rounded-xl border border-gray-300 text-gray-700 text-sm font-medium hover:bg-gray-50"
                    >
                        取消
                    </button>,
                    <button
                        key="confirm"
                        onClick={handleUnbindQmt}
                        disabled={isUnbinding}
                        className="px-4 py-2 rounded-xl bg-red-600 text-white text-sm font-medium hover:bg-red-700 disabled:bg-gray-300 disabled:cursor-not-allowed"
                    >
                        {isUnbinding ? '解绑中...' : '确认解绑'}
                    </button>,
                ]}
                centered
                width={420}
            >
                <div className="py-4">
                    <div className="flex items-start gap-3 mb-4">
                        <div className="w-10 h-10 rounded-full bg-red-100 flex items-center justify-center flex-shrink-0">
                            <Link2Off size={20} className="text-red-600" />
                        </div>
                        <div>
                            <p className="text-gray-800 font-medium">确定要解除 QMT 绑定吗？</p>
                            <p className="text-gray-500 text-sm mt-1">
                                解绑后，QMT Agent 将断开连接，需要重新启动 Agent 并绑定新设备。
                            </p>
                        </div>
                    </div>
                    <div className="bg-amber-50 border border-amber-200 rounded-xl p-3">
                        <p className="text-amber-800 text-sm">
                            <strong>注意：</strong>解绑仅删除设备绑定关系，资产快照数据将保留。如需清除资产数据，请单独操作。
                        </p>
                    </div>
                </div>
            </Modal>
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

export default PersonalCenter;
