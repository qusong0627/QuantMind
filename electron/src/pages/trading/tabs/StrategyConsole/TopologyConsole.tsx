import React, { useEffect, useMemo, useRef, useState } from 'react';
import { useAppSelector } from '../../../../store';
import { selectCurrentMarket } from '../../../../store/slices/uiSlice';
import type { StrategyFile } from '../../../../types/backtest/strategy';
import { useRuntimeOverview } from './hooks/useRuntimeOverview';
import type { ConsoleTradingMode } from './hooks/useRuntimeOverview';
import { useAwayWhileRunning } from './hooks/useAwayWhileRunning';
import { PlanSection } from '../../../../features/desk/components/DeskSections';
import CommandBar from './layers/CommandBar';
import GuardianStrip from './layers/GuardianStrip';
import InputLayer from './layers/InputLayer';
import RuntimeLayer from './layers/RuntimeLayer';
import RhythmLayer from './layers/RhythmLayer';
import RiskLayer from './layers/RiskLayer';
import OutputLayer from './layers/OutputLayer';
import RuntimeLogPanel from './layers/RuntimeLogPanel';
import { sortTradingStrategies } from '../../utils/sortTradingStrategies';
import { normalizeTradingMode } from '../../utils/tradingModeCopy';
import { DangerConfirmModal } from '../../../../components/shared/compliance/DangerConfirmModal';
import { buildStopStrategyScenario, STOP_REASONS } from '../../../../components/shared/compliance/dangerAction';

interface TopologyConsoleProps {
    tenantId: string;
    userId: string;
    tradingMode?: 'real' | 'simulation';
    onDeploy: (
        strategyId: string,
        isShadow: boolean,
        strategy?: StrategyFile | null,
    ) => Promise<void>;
    onStop: (reason?: string) => Promise<void>;
    onOpenManualTask?: () => void;
    onOpenHistory?: () => void;
}

/** 判定「策略是否在跑」的唯一口径：待生效也算在跑（调度器还在推进）。 */
const isLiveState = (runState: string): boolean =>
    runState === 'running' || runState === 'starting' || runState === 'config_pending';

