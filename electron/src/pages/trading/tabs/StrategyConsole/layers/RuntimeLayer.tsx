import React from 'react';
import { Skeleton } from 'antd';
import type { RealTradingStatus } from '../../../../../services/realTradingService';
import type { LatestInferenceRunInfo } from '../../../../../services/modelTrainingService';
import { RUN_STATE_META } from '../topologyTypes';
import type { RunState } from '../topologyTypes';

interface RuntimeLayerProps {
    runState: RunState;
    status: RealTradingStatus | null;
    loading: boolean;
    latestRun: LatestInferenceRunInfo | null;
    defaultModelName: string;
}

const ParamCell: React.FC<{ label: string; value: string; title?: string }> = ({ label, value, title }) => (
    <div className="rounded-xl bg-slate-50/70 p-2.5 border border-slate-100/50 min-w-0">
        <div className="text-xs font-bold text-slate-500 mb-0.5">{label}</div>
        <div className="font-bold text-slate-700 text-xs truncate" title={title || value}>{value}</div>
    </div>
);

const taskTone = (value?: string | null): string => {
    const s = String(value || '').toLowerCase();
    if (s === 'completed') return 'bg-emerald-50 text-emerald-700 border-emerald-200';
    if (['running', 'dispatching', 'validating', 'queued'].includes(s)) return 'bg-blue-50 text-blue-700 border-blue-200';
    if (s === 'failed' || s === 'cancelled') return 'bg-rose-50 text-rose-700 border-rose-200';
    return 'bg-slate-50 text-slate-600 border-slate-200';
};

const taskLabel = (value?: string | null): string => {
    const s = String(value || '').toLowerCase();
    if (s === 'completed') return '已完成';
    if (s === 'running') return '执行中';
    if (s === 'dispatching') return '派发中';
    if (s === 'validating') return '校验中';
    if (s === 'queued') return '排队中';
    if (s === 'failed') return '已失败';
    if (s === 'cancelled') return '已取消';
    return value || '-';
};

/**
 * L2 运行层：左列运行策略 + 策略参数，右列下个交易日计划 + 任务汇报。
 * 交易记录已下沉到独立全宽 section，本层只保留状态与计划。
 */
