import React, { useMemo, useState } from 'react';
import { Activity, Play, RefreshCw, Square } from 'lucide-react';
import { Select } from 'antd';
import { useAppSelector } from '../../../../store';
import { selectCurrentMarket } from '../../../../store/slices/uiSlice';
import type { StrategyFile } from '../../../../types/backtest/strategy';
import { useRuntimeOverview } from './hooks/useRuntimeOverview';
import type { ConsoleTradingMode } from './hooks/useRuntimeOverview';
import InputLayer from './layers/InputLayer';
import RuntimeLayer from './layers/RuntimeLayer';
import OutputLayer from './layers/OutputLayer';
import LogPanel from './layers/LogPanel';
import { RUN_STATE_META } from './topologyTypes';

interface TopologyConsoleProps {
    tenantId: string;
    userId: string;
    tradingMode?: 'real' | 'simulation';
    onDeploy: (
        strategyId: string,
        isShadow: boolean,
        strategy?: StrategyFile | null,
    ) => Promise<void>;
    onStop: () => Promise<void>;
    onOpenManualTask?: () => void;
    onOpenHistory?: () => void;
}

const MARKET_BROKER_LABEL: Record<string, string> = {
    CN: '通达信实盘交易',
    HK: '券商实盘交易（富途/老虎/IB）',
    US: '券商实盘交易（老虎/IB/富途）',
    FUTURES: '券商实盘交易（IB）',
    CRYPTO: '暂无券商通道',
};

/**
 * 拓扑控制台：输入层 → 运行层（状态+计划） → 交易记录 → 日志折叠。
 * 数据经 useRuntimeOverview 聚合：首屏 3 路并行、preflight 不进轮询、
 * 策略列表懒加载、每层独立骨架。
 */
