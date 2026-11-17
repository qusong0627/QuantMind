import React from 'react';
import {
    Activity,
    ArrowDownRight,
    ArrowUpRight,
    Layers,
    PieChart,
    Wallet,
    Wifi,
} from 'lucide-react';
import type { DeployMode } from '../../../types/liveTrading';

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
    runMode?: DeployMode;
    orchestrationMode?: 'docker' | 'k8s';
}

/**
 * 交易页顶部账户概览（设计对齐企业版模拟盘）：
 * 深色总资产卡 + 2×2 指标卡 + 环形仓位卡，三栏栅格。
 */
const TopBar: React.FC<TopBarProps> = ({
    accountInfo,
    isConnected,
    strategyStatus,
    tradingMode,
    runMode,
    orchestrationMode,
}) => {
    const info = accountInfo;
    const isLive = tradingMode === 'real';
    const formatMoney = (value?: number) => {
        if (value === undefined) return '—';
        return value.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    };
    const formatSignedMoney = (value?: number) => {
        if (value === undefined) return '—';
        return `${value > 0 ? '+' : ''}${formatMoney(value)}`;
    };
    const formatPercent = (value?: number, signed = false) => {
        if (value === undefined) return '—';
        return `${signed && value > 0 ? '+' : ''}${(value * 100).toFixed(2)}%`;
    };
    // A 股习惯：红涨绿跌
    const pnlColor = (value: number, dark = false) => {
        if (value > 0) return dark ? 'text-[#fb7185]' : 'text-red-600';
        if (value < 0) return dark ? 'text-[#34d399]' : 'text-emerald-600';
        return dark ? 'text-white' : 'text-slate-800';
    };
    const strategyStatusLabel = strategyStatus === 'running'
        ? '策略运行中'
        : strategyStatus === 'starting'
            ? '正在启动'
            : '策略待运行';
    const strategyStatusColor = strategyStatus === 'running'
        ? 'bg-emerald-500 shadow-[0_0_0_4px_rgba(16,185,129,0.12)]'
        : strategyStatus === 'starting'
            ? 'bg-amber-400 animate-pulse'
            : 'bg-slate-300';
    const allocation = Math.max(0, Math.min(1, info?.position_ratio || 0));
    const allocationPercent = allocation * 100;
    const circumference = 2 * Math.PI * 42;
    const dailyPnl = info?.daily_pnl || 0;
    const floatingPnl = info?.floating_pnl || 0;
    const totalPnl = info?.total_pnl || 0;

    const metrics = [
        {
            label: '可用资金',
            value: formatMoney(info?.cash),
            detail: `冻结 ${formatMoney(info?.frozen)}`,
            icon: Wallet,
            tone: 'bg-indigo-50 text-indigo-600',
        },
        {
            label: '持仓市值',
            value: formatMoney(info?.market_value),
            detail: `${info?.position_count || 0} 只持仓`,
            icon: PieChart,
            tone: 'bg-cyan-50 text-cyan-600',
        },
        {
            label: '今日盈亏',
            value: formatSignedMoney(info?.daily_pnl),
            detail: formatPercent(info?.daily_pnl_percent, true),
            icon: dailyPnl >= 0 ? ArrowUpRight : ArrowDownRight,
            tone: dailyPnl > 0 ? 'bg-red-50 text-red-500' : dailyPnl < 0 ? 'bg-emerald-50 text-emerald-600' : 'bg-slate-50 text-slate-500',
            pnl: dailyPnl,
        },
        {
            label: '浮动盈亏',
            value: formatSignedMoney(info?.floating_pnl),
            detail: formatPercent(info?.floating_pnl_percent, true),
            icon: floatingPnl >= 0 ? ArrowUpRight : ArrowDownRight,
            tone: floatingPnl > 0 ? 'bg-red-50 text-red-500' : floatingPnl < 0 ? 'bg-emerald-50 text-emerald-600' : 'bg-slate-50 text-slate-500',
            pnl: floatingPnl,
        },
    ];

    return (
        <header className="relative shrink-0 overflow-hidden border-b border-slate-200/70 bg-white px-6 pb-5 pt-4">
            <div className="pointer-events-none absolute -right-20 -top-32 h-72 w-72 rounded-full bg-indigo-100/50 blur-3xl" />
            <div className="relative mb-4 flex items-center justify-between gap-4">
                <div className="flex items-center gap-3">
                    <div>
                        <div className="mb-0.5 flex items-center gap-2 text-[12px] font-extrabold uppercase tracking-[0.24em] text-slate-400">
                            <span>{isLive ? 'Live trading' : 'Paper trading'}</span>
                            <span className="h-px w-6 bg-slate-200" />
                            <span>CN</span>
                        </div>
                        <h1 className="text-xl font-black tracking-tight text-slate-900">
                            {isLive ? '实盘交易仪表盘' : '模拟交易仪表盘'}
                        </h1>
                    </div>
                    <span className="ml-1 inline-flex items-center gap-1.5 rounded-full border border-indigo-100 bg-indigo-50/70 px-2.5 py-1 text-[12px] font-bold text-indigo-600">
                        <Layers size={12} />
                        {isLive ? '实盘账户' : '模拟账户'}
                    </span>
                </div>
                <div className="flex items-center gap-2.5 text-[12px] font-semibold">
                    <div className="flex items-center gap-2 rounded-full border border-slate-200/80 bg-slate-50/80 px-3 py-1.5 text-slate-500">
                        <Wifi size={13} className={isConnected ? 'text-indigo-500' : 'text-slate-300'} />
                        {isConnected ? '数据已同步' : '等待数据'}
                    </div>
                    <div className="flex items-center gap-2 rounded-full border border-slate-200/80 bg-white px-3 py-1.5 text-slate-600 shadow-sm">
                        <span className={`h-1.5 w-1.5 rounded-full ${strategyStatusColor}`} />
                        {strategyStatusLabel}
                        {runMode === 'SIMULATION' && orchestrationMode && (
                            <span className="border-l border-slate-200 pl-2 font-mono text-[12px] uppercase text-slate-400">{orchestrationMode}</span>
                        )}
                    </div>
                </div>
            </div>

            <div className="relative grid grid-cols-1 gap-3 xl:grid-cols-[1.12fr_1.35fr_0.78fr]">
                {/* 总资产：深色主卡 */}
                <section className="relative min-h-[164px] overflow-hidden rounded-[22px] bg-[#131d31] px-6 py-5 text-white shadow-[0_16px_36px_-18px_rgba(15,23,42,0.65)]">
                    <div className="pointer-events-none absolute -right-12 -top-20 h-56 w-56 rounded-full bg-indigo-500/20 blur-2xl" />
                    <div className="pointer-events-none absolute bottom-0 right-0 h-24 w-48 bg-gradient-to-tl from-cyan-400/10 to-transparent" />
                    <div className="relative flex h-full flex-col">
                        <div className="flex items-center justify-between">
                            <span className="text-[12px] font-semibold tracking-[0.14em] text-slate-400">TOTAL EQUITY · 总资产</span>
                            <Activity size={15} className="text-indigo-300" />
                        </div>
                        <div className="mt-2 flex items-end gap-2 whitespace-nowrap">
                            <span className="mb-1.5 text-lg font-medium text-slate-400">¥</span>
                            <span className="font-mono text-[34px] font-bold leading-none tracking-[-0.045em] text-white">{formatMoney(info?.total_asset)}</span>
                        </div>
                        <div className="mt-3 flex items-center gap-2.5">
                            <span className={`font-mono text-sm font-bold ${pnlColor(totalPnl, true)}`}>{formatSignedMoney(info?.total_pnl)}</span>
                            <span className="rounded-md border border-white/10 bg-white/[0.06] px-2 py-0.5 font-mono text-[12px] text-slate-300">
                                累计 {formatPercent(info?.total_pnl_percent, true)}
                            </span>
                        </div>
                        <div className="mt-auto flex items-center justify-between border-t border-white/[0.08] pt-3 text-[12px] text-slate-400">
                            <span>初始权益 <b className="ml-1 font-mono font-medium text-slate-200">{formatMoney(info?.initial_equity)}</b></span>
                            <span className="inline-flex items-center gap-1.5"><span className="h-1 w-1 rounded-full bg-emerald-400" />{isLive ? '容器节点' : '本地沙箱'}</span>
                        </div>
                    </div>
                </section>

                {/* 资金/持仓/盈亏：2×2 指标卡 */}
                <section className="grid min-h-[164px] grid-cols-2 overflow-hidden rounded-[22px] border border-slate-200/80 bg-white shadow-[0_8px_28px_-20px_rgba(15,23,42,0.25)]">
                    {metrics.map((metric, index) => (
                        <div
                            key={metric.label}
                            className={`relative flex min-w-0 flex-col justify-center px-4 py-3 ${index % 2 === 0 ? 'border-r border-slate-100' : ''} ${index < 2 ? 'border-b border-slate-100' : ''}`}
                        >
                            <div className="flex items-center gap-2">
                                <span className={`flex h-6 w-6 items-center justify-center rounded-lg ${metric.tone}`}>
                                    <metric.icon size={13} strokeWidth={2.2} />
                                </span>
                                <span className="text-[12px] font-semibold text-slate-500">{metric.label}</span>
                            </div>
                            <div className={`mt-1.5 truncate text-center font-mono text-[17px] font-bold tracking-tight ${metric.pnl !== undefined ? pnlColor(metric.pnl) : 'text-slate-800'}`} title={metric.value}>
                                {metric.value}
                            </div>
                            <div className={`mt-0.5 text-center font-mono text-[12px] ${metric.pnl !== undefined ? pnlColor(metric.pnl) : 'text-slate-400'}`}>{metric.detail}</div>
                        </div>
                    ))}
                </section>

                {/* 资金配置：环形仓位 */}
                <section className="flex min-h-[164px] items-center gap-4 rounded-[22px] border border-slate-200/80 bg-[#f8fafc] px-5 py-4">
                    <div className="relative h-[104px] w-[104px] shrink-0">
                        <svg viewBox="0 0 100 100" className="h-full w-full -rotate-90" aria-hidden="true">
                            <circle cx="50" cy="50" r="42" fill="none" stroke="#e8edf3" strokeWidth="7" />
                            <circle
                                cx="50"
                                cy="50"
                                r="42"
                                fill="none"
                                stroke="#6366f1"
                                strokeWidth="7"
                                strokeLinecap="round"
                                strokeDasharray={circumference}
                                strokeDashoffset={circumference * (1 - allocation)}
                                className="transition-all duration-700"
                            />
                        </svg>
                        <div className="absolute inset-0 flex flex-col items-center justify-center">
                            <span className="font-mono text-xl font-black tracking-tight text-slate-800">{info ? `${allocationPercent.toFixed(1)}%` : '—'}</span>
                            <span className="mt-0.5 text-[12px] font-semibold text-slate-400">当前仓位</span>
                        </div>
                    </div>
                    <div className="min-w-0 flex-1 space-y-3">
                        <div>
                            <div className="text-[12px] font-bold uppercase tracking-[0.16em] text-slate-400">Allocation</div>
                            <div className="mt-0.5 text-sm font-extrabold text-slate-700">资金配置</div>
                        </div>
                        <div className="space-y-2 text-[12px]">
                            <div className="flex items-center justify-between gap-2">
                                <span className="flex items-center gap-1.5 text-slate-500"><i className="h-1.5 w-1.5 rounded-full bg-indigo-500" />持仓</span>
                                <b className="font-mono text-slate-700">{info ? `${allocationPercent.toFixed(1)}%` : '—'}</b>
                            </div>
                            <div className="flex items-center justify-between gap-2">
                                <span className="flex items-center gap-1.5 text-slate-500"><i className="h-1.5 w-1.5 rounded-full bg-slate-200" />现金</span>
                                <b className="font-mono text-slate-700">{info ? `${(100 - allocationPercent).toFixed(1)}%` : '—'}</b>
                            </div>
                        </div>
                    </div>
                </section>
            </div>
        </header>
    );
};

export default TopBar;