/**
 * 策略控制台（T-RC-17/20）——机构级实时监控台，五层拓扑 + 守护条。
 *
 * 与旧版的关键差别不是「多几个卡片」，而是三条硬约束：
 * 1. **模式不张冠李戴**：所有「模拟/实盘」字样出自 `tradingModeCopy`（旧版把
 *    isSim 的分支写反，实盘页签的按钮写着「启动模拟交易」）；
 * 2. **运行不因离开而停**：守护条常驻服务端心跳，离开过再回来有显式提示；
 * 3. **停止必二次确认并留痕**：走 `DangerConfirmModal`（内部 `recordComplianceEvent`），
 *    弹窗内选停止原因，随 `/stop` 落服务端审计与运行日志。
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
    const copyMode = normalizeTradingMode(mode);
    const overview = useRuntimeOverview(tenantId, userId, mode, currentMarket, true);
    const { status, latestRun, defaultModel, runState, nodes, ready } = overview;

    const [selectedStrategyId, setSelectedStrategyId] = useState('');
    const [logsOpen, setLogsOpen] = useState(true);
    const [stopOpen, setStopOpen] = useState(false);
    const [stopReason, setStopReason] = useState<string>(STOP_REASONS[0].value);
    const [stopping, setStopping] = useState(false);

    const isRunning = isLiveState(runState);
    // 「关闭页面不影响运行」不能只是一句承诺：记录用户是否真的离开过，回来时明示。
    const awayWhileRunning = useAwayWhileRunning(isRunning);

    const strategyOptions = useMemo(
        () => sortTradingStrategies(overview.strategies).map((s) => ({
            value: s.id,
            label: s.is_system ? `(内置) ${s.name}` : s.name,
        })),
        [overview.strategies],
    );
    const selectedStrategy = overview.strategies.find((s) => s.id === selectedStrategyId);

    const defaultModelName = useMemo(() => {
        const metadata = (defaultModel?.metadata_json || {}) as Record<string, unknown>;
        const displayName = typeof metadata.display_name === 'string' ? metadata.display_name.trim() : '';
        return displayName || defaultModel?.model_id || '未配置默认模型';
    }, [defaultModel]);

    const handleDeploy = () => {
        if (!selectedStrategyId) return;
        void onDeploy(selectedStrategyId, false, selectedStrategy || null);
    };

    const handleStopConfirmed = async () => {
        setStopping(true);
        try {
            await onStop(stopReason);
            setStopOpen(false);
        } finally {
            setStopping(false);
        }
    };

    const stopScenario = useMemo(
        () => buildStopStrategyScenario({
            mode: copyMode,
            strategyName: status?.strategy?.name,
            positionCount: status?.portfolio?.position_count ?? null,
        }),
        [copyMode, status?.strategy?.name, status?.portfolio?.position_count],
    );
    const stopReasonLabel = STOP_REASONS.find((r) => r.value === stopReason)?.label || stopReason;

    return (
        <div
            className="h-full overflow-y-auto custom-scrollbar"
            // 探针落点：模式与市场挂在根节点上，E2E 才能在「实盘页签」这个上下文里
            // 断言文案——否则只能全页扫文本，被左侧的模式切换器误伤。
            data-testid="strategy-console"
            data-mode={copyMode}
            data-market={currentMarket}
        >
            <GuardianStrip status={status} loading={!ready.status} />
            <div className="p-4 flex flex-col gap-3 pb-12">
                {/* 顶部状态条 */}
                <CommandBar
                    mode={copyMode}
                    market={currentMarket}
                    status={status}
                    loading={!ready.status}
                    runState={runState}
                    lastUpdatedAt={overview.lastUpdatedAt}
                    userId={userId}
                    strategyOptions={strategyOptions}
                    strategiesLoading={overview.strategiesLoading}
                    selectedStrategyId={selectedStrategyId}
                    selectedStrategy={selectedStrategy}
                    onSelectStrategy={setSelectedStrategyId}
                    onRefreshStrategies={() => void overview.ensureStrategies(true)}
                    onDeploy={handleDeploy}
                    onStop={() => setStopOpen(true)}
                    awayWhileRunning={awayWhileRunning}
                />

                {/* L1 输入层 */}
                <InputLayer nodes={nodes} loading={!ready.precheck} />

                {/* L2 运行层 */}
                <RuntimeLayer
                    runState={runState}
                    status={status}
                    loading={!ready.status}
                    latestRun={latestRun}
                    defaultModelName={defaultModelName}
                />

                {/* L3 节奏层：频率档位 / 调仓 / 时段 / 响应 */}
                <RhythmLayer status={status} loading={!ready.status} />

                {/* L4 风控层：生效风控值 + 风险锁 + 口径分裂告警 */}
                <RiskLayer status={status} enabled={true} refreshKey={overview.refreshTick} />

                {/* L5 输出层：调仓计划 ｜ 交易记录（窄屏自动堆叠） */}
                <div className="grid grid-cols-1 lg:grid-cols-2 gap-3 items-stretch">
                    <PlanSection />
                    <OutputLayer
                        recentOrders={overview.recentOrders}
                        ordersLoading={!overview.ordersReady}
                        onOpenHistory={onOpenHistory}
                        onOpenManualTask={onOpenManualTask}
                        logsOpen={logsOpen}
                        onToggleLogs={() => setLogsOpen(!logsOpen)}
                    />
                </div>

                {/* L5 运行日志流：两条托管链路共用同一运行维度流 */}
                <RuntimeLogPanel
                    open={logsOpen}
                    onToggle={() => setLogsOpen(!logsOpen)}
                    isRunning={isRunning}
                />
            </div>

            {/* 停止二次确认（T-RC-19）：后果文案 + 原因选择，确认后随 /stop 落审计 */}
            <DangerConfirmModal
                open={stopOpen}
                scenario={stopScenario}
                loading={stopping}
                confirmDetail={`停止原因：${stopReasonLabel}`}
                onConfirm={() => void handleStopConfirmed()}
                onCancel={() => setStopOpen(false)}
                extra={
                    <div className="pt-1.5">
                        <div className="font-bold text-slate-700 mb-1">停止原因（记入审计）</div>
                        <div className="flex flex-wrap gap-1.5">
                            {STOP_REASONS.map((r) => (
                                <button
                                    key={r.value}
                                    type="button"
                                    onClick={() => setStopReason(r.value)}
                                    className={`px-2.5 py-1 rounded-lg border text-[11px] font-bold transition-colors ${
                                        stopReason === r.value
                                            ? 'bg-rose-50 border-rose-300 text-rose-700'
                                            : 'bg-white border-slate-200 text-slate-600 hover:bg-slate-50'
                                    }`}
                                >
                                    {r.label}
                                </button>
                            ))}
                        </div>
                    </div>
                }
            />
        </div>
    );
};

export default TopologyConsole;
