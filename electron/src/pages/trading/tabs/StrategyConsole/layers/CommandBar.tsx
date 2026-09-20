import React from 'react';
import { Activity, Play, RefreshCw, Square, Wifi, WifiOff } from 'lucide-react';
import { Select } from 'antd';
import type { RealTradingStatus } from '../../../../../services/realTradingService';
import type { StrategyFile } from '../../../../../types/backtest/strategy';
import { RUN_STATE_META } from '../topologyTypes';
import type { RunState } from '../topologyTypes';
import { modeCopy } from '../../../utils/tradingModeCopy';
import type { TradingMode } from '../../../utils/tradingModeCopy';
import { describeMarketMismatch } from '../../../utils/strategyMarket';

const MARKET_LABELS: Record<string, string> = {
    CN: 'A 股',
    HK: '港股',
    US: '美股',
    CRYPTO: '区块链',
    FUTURES: '期货',
};

const MARKET_BROKER_LABEL: Record<string, string> = {
    CN: '通达信实盘交易',
    HK: '券商实盘交易（富途/老虎/IB）',
    US: '券商实盘交易（老虎/IB/富途）',
    FUTURES: '券商实盘交易（IB）',
    CRYPTO: '暂无券商通道',
};

interface CommandBarProps {
    mode: TradingMode;
    market: string;
    status: RealTradingStatus | null;
    loading: boolean;
    runState: RunState;
    lastUpdatedAt: string | null;
    userId: string;
    strategyOptions: Array<{ value: string; label: string }>;
    strategiesLoading: boolean;
    selectedStrategyId: string;
    selectedStrategy: StrategyFile | undefined;
    onSelectStrategy: (id: string) => void;
    onRefreshStrategies: () => void;
    onDeploy: () => void;
    onStop: () => void;
    /** 离开页签后仍运行 —— 由页面可见性推导，用于提示用户「你走开期间它也在跑」 */
    awayWhileRunning?: boolean;
}

/**
 * 顶部状态条（T-RC-17）：模式 + 市场 + 运行状态机 + 账户关键值 + 操作区。
 *
 * **模式文案全部取自 `tradingModeCopy`**——此前三处手写三元里有一处分支写反，
 * 实盘部署的按钮写着「启动模拟交易」，参数向导标题硬编码「模拟执行参数」。
 * 对交易台来说这不是文案瑕疵：用户会据此判断自己下的单进了哪个账户。
 */
