/**
 * 深色导航轴底部的系统负载读数（2026-10-09 机构版改版）。
 *
 * 纯展示组件：数据由 AdminPage 统一轮询后下发（顶栏健康告示与这里同源，
 * 避免同一接口两个轮询器）。深色底上数值统一 admin-num（等宽数字）。
 */
import React from 'react';
import { Tooltip } from 'antd';
import { Activity } from 'lucide-react';
import type { SystemLoadSummary } from '../types';

interface AdminSystemLoadWidgetProps {
    collapsed?: boolean;
    load: SystemLoadSummary | null;
}

/** 深色底阈值色（与浅色面同名语义，亮度上调一档）。 */
const barColor = (percent: number): string => {
    if (percent < 60) return '#34d399'; // emerald-400
    if (percent < 85) return '#fbbf24'; // amber-400
    return '#fb7185'; // rose-400
};

const healthDot = (score: number): string => {
    if (score >= 80) return 'bg-emerald-400';
    if (score >= 60) return 'bg-amber-400';
    return 'bg-rose-400';
};

const Bar: React.FC<{ label: string; value: string; percent: number }> = ({ label, value, percent }) => (
    <div className="space-y-1">
        <div className="flex items-center justify-between">
            <span className="text-[10px] text-slate-500">{label}</span>
            <span className="admin-num text-[10px] font-medium text-slate-300">{value}</span>
        </div>
        <div className="h-1 w-full overflow-hidden rounded-full bg-white/10">
            <div
                className="h-full rounded-full transition-all duration-500"
                style={{ width: `${Math.min(100, Math.max(2, percent))}%`, backgroundColor: barColor(percent) }}
            />
        </div>
    </div>
);

export const AdminSystemLoadWidget: React.FC<AdminSystemLoadWidgetProps> = ({ collapsed = false, load }) => {
    const cpu = load?.workload?.cpu_percent ?? 0;
    const mem = load?.workload?.memory_percent ?? 0;
    const disk = load?.workload?.disk_percent ?? 0;
    const cpuCount = load?.workload?.cpu_count ?? 0;
    const memUsed = load?.workload?.memory_used_gb ?? 0;
    const memTotal = load?.workload?.memory_total_gb ?? 0;
    const healthScore = load?.health_score ?? 100;
    const healthy = load?.services_summary?.healthy ?? 0;
    const total = load?.services_summary?.total ?? 0;
    const uptimeDays = load?.uptime_days ?? 0;

    const detail = (
        <div className="admin-num space-y-1 p-1 text-[11px] leading-4">
            <div className="border-b border-slate-600 pb-1 font-semibold text-slate-100">宿主机负载</div>
            <div className="flex justify-between gap-6 text-slate-300">
                <span>CPU</span>
                <span>
                    {cpu}% · {cpuCount} 核
                </span>
            </div>
            <div className="flex justify-between gap-6 text-slate-300">
                <span>内存</span>
                <span>
                    {mem}% · {memUsed}G / {memTotal}G
                </span>
            </div>
            <div className="flex justify-between gap-6 text-slate-300">
                <span>数据盘</span>
                <span>{disk}%</span>
            </div>
            <div className="flex justify-between gap-6 text-slate-300">
                <span>服务</span>
                <span>
                    {healthy}/{total} 在线
                </span>
            </div>
        </div>
    );

    if (collapsed) {
        return (
            <Tooltip title={detail} placement="right">
                <div className="flex cursor-pointer flex-col items-center gap-1.5 border-t border-white/[0.06] px-2 py-3 hover:bg-white/[0.04]">
                    <div className="relative">
                        <Activity className="h-4 w-4 text-slate-400" />
                        <span
                            className={`absolute -right-0.5 -top-0.5 h-1.5 w-1.5 rounded-full ring-2 ring-[#0B1220] ${healthDot(healthScore)}`}
                        />
                    </div>
                    <span className="admin-num text-[9px] font-medium text-slate-400">{cpu}%</span>
                </div>
            </Tooltip>
        );
    }

    return (
        <div className="border-t border-white/[0.06] px-4 py-3">
            <div className="mb-2.5 flex items-center justify-between">
                <div className="flex items-center gap-1.5">
                    <span className={`h-1.5 w-1.5 rounded-full ${healthDot(healthScore)} ${load ? 'animate-pulse' : ''}`} />
                    <span className="text-[10px] font-semibold tracking-wider text-slate-400">系统负载</span>
                </div>
                <span className="admin-num text-[10px] text-slate-500">
                    {load ? `运行 ${uptimeDays} 天` : '检测中…'}
                </span>
            </div>
            <div className="space-y-2">
                <Bar label="CPU" value={`${cpu}%`} percent={cpu} />
                <Bar label="内存" value={`${mem}%`} percent={mem} />
                <Bar label="数据盘" value={`${disk}%`} percent={disk} />
            </div>
            {total > 0 && (
                <div className="mt-2.5 flex items-center justify-between border-t border-white/[0.06] pt-2">
                    <span className="text-[10px] text-slate-500">服务在线</span>
                    <span className="admin-num text-[10px] font-medium text-slate-300">
                        {healthy}/{total}
                    </span>
                </div>
            )}
        </div>
    );
};

export default AdminSystemLoadWidget;