const RuntimeLayer: React.FC<RuntimeLayerProps> = ({
    runState,
    status,
    loading,
    latestRun,
    defaultModelName,
}) => {
    const meta = RUN_STATE_META[runState];
    const live = status?.live_trade_config;
    const exec = status?.execution_config;

    const scheduleText = live?.schedule_type === 'weekly'
        ? (live.trade_weekdays && live.trade_weekdays.length > 0 ? `每周 ${live.trade_weekdays.join(' / ')}` : '每周执行')
        : (live?.rebalance_days ? `每 ${live.rebalance_days} 个交易日` : '-');
    const timeText = live?.sell_time && live?.buy_time ? `${live.sell_time} / ${live.buy_time}` : '-';
    const orderText = live?.order_type
        ? `${live.order_type === 'MARKET' ? '市价' : '限价'}${typeof live.max_price_deviation === 'number' ? ` / 偏离 ${(live.max_price_deviation * 100).toFixed(1)}%` : ''}`
        : '-';
    const strategyName = status?.strategy?.name || status?.strategy?.id
        || (status?.latest_hosted_task as unknown as Record<string, unknown> | null)?.strategy_name as string
        || '-';
    const progress = Number(status?.latest_hosted_task?.progress ?? NaN);
    // 有最后一次托管任务时也展示运行卡（标注已停止），避免停止后左侧空白
    const showIdleGuide = (runState === 'idle' || runState === 'stopped') && !status?.strategy && !status?.latest_hosted_task;

    // 右列数据源：最新托管任务（后端已聚合）
    const task = status?.latest_hosted_task || null;
    const result = (task?.result_json || {}) as Record<string, unknown>;
    const request = (task?.request_json || {}) as Record<string, unknown>;
    const preview = (result?.preview_summary || {}) as Record<string, unknown>;
    const execWindow = (request?.execution_window || {}) as Record<string, string | undefined>;
    const success = Number(task?.success_count ?? (result?.success_count as number) ?? 0);
    const failed = Number(task?.failed_count ?? (result?.failed_count as number) ?? 0);
    const skipped = Number(preview?.skipped_count ?? 0);
    const rawHorizon = (task as unknown as Record<string, unknown> | null)?.target_horizon_days
        ?? request?.target_horizon_days
        ?? result?.target_horizon_days
        ?? preview?.target_horizon_days;
    const horizon = typeof rawHorizon === 'number' && Number.isFinite(rawHorizon) && rawHorizon > 0
        ? rawHorizon
        : (typeof rawHorizon === 'string' && rawHorizon.trim() !== '' && Number.isFinite(Number(rawHorizon)) && Number(rawHorizon) > 0
            ? Number(rawHorizon)
            : undefined);

    return (
        <section className="bg-white rounded-2xl border border-slate-200/80 shadow-xs p-4">
            <div className="flex items-center gap-2 mb-3">
                <span className="text-[10px] font-black px-1.5 py-0.5 rounded bg-blue-50 text-blue-500 tracking-widest">RUNTIME</span>
                <h3 className="font-bold text-slate-800 text-sm">运行状态</h3>
            </div>

            {/* 状态机横幅 */}
            <div className={`rounded-xl border px-4 py-2.5 mb-3 flex items-center gap-2.5 ${meta.banner}`}>
                <span className={`w-2.5 h-2.5 rounded-full ${meta.dot}`} />
                <span className="text-sm font-black">{loading && !status ? '加载中…' : meta.label}</span>
                {runState === 'observing' && (
                    <span className="text-xs font-medium">当前无可交易信号，只跑观察链路，不自动下单</span>
                )}
                {status?.mode && (
                    <span className="ml-auto text-[11px] font-bold opacity-70">
                        {status.mode === 'SIMULATION' ? '模拟运行' : status.mode === 'SHADOW' ? '影子运行' : '实盘运行'}
                        {status.orchestration_mode ? ` · ${status.orchestration_mode}` : ''}
                    </span>
                )}
            </div>

            {/* 两行网格：同行两卡自动等高，第二行即 策略参数 vs 任务汇报 底部对齐 */}
            <div className="grid grid-cols-1 lg:grid-cols-5 gap-3 items-stretch">
                {loading && !status ? (
                    <div className="lg:col-span-5">
                        <Skeleton active paragraph={{ rows: 4 }} />
                    </div>
                ) : (
                    <>
                        {showIdleGuide ? (
                            <div className="lg:col-span-3 border border-dashed border-slate-200 rounded-xl py-10 text-center text-xs text-slate-400">
                                尚未启动策略运行时
                                <div className="mt-1 text-[11px] text-slate-300">在顶部选择已验证策略并启动，运行状态与参数将显示在这里</div>
                            </div>
                        ) : (
                            <div className="lg:col-span-3 rounded-xl border border-slate-100 p-3 text-center flex flex-col">
                                <div className="text-xs font-bold text-slate-500 mb-2">运行策略</div>
                                <div className="text-sm font-black text-slate-800 truncate" title={strategyName}>{strategyName}</div>
                                <div className="mt-2 grid grid-cols-2 gap-2">
                                    <ParamCell label="默认模型" value={defaultModelName} title={defaultModelName} />
                                    <ParamCell label="生产批次交易日" value={latestRun?.prediction_trade_date || '-'} />
                                </div>
                                {Number.isFinite(progress) && (
                                    <div className="mt-auto pt-2.5">
                                        <div className="flex justify-between text-xs font-bold text-slate-500 mb-1">
                                            <span>任务进度</span><span>{progress}%</span>
                                        </div>
                                        <div className="h-1.5 rounded-full bg-slate-100 overflow-hidden">
                                            <div
                                                className="h-full rounded-full bg-gradient-to-r from-blue-500 via-cyan-400 to-emerald-400 transition-all"
                                                style={{ width: `${Math.max(0, Math.min(100, progress))}%` }}
                                            />
                                        </div>
                                    </div>
                                )}
                            </div>
                        )}
                        <div className="lg:col-span-2 rounded-2xl border border-slate-200 bg-slate-50/40 p-4">
                            <div className="text-sm font-black text-slate-700 mb-2.5">下个交易日计划</div>
                            {!task ? (
                                <div className="text-sm text-slate-400 py-3 text-center">今日暂未触发自动化托管任务</div>
                            ) : (
                                <div className="space-y-2.5 text-sm font-bold text-slate-700">
                                    <div className="flex justify-between gap-3 items-center bg-white rounded-xl border border-slate-100 px-3.5 py-2.5">
                                        <span className="text-slate-400 font-semibold text-xs" title="策略实际买卖节奏：策略代码优先，没写才用启动器选择">调仓周期</span>
                                        <span className="text-sm font-black text-slate-800">{scheduleText}</span>
                                    </div>
                                    <div className="flex justify-between gap-3 items-center bg-white rounded-xl border border-slate-100 px-3.5 py-2.5">
                                        <span className="text-slate-400 font-semibold text-xs" title="本批模型信号的有效天数，只决定信号用到哪天，不决定买卖节奏">信号有效期</span>
                                        <span className="text-sm font-black text-slate-800">{horizon ? `${horizon} 个交易日` : '-'}</span>
                                    </div>
                                    <div className="flex justify-between gap-3 items-center bg-white rounded-xl border border-slate-100 px-3.5 py-2.5">
                                        <span className="text-slate-400 font-semibold text-xs" title="本批信号可执行的时间范围，过期需等新一批推理">信号窗口</span>
                                        <span className="text-xs font-bold text-slate-800 truncate text-right" title={`${execWindow?.start || '-'} ~ ${execWindow?.end || '-'}`}>
                                            {execWindow?.start || '-'} ~ {execWindow?.end || '-'}
                                        </span>
                                    </div>
                                    <div className="flex justify-between gap-3 items-center bg-white rounded-xl border border-slate-100 px-3.5 py-2.5">
                                        <span className="text-slate-400 font-semibold text-xs">信号批次</span>
                                        <span className="font-mono text-xs font-bold text-slate-800 truncate" title={task.run_id}>{task.prediction_trade_date || '-'}</span>
                                    </div>
                                </div>
                            )}
                        </div>
                        {!showIdleGuide && (
                            <div className="lg:col-span-3 rounded-xl border border-slate-100 p-3 text-center">
                                <div className="text-xs font-bold text-slate-500 mb-2">策略参数</div>
                                <div className="grid grid-cols-2 gap-2">
                                    <ParamCell label="调仓周期" value={scheduleText} title={scheduleText} />
                                    <ParamCell label="买卖时点" value={timeText} />
                                    <ParamCell label="委托方式" value={orderText} title={orderText} />
                                    <ParamCell
                                        label="单轮最大委托"
                                        value={typeof live?.max_orders_per_cycle === 'number' ? `${live.max_orders_per_cycle} 单/轮` : '-'}
                                    />
                                </div>
                                {exec && (
                                    <div className="mt-2 rounded-xl border border-indigo-100 bg-indigo-50/30 px-2.5 py-2 text-[11px] font-bold text-indigo-700">
                                        大跌拦截 {typeof exec.max_buy_drop === 'number' ? `${(exec.max_buy_drop * 100).toFixed(1)}%` : 'N/A'}
                                        <span className="mx-2 text-indigo-200">|</span>
                                        止损 {typeof exec.stop_loss === 'number' ? `${(exec.stop_loss * 100).toFixed(1)}%` : 'N/A'}
                                    </div>
                                )}
                            </div>
                        )}
                        <div className="lg:col-span-2 rounded-2xl border border-slate-200 bg-white p-4">
                            <div className="flex items-center justify-between mb-2.5">
                                <span className="text-sm font-black text-slate-700">任务汇报</span>
                                {task && (
                                    <span className={`px-2.5 py-0.5 rounded-full text-xs font-black border ${taskTone(task.status)}`}>
                                        {taskLabel(task.status)}
                                    </span>
                                )}
                            </div>
                            <div className="grid grid-cols-3 gap-2.5 text-center">
                                <div className="rounded-xl bg-emerald-50 border border-emerald-100 py-3 px-2">
                                    <div className="text-xs font-bold text-emerald-600/80 mb-0.5">成功</div>
                                    <div className="text-xl font-black text-emerald-700">{success}</div>
                                </div>
                                <div className="rounded-xl bg-rose-50 border border-rose-100 py-3 px-2">
                                    <div className="text-xs font-bold text-rose-600/80 mb-0.5">失败</div>
                                    <div className="text-xl font-black text-rose-700">{failed}</div>
                                </div>
                                <div className="rounded-xl bg-slate-50 border border-slate-200 py-3 px-2">
                                    <div className="text-xs font-bold text-slate-500 mb-0.5">跳过</div>
                                    <div className="text-xl font-black text-slate-700">{skipped}</div>
                                </div>
                            </div>
                        </div>
                    </>
                )}
            </div>
        </section>
    );
};

export default RuntimeLayer;