const CommandBar: React.FC<CommandBarProps> = ({
    mode,
    market,
    status,
    loading,
    runState,
    lastUpdatedAt,
    userId,
    strategyOptions,
    strategiesLoading,
    selectedStrategyId,
    selectedStrategy,
    onSelectStrategy,
    onRefreshStrategies,
    onDeploy,
    onStop,
    awayWhileRunning,
}) => {
    const copy = modeCopy(mode);
    const runMeta = RUN_STATE_META[runState];
    const isRunning = runState === 'running' || runState === 'starting' || runState === 'config_pending';
    const portfolio = status?.portfolio;
    const marketLabel = MARKET_LABELS[market] || market;
    const mismatch = describeMarketMismatch(status?.strategy_market, status?.market);
    // 信号源可用性作为「行情/链路通不通」的外显证据
    const signalOk = !!status?.signal_source_status?.available;

    const isDeployDisabled = !selectedStrategyId || !selectedStrategy?.is_verified;

    return (
        <div
            data-testid="command-bar"
            data-run-state={runState}
            className="bg-white rounded-2xl shadow-xs border border-slate-200/80 overflow-hidden"
        >
            <div className="p-4 px-5 flex flex-col lg:flex-row items-stretch lg:items-center justify-between gap-4">
                <div className="flex-1 min-w-0">
                    <div className="flex flex-wrap items-center gap-2.5 mb-1.5">
                        <span className={`px-2 py-0.5 rounded-full text-[11px] font-black border ${copy.badgeClass}`}>
                            {copy.full}
                        </span>
                        <span className="px-2 py-0.5 rounded-full text-[11px] font-black border bg-slate-50 text-slate-700 border-slate-200">
                            {marketLabel}
                        </span>
                        <div className={`w-2 h-2 rounded-full ${runMeta.dot}`} />
                        <h2 className="text-base font-bold text-slate-800">{copy.bannerTitle}</h2>
                        <span className={`px-2 py-0.5 rounded-full text-[10px] font-black border ${runMeta.banner}`}>
                            {runMeta.label}
                        </span>
                    </div>

                    <div className="flex flex-wrap items-center gap-x-3.5 gap-y-1 text-slate-500 text-[11px] font-bold">
                        <span className="flex items-center gap-1.5">
                            <Activity size={12} className={copy.isReal ? 'text-rose-500' : 'text-sky-500'} />
                            {copy.isReal
                                ? (MARKET_BROKER_LABEL[market] || '券商通道')
                                : copy.bannerSubtitle}
                        </span>
                        <span className="text-slate-200">|</span>
                        <span className="flex items-center gap-1.5" title={signalOk ? '信号源可用' : status?.signal_source_status?.message}>
                            {signalOk
                                ? <Wifi size={12} className="text-emerald-500" />
                                : <WifiOff size={12} className="text-slate-400" />}
                            {signalOk ? '信号就绪' : '信号未就绪'}
                        </span>
                        {typeof portfolio?.position_count === 'number' && (
                            <>
                                <span className="text-slate-200">|</span>
                                <span>持仓 {portfolio.position_count} 只</span>
                            </>
                        )}
                        {status?.config_version !== undefined && status.config_version > 0 && (
                            <>
                                <span className="text-slate-200">|</span>
                                <span title="每次热更新 +1；新版本在下一个调仓周期生效">
                                    配置 v{status.config_version}
                                </span>
                            </>
                        )}
                        <span className="text-slate-200">|</span>
                        <span className="font-mono">USER: {userId}</span>
                        {lastUpdatedAt && (
                            <>
                                <span className="text-slate-200">|</span>
                                <span className="text-slate-400">
                                    更新于 {new Date(lastUpdatedAt).toLocaleTimeString()}
                                </span>
                            </>
                        )}
                    </div>

                    {mismatch && (
                        <div className="mt-1.5 text-[11px] font-bold text-amber-700">
                            {mismatch}
                        </div>
                    )}
                    {!loading && runState === 'config_pending' && runMeta.hint && (
                        <div className="mt-1.5 text-[11px] font-bold text-amber-700">{runMeta.hint}</div>
                    )}
                </div>

                <div className="flex items-center gap-2.5 shrink-0">
                    {!isRunning ? (
                        <>
                            <div className="w-56" data-testid="strategy-select">
                                <Select
                                    value={selectedStrategyId || undefined}
                                    onChange={(value) => onSelectStrategy(String(value))}
                                    onOpenChange={(open) => {
                                        if (open) onRefreshStrategies();
                                    }}
                                    options={strategyOptions}
                                    placeholder={`选择已验证策略…（${marketLabel}）`}
                                    className="w-full custom-antd-select-v2"
                                    size="middle"
                                    showSearch
                                    loading={strategiesLoading}
                                    notFoundContent={strategiesLoading ? '加载中…' : `暂无 ${marketLabel} 策略`}
                                />
                            </div>
                            <button
                                onClick={onRefreshStrategies}
                                className="p-2 text-slate-400 hover:text-blue-600 border border-slate-200 rounded-xl"
                                title="刷新策略列表"
                            >
                                <RefreshCw size={16} className={strategiesLoading ? 'animate-spin' : ''} />
                            </button>
                            <button
                                onClick={onDeploy}
                                disabled={isDeployDisabled}
                                className={`px-5 py-2 rounded-xl text-xs font-bold text-white transition-all ${
                                    isDeployDisabled
                                        ? 'bg-slate-300'
                                        : (copy.isReal ? 'bg-rose-500 hover:bg-rose-600' : 'bg-blue-600 hover:bg-blue-700')
                                }`}
                            >
                                <Play size={15} className="inline mr-1.5" />
                                {selectedStrategy?.is_verified ? copy.startButton : '未经验证'}
                            </button>
                        </>
                    ) : (
                        <button
                            data-testid="stop-strategy"
                            onClick={onStop}
                            className="px-6 py-2.5 bg-rose-500 hover:bg-rose-600 text-white rounded-xl font-bold shadow-lg shadow-rose-100 flex items-center gap-2 text-xs"
                        >
                            <Square size={15} fill="currentColor" /> {copy.stopButton}
                        </button>
                    )}
                </div>
            </div>

            {awayWhileRunning && isRunning && (
                <div className="bg-amber-50 border-t border-amber-200 px-5 py-1.5 text-[11px] font-bold text-amber-800">
                    你刚才离开过本页，策略在这期间<span className="font-black">持续运行</span>（见上方托管心跳）——离开界面不会停止策略。
                </div>
            )}
        </div>
    );
};

export default CommandBar;
