import React from 'react';
import { Skeleton } from 'antd';
import { Eye, ShieldAlert, ShieldCheck } from 'lucide-react';
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
    /** 本页签属于哪个控制台（REAL=实盘栏）。托管档位由启动时选定、不随页签变：
     *  实盘栏里托管策略却跑在沙箱档位时，必须有一句显式解释，否则「实盘运行台
     *  ＋沙箱模拟」摆在一起就是自相矛盾（2026-10-09 用户原话「有些还是模拟运行？」）。 */
    consoleMode: 'REAL' | 'SIMULATION';
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

/** 托管档位（后端 `mode`，启动时选定）的展示元数据。
 *
 *  档位回答的是「钱在哪个池子里」，是整页最要紧的一句话，所以独立成语义色
 *  chip：实盘=rose（危险）、影子=violet（中间态）、沙箱=sky（安全）。
 *  未登记的档位值按原样显示——绝不用「实盘运行」兜底，否则未知值会被渲染得
 *  比沙箱更吓人，方向就反了。 */
const TIER_META: Record<string, { label: string; sub: string; tone: string; tip: string; icon: React.ReactNode }> = {
    SIMULATION: {
        label: '沙箱模拟',
        sub: '不碰真钱',
        tone: 'bg-sky-50 text-sky-700 border-sky-200',
        tip: '策略运行档位（启动时选定，不随页签切换）：委托只落模拟账户，不碰真实资金',
        icon: <ShieldCheck size={13} />,
    },
    SHADOW: {
        label: '影子运行',
        sub: '真行情不下单',
        tone: 'bg-violet-50 text-violet-700 border-violet-200',
        tip: '策略运行档位（启动时选定，不随页签切换）：用真实行情算信号，但不向券商发单',
        icon: <Eye size={13} />,
    },
    REAL: {
        label: '实盘运行',
        sub: '真实资金',
        tone: 'bg-rose-50 text-rose-700 border-rose-200',
        tip: '策略运行档位（启动时选定，不随页签切换）：委托会进券商真实下单',
        icon: <ShieldAlert size={13} />,
    },
};

const tierMeta = (mode?: string | null) => TIER_META[String(mode ?? '')] ?? {
    label: mode ? String(mode) : '未知档位',
    sub: mode ? '未登记档位' : '后端未给出档位',
    tone: 'bg-slate-50 text-slate-600 border-slate-200',
    tip: '后端 mode 返回了界面未登记的值，按原样显示以免误读',
    icon: <ShieldAlert size={13} />,
};

/** ISO 时间戳转本地可读——后端给的是 UTC ISO，直接铺给用户看是工程噪声。 */
const fmtStamp = (iso: string): string => {
    const t = new Date(iso);
    return Number.isNaN(t.getTime()) ? iso : t.toLocaleString();
};

const Stat: React.FC<{ label: string; value: number; tone: string }> = ({ label, value, tone }) => (
    <span className="flex items-baseline gap-1.5">
        <span className="text-xs font-bold text-slate-400">{label}</span>
        <span className={`text-lg font-black tabular-nums ${tone}`}>{value}</span>
    </span>
);

/**
 * L2 运行层：状态机横幅（内嵌托管档位 chip）→ 配置生效 chip → 运行策略 + 下个
 * 交易日计划 + 任务汇报。交易记录与调仓计划已下沉到独立全宽 section。
 *
 * 2026-10-09 改版（用户：「杂乱的很、不专业、不知道重点在哪里、布局不合理」）：
 * 1. 档位从 11px 灰字升级为横幅内语义色 chip（钱进哪个池子是第一重点），
 *    实盘栏 × 沙箱档位时补一句显式解释；
 * 2. 配置版本 + 生效状态合并为一枚 chip（热更新的「新版本何时生效」一眼可见），
 *    删除 CommandBar 里重复的「配置 v{n}」；
 * 3. 删除「策略参数」卡——其 4 格（调仓周期/买卖时点/委托方式/单轮最大委托）
 *    与 L3 节奏层逐项重复，「大跌拦截|止损」条与 L4 风控层重复；
 * 4. 任务汇报改全宽横条并只在有托管任务时渲染——旧版无任务时铺 0/0/0，
 *    把「没有数据」显示成「真实的 0」。
 */
