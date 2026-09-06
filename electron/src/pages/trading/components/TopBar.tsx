import React from 'react';
import { Wallet, Wifi, Activity, Layers, Cpu, Server } from 'lucide-react';

interface AccountInfo {
    total_asset: number;
    initial_equity: number;
    day_open_equity: number;
    month_open_equity: number;
    cash: number;
    market_value: number;
    frozen: number;
    daily_pnl: number;
    daily_pnl_percent: number;
    floating_pnl: number;
    floating_pnl_percent: number;
    total_pnl: number;
    total_pnl_percent: number;
    position_ratio: number;
    position_count: number;
}

interface TopBarProps {
    accountInfo?: AccountInfo;
    isConnected: boolean;
    strategyStatus: 'running' | 'starting' | 'stopped';
    tradingMode?: 'real' | 'simulation';
    runMode?: 'REAL' | 'SHADOW' | 'SIMULATION';
    orchestrationMode?: 'docker' | 'k8s';
}

const TopBar: React.FC<TopBarProps> = ({ accountInfo, isConnected, strategyStatus, tradingMode, runMode, orchestrationMode }) => {
    const formatMoney = (val: number | undefined) => {
        if (val === undefined || (!accountInfo && val === 0)) return '0.00';
        return val.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    };

    const formatPercent = (val: number | undefined) => {
        if (val === undefined || (!accountInfo && val === 0)) return '0.00%';
        return `${(val * 100).toFixed(2)}%`;
    };

    const info = accountInfo;

    const getPnLColor = (val: number) => val > 0 ? 'text-red-600' : val < 0 ? 'text-emerald-600' : 'text-slate-800';
    const getPnLTagClass = (val: number) => val > 0
        ? 'bg-red-50 text-red-600 border-red-200'
        : (val < 0 ? 'bg-emerald-50 text-emerald-600 border-emerald-200' : 'bg-slate-100 text-slate-700 border-slate-200');

    const modeLabel = tradingMode === 'real' ? '实盘' : '模拟盘';
    const runModeLabel = runMode === 'SHADOW'
        ? '影子运行'
        : (runMode === 'REAL' ? '实盘运行' : (runMode === 'SIMULATION' ? '模拟运行' : '未启动'));
    const runModeTone = runMode === 'SHADOW' ? 'bg-purple-50 text-purple-700 border-purple-200'
        : (runMode === 'REAL' ? 'bg-blue-50 text-blue-700 border-blue-200'
            : (runMode === 'SIMULATION' ? 'bg-amber-50 text-amber-700 border-amber-200' : 'bg-slate-100 text-slate-500 border-slate-200'));

    const deployChannelLabel = runMode === 'SIMULATION'
        ? '本地沙箱'
        : (runMode === 'REAL' || runMode === 'SHADOW'
            ? (orchestrationMode === 'docker' ? 'Docker 容器' : (orchestrationMode === 'k8s' ? 'K8s 集群' : '容器节点'))
            : '待部署');
    const deployChannelTone = runMode === 'SHADOW' ? 'bg-purple-50/60 text-purple-600 border-purple-200'
        : (runMode === 'REAL' ? 'bg-blue-50/60 text-blue-600 border-blue-200'
            : (runMode === 'SIMULATION' ? 'bg-amber-50/60 text-amber-600 border-amber-200' : 'bg-slate-50 text-slate-400 border-slate-200'));

    const strategyStatusLabel = strategyStatus === 'running' ? '策略运行中' : (strategyStatus === 'starting' ? '正在启动' : '策略已停止');
    const strategyStatusColor = strategyStatus === 'running' ? 'text-emerald-500' : (strategyStatus === 'starting' ? 'text-amber-500' : 'text-slate-400');

    // 保留早期 4×2 信息密度：每张卡只消费已由 accountAdapter 归一化的账户字段，
    // 不新增接口或改变现有账户/行情刷新链路。
    const metrics = [
        {
            label: '总资产',
            hint: '账户当前总权益，含现金与持仓市值。',
            value: formatMoney(info?.total_asset),
            subLabel: '账户权益',
        },
        {
            label: '初始权益',
            hint: '统一基线口径，对应账户初始权益。',
            value: formatMoney(info?.initial_equity),
            subLabel: '收益计算基线',
        },
        {
            label: '可用资金',
            hint: '可用现金口径，冻结资金单独展示。',
            value: formatMoney(info?.cash),
            subLabel: `冻结 ${formatMoney(info?.frozen)}`,
        },
        {
            label: '持仓市值',
            hint: '当前持仓证券的实时市值汇总。',
            value: formatMoney(info?.market_value),
            subLabel: `仓位 ${formatPercent(info?.position_ratio)}`,
        },
        {
            label: '累计总盈亏',
            hint: '累计盈亏金额；副标签展示总收益率。',
            value: formatMoney(info?.total_pnl),
            subLabel: `${(info?.total_pnl || 0) > 0 ? '+' : ''}${formatPercent(info?.total_pnl_percent)}`,
            highlight: true,
            pnl: info?.total_pnl || 0,
        },
        {
            label: '今日盈亏',
            hint: '交易日口径盈亏；副标签展示日收益率。',
            value: formatMoney(info?.daily_pnl),
            subLabel: `${(info?.daily_pnl || 0) > 0 ? '+' : ''}${formatPercent(info?.daily_pnl_percent)}`,
            highlight: true,
            pnl: info?.daily_pnl || 0,
        },
        {
            label: '浮动盈亏',
            hint: '当前持仓未实现盈亏；副标签展示相对持仓市值收益率。',
            value: formatMoney(info?.floating_pnl),
            subLabel: `${(info?.floating_pnl || 0) > 0 ? '+' : ''}${formatPercent(info?.floating_pnl_percent)}`,
            highlight: true,
            pnl: info?.floating_pnl || 0,
        },
        {
            label: '持仓数量',
            hint: '当前持仓标的数量；副标签展示仓位占比。',
            value: `${info?.position_count || 0} 只`,
            subLabel: `仓位 ${formatPercent(info?.position_ratio)}`,
        },
    ];

    return (
        <div className="flex flex-col gap-2 p-4 px-6 bg-white h-full w-full min-h-0">
            {/* Header: Title, Tags, and Status Indicators */}
            <div className="flex items-center justify-between gap-3">
                <div className="flex items-center gap-2.5">
                    <div className="p-1.5 bg-blue-50 text-blue-600 rounded-xl">
                        <Wallet size={18} />
                    </div>
                    <div className="flex items-baseline gap-2">
                        <span className="text-base font-bold text-slate-800 tracking-tight">资产概览</span>
                    </div>
                    <span className="inline-flex items-center gap-1.5 px-2.5 py-1 bg-slate-50 border border-slate-200/80 rounded-full text-xs text-slate-600 font-medium">
                        <Layers size={13} className="text-slate-400" />
                        <span>{modeLabel}</span>
                    </span>
                    <span className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium border ${runModeTone}`}>
                        <Cpu size={13} />
                        <span>{runModeLabel}</span>
                    </span>
                    <span className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium border ${deployChannelTone}`}>
                        <Server size={13} />
                        <span>{deployChannelLabel}</span>
                    </span>
                </div>

                <div className="flex items-center gap-2">
                    <div className="flex items-center gap-1.5 px-2.5 py-1 bg-slate-50 border border-slate-200/80 rounded-full text-xs text-slate-600 font-medium">
                        <Wifi size={13} className={isConnected ? 'text-emerald-500 animate-pulse' : 'text-slate-300'} />
                        <span>{isConnected ? '行情已连接' : '未连接'}</span>
                    </div>
                    <div className="flex items-center gap-1.5 px-2.5 py-1 bg-slate-50 border border-slate-200/80 rounded-full text-xs text-slate-600 font-medium">
                        <Activity size={13} className={strategyStatusColor} />
                        <span>{strategyStatusLabel}</span>
                    </div>
                </div>
            </div>

            {/* 8 卡片弹性填满 30% 区域：桌面端固定 4×2，较窄屏幕自然折行。 */}
            <div className="grid grid-cols-2 sm:grid-cols-4 sm:grid-rows-2 gap-2 flex-1 min-h-0">
                {metrics.map((metric) => {
                    const pnl = metric.pnl || 0;
                    const valueClass = metric.highlight ? getPnLColor(pnl) : 'text-slate-900';
                    return (
                        <div
                            key={metric.label}
                            title={metric.hint}
                            className="flex min-h-0 h-full flex-col items-center justify-center rounded-2xl border border-slate-200 bg-slate-50/80 p-2 text-center transition-all hover:bg-white hover:shadow-xs overflow-hidden"
                        >
                            <span className="mb-1 text-[11px] font-bold tracking-wide text-slate-600">
                                {metric.label}
                            </span>
                            <span className={`font-mono text-lg font-black tracking-tight ${valueClass}`}>
                                {metric.highlight && pnl > 0 ? '+' : ''}{metric.value}
                            </span>
                            <span
                                className={metric.highlight
                                    ? `mt-1 rounded border px-1.5 py-0.5 text-[11px] font-bold ${getPnLTagClass(pnl)}`
                                    : 'mt-1 text-[11px] font-medium text-slate-500'}
                            >
                                {metric.subLabel}
                            </span>
                        </div>
                    );
                })}
            </div>
        </div>
    );
};

export default TopBar;