const TopologyConsole: React.FC<TopologyConsoleProps> = ({
    tenantId,
    userId,
    tradingMode,
    onDeploy,
    onStop,
    onOpenManualTask,
    onOpenHistory,
}) => {
    const currentMarket = useAppSelector(selectCurrentMarket);
    const mode: ConsoleTradingMode = tradingMode === 'simulation' ? 'simulation' : 'real';
    const isSim = mode === 'simulation';
    const overview = useRuntimeOverview(tenantId, userId, mode, currentMarket, true);
    const { status, latestRun, defaultModel, runState, nodes, ready } = overview;

    const [selectedStrategyId, setSelectedStrategyId] = useState('');
    const [logsOpen, setLogsOpen] = useState(false);

    const strategyOptions = useMemo(
        () => overview.strategies.map((s) => ({
            value: s.id,
            label: s.is_system ? `(内置) ${s.name}` : s.name,
        })),
        [overview.strategies],
    );
    const selectedStrategy = overview.strategies.find((s) => s.id === selectedStrategyId);
    const isDeployDisabled = !selectedStrategyId || !selectedStrategy?.is_verified;
    const isRunning = runState === 'running' || runState === 'starting';
    const runMeta = RUN_STATE_META[runState];

    const defaultModelName = useMemo(() => {
        const metadata = (defaultModel?.metadata_json || {}) as Record<string, unknown>;
        const displayName = typeof metadata.display_name === 'string' ? metadata.display_name.trim() : '';
        return displayName || defaultModel?.model_id || '未配置默认模型';
    }, [defaultModel]);

    const handleDeploy = () => {
        if (!selectedStrategyId || isDeployDisabled) return;
        void onDeploy(selectedStrategyId, false, selectedStrategy || null);
    };

    return (
        <div className="h-full overflow-y-auto custom-scrollbar">
            <div className="p-4 flex flex-col gap-3 pb-12">
                {/* Header 控制条：模式 + 策略选择 + 启动/停止 */}
                <div className="bg-white rounded-2xl shadow-xs border border-slate-200/80 p-4 px-6 flex flex-col md:flex-row items-center justify-between gap-4">
                    <div className="flex-1">
                        <div className="flex items-center gap-3 mb-1.5">
                            <div className={`w-2.5 h-2.5 rounded-full ${runMeta.dot}`} />
                            <h2 className="text-lg font-bold text-slate-800">
                                {isSim ? '全自动实盘模拟控制台' : '全自动实盘交易控制台'}
                            </h2>
                            <span className={`px-2 py-0.5 rounded-full text-[10px] font-black border ${runMeta.banner}`}>
                                {runMeta.label}
                            </span>
                        </div>
                        <div className="flex items-center gap-4 text-slate-500 text-xs">
                            <span className="flex items-center gap-1.5">
                                <Activity size={13} className={isSim ? 'text-indigo-500' : 'text-rose-500'} />
                                模式: <span className={`font-bold ${isSim ? 'text-indigo-600' : 'text-rose-600'}`}>
                                    {isSim ? '实盘模拟运行' : (MARKET_BROKER_LABEL[currentMarket] || '通达信实盘交易')}
                                </span>
                            </span>
                            <span className="text-slate-200">|</span>
                            <span className="font-mono">USER: {userId}</span>
                            {overview.lastUpdatedAt && (
                                <>
                                    <span className="text-slate-200">|</span>
                                    <span className="text-slate-400">
                                        更新于 {new Date(overview.lastUpdatedAt).toLocaleTimeString()}
                                    </span>
                                </>
                            )}
                        </div>
                    </div>
                    <div className="flex items-center gap-2.5">
                        {!isRunning ? (
                            <>
                                <div className="w-60">
                                    <Select
                                        value={selectedStrategyId || undefined}
                                        onChange={(value) => setSelectedStrategyId(String(value))}
                                        onOpenChange={(open) => {
                                            if (open) void overview.ensureStrategies();
                                        }}
                                        options={strategyOptions}
                                        placeholder="选择已验证策略..."
                                        className="w-full custom-antd-select-v2"
                                        size="middle"
                                        showSearch
                                        loading={overview.strategiesLoading}
                                        notFoundContent={overview.strategiesLoading ? '加载中…' : '暂无策略'}
                                    />
                                </div>
                                <button
                                    onClick={() => void overview.ensureStrategies()}
                                    className="p-2 text-slate-400 hover:text-blue-600 border border-slate-200 rounded-xl"
                                    title="刷新策略列表"
                                >
                                    <RefreshCw size={16} className={overview.strategiesLoading ? 'animate-spin' : ''} />
                                </button>
                                <button
                                    onClick={handleDeploy}
                                    disabled={isDeployDisabled}
                                    className={`px-6 py-2 rounded-xl text-xs font-bold text-white transition-all ${isDeployDisabled ? 'bg-slate-300' : (isSim ? 'bg-indigo-500 hover:bg-indigo-600' : 'bg-blue-600 hover:bg-blue-700')}`}
                                >
                                    <Play size={16} className="inline mr-1.5" />
                                    {selectedStrategy?.is_verified ? (isSim ? '开启实时模拟' : '启动模拟交易') : '未经验证'}
                                </button>
                            </>
                        ) : (
                            <button
                                onClick={() => void onStop()}
                                className="px-8 py-2.5 bg-rose-500 hover:bg-rose-600 text-white rounded-xl font-bold shadow-lg shadow-rose-100 flex items-center gap-2 text-xs"
                            >
                                <Square size={16} fill="currentColor" /> 停止运行
                            </button>
                        )}
                    </div>
                </div>

                {/* L1 输入层 */}
                <InputLayer nodes={nodes} loading={!ready.precheck} />

                {/* L2 运行层：左运行策略+参数，右下个交易日计划+任务汇报 */}
                <RuntimeLayer
                    runState={runState}
                    status={status}
                    loading={!ready.status}
                    latestRun={latestRun}
                    defaultModelName={defaultModelName}
                />

                {/* L3 交易记录（全宽） */}
                <OutputLayer
                    recentOrders={overview.recentOrders}
                    ordersLoading={!overview.ordersReady}
                    onOpenHistory={onOpenHistory}
                    onOpenManualTask={onOpenManualTask}
                    logsOpen={logsOpen}
                    onToggleLogs={() => setLogsOpen(!logsOpen)}
                />

                {/* L4 日志折叠 */}
                <LogPanel
                    taskId={status?.latest_hosted_task?.task_id || null}
                    open={logsOpen}
                    onToggle={() => setLogsOpen(!logsOpen)}
                />
            </div>
        </div>
    );
};

export default TopologyConsole;