const RuntimeLayer: React.FC<RuntimeLayerProps> = ({
    runState,
    status,
    loading,
    latestRun,
    defaultModelName,
    consoleMode,
}) => {
    const meta = RUN_STATE_META[runState];
    const live = status?.live_trade_config;
    // 「有活跃策略」的唯一口径：后端要么回了策略身份，要么状态机说在跑。
    // 二者皆无时 `status.mode` 是缺省值，不能当作运行档位展示。
    const active = !!status?.strategy?.id
        || ['running', 'starting'].includes(String(status?.status || '').toLowerCase());
    const isLiveState = ['running', 'starting', 'config_pending'].includes(runState);

    const tier = tierMeta(status?.mode);
    const configVersion = status?.config_version;
    const hasConfig = typeof configVersion === 'number' && configVersion > 0;
    const configPending = runState === 'config_pending';
    const configChangedAt = status?.config_updated_at ? fmtStamp(status.config_updated_at) : null;
    const configTip = configPending
        ? `托管调度器在下一个周期读取新配置，本轮仍按原参数执行${configChangedAt ? `｜最近一次配置变更：${configChangedAt}` : ''}`
        : configChangedAt
            ? `最近一次配置变更：${configChangedAt}`
            : '每次热更新 +1；新版本在下一个调仓周期生效';

    const scheduleText = live?.schedule_type === 'weekly'
        ? (live.trade_weekdays && live.trade_weekdays.length > 0 ? `每周 ${live.trade_weekdays.join(' / ')}` : '每周执行')
        : (live?.rebalance_days ? `每 ${live.rebalance_days} 个交易日` : '-');
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

            {/* 状态机横幅 + 托管档位 chip。档位由启动时选定、不随页签切换——未运行时
                不显示（后端缺省值是 SIMULATION，照抄会变成「界面把系统默认值说成
                用户的选择」）。 */}
            <div className={`rounded-xl border px-4 py-2.5 mb-3 ${meta.banner}`}>
                <div className="flex flex-wrap items-center gap-2.5">
                    <span className={`w-2.5 h-2.5 rounded-full ${meta.dot}`} />
                    <span className="text-sm font-black">{loading && !status ? '加载中…' : meta.label}</span>
                    {runState === 'observing' && (
                        <span className="text-xs font-medium">当前无可交易信号，只跑观察链路，不自动下单</span>
                    )}
                    {status && active && (
                        <span
                            data-testid="console-tier-chip"
                            title={tier.tip}
                            className={`ml-auto inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg border text-xs font-black ${tier.tone}`}
                        >
                            {tier.icon}
                            {tier.label}
                            <span className="font-bold opacity-60">· {tier.sub}</span>
                        </span>
                    )}
                </div>
            </div>

            {/* 实盘栏 × 沙箱档位：这不是矛盾而是事实（托管档位启动时选定，本页签改不了
                它），必须解释——否则「实盘运行台 + 沙箱模拟」只会更困惑。 */}
            {consoleMode === 'REAL' && isLiveState && String(status?.mode) === 'SIMULATION' && (
                <div
                    data-testid="sandbox-tier-note"
                    className="mb-3 rounded-xl border border-sky-100 bg-sky-50/70 px-3.5 py-2 text-[11px] font-bold text-sky-800"
                >
                    本页签只是观测视图，不改变托管档位：该策略启动时选定的是沙箱档位，信号与委托只进模拟账户，不碰真钱。如需真钱执行，请先停止本策略，再在实盘页签重新启动。
                </div>
            )}

            {/* 配置生效 chip + 托管方式：热更新的「新版本什么时候生效」必须一眼可见。 */}
            {status && active && (
                <div className="mb-3 flex flex-wrap items-center gap-x-3 gap-y-1.5 text-[11px] font-bold text-slate-500">
                    {hasConfig && (
                        <span
                            data-testid="config-effect-chip"
                            title={configTip}
                            className={`inline-flex items-center px-2 py-0.5 rounded-lg border font-black ${
                                configPending
                                    ? 'bg-amber-50 text-amber-700 border-amber-200'
                                    : 'bg-emerald-50 text-emerald-700 border-emerald-200'
                            }`}
                        >
                            配置 v{configVersion} · {configPending ? '待下个周期生效' : '已生效'}
                        </span>
                    )}
                    <span title="策略由服务端调度器托管推进；关闭本页面不影响运行">
                        托管方式：{status.orchestration_mode || '进程内调度'}
                    </span>
                </div>
            )}

            {/* 两行网格：第一行 运行策略（3/5）+ 下个交易日计划（2/5），第二行 任务汇报全宽条 */}
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
                        {task && (
                            <div className="lg:col-span-5 rounded-2xl border border-slate-200 bg-white px-4 py-3">
                                <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
                                    <span className="text-sm font-black text-slate-700">任务汇报</span>
                                    <span className={`px-2.5 py-0.5 rounded-full text-xs font-black border ${taskTone(task.status)}`}>
                                        {taskLabel(task.status)}
                                    </span>
                                    <div className="ml-auto flex items-center gap-6">
                                        <Stat label="成功" value={success} tone="text-emerald-700" />
                                        <Stat label="失败" value={failed} tone={failed > 0 ? 'text-rose-700' : 'text-slate-400'} />
                                        <Stat label="跳过" value={skipped} tone="text-slate-600" />
                                    </div>
                                </div>
                            </div>
                        )}
                    </>
                )}
            </div>
        </section>
    );
};

export default RuntimeLayer;
