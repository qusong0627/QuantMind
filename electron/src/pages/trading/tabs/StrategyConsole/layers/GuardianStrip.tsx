import React from 'react';
import { Activity, HeartPulse, Info, ServerCog } from 'lucide-react';
import type { RealTradingStatus } from '../../../../../services/realTradingService';

interface GuardianStripProps {
    status: RealTradingStatus | null;
    loading: boolean;
}

/**
 * 循环短名（仅显示层别名，键是后端 JobSpec 的稳定 key）。
 *
 * 后端 JobSpec 的 `name` 是给体检 CLI 看的工程描述（「手动执行任务消费」「模拟盘
 * 托管调度」），直接铺在交易台上会有两个问题：一是不说人话，二是「模拟盘托管调度」
 * 出现在实盘页签里会被读成「我这单是模拟的」——而它只是那个循环组件的名字。
 * 短名回答的是交易员真正关心的那件事：哪个托管链路还活着。未登记的 key 回退原名。
 */
const SHORT_LABEL: Record<string, string> = {
    sim_hosted: '沙箱托管',
    manual_execution: '实盘托管',
    sentinel_push: '哨兵',
};

/** 心跳状态口径与体检 C07 一致（后端 `read_heartbeats` 保证同源）。 */
const STATE_META: Record<string, { label: string; cls: string }> = {
    ok: { label: '正常', cls: 'bg-emerald-50 text-emerald-700 border-emerald-200' },
    stale: { label: '心跳超时', cls: 'bg-rose-50 text-rose-700 border-rose-200' },
    off: { label: '已关闭', cls: 'bg-slate-100 text-slate-500 border-slate-200' },
    missing: { label: '无心跳', cls: 'bg-amber-50 text-amber-700 border-amber-200' },
};

const fmtAge = (age: number | null | undefined): string => {
    if (age === null || age === undefined || !Number.isFinite(Number(age))) return '—';
    const s = Number(age);
    if (s < 60) return `${s}s 前`;
    if (s < 3600) return `${Math.floor(s / 60)}m 前`;
    return `${(s / 3600).toFixed(1)}h 前`;
};

/**
 * 守护条（T-RC-20）：常驻回答用户那句「关闭页面会不会停」。
 *
 * 不停留在一句承诺上，而是把**证据**摆在旁边：托管循环的心跳年龄。心跳来自后端
 * `scheduler_registry`，与体检 C07 同一判定函数——所以「这里显示活着、体检报死」
 * 这种自相矛盾不可能出现。
 */
const GuardianStrip: React.FC<GuardianStripProps> = ({ status, loading }) => {
    const schedulers = Array.isArray(status?.schedulers) ? status!.schedulers! : [];
    const cycle = status?.latest_cycle;
    const running = String(status?.status || '').toLowerCase() === 'running';

    return (
        <div
            data-testid="guardian-strip"
            data-heartbeat-count={schedulers.length}
            className="sticky top-0 z-20 bg-white/95 backdrop-blur border-b border-slate-200 px-4 py-2 flex flex-wrap items-center gap-x-4 gap-y-1.5 text-[11px] font-bold text-slate-600"
        >
            <span className="flex items-center gap-1.5">
                <ServerCog size={13} className={running ? 'text-emerald-500' : 'text-slate-400'} />
                服务端运行中
                <span className={`px-1.5 py-0.5 rounded text-[10px] font-black border ${
                    running ? 'bg-emerald-50 text-emerald-700 border-emerald-200' : 'bg-slate-100 text-slate-500 border-slate-200'
                }`}>
                    {running ? '是' : '否'}
                </span>
            </span>

            <span className="text-slate-200">|</span>

            <span className="flex items-center gap-1.5">
                <HeartPulse size={13} className="text-slate-400" />
                托管心跳
            </span>
            {schedulers.length === 0 ? (
                <span className="text-slate-400" title="后端未提供心跳块（旧版本或采集失败）">
                    {loading ? '读取中…' : '不可用'}
                </span>
            ) : (
                <span className="flex flex-wrap items-center gap-1.5">
                    {schedulers.map((s) => {
                        const meta = STATE_META[String(s.state)] ?? STATE_META.missing;
                        return (
                            <span
                                key={s.key}
                                title={`${s.name}｜周期心跳 TTL ${s.ttl ?? '-'}s｜最近 ${fmtAge(s.age)}`}
                                className={`px-1.5 py-0.5 rounded border text-[10px] font-black ${meta.cls}`}
                            >
                                {SHORT_LABEL[String(s.key)] ?? s.name} {fmtAge(s.age)}
                            </span>
                        );
                    })}
                </span>
            )}

            <span className="text-slate-200">|</span>

            <span className="flex items-center gap-1.5">
                <Activity size={13} className="text-slate-400" />
                最近周期：{cycle?.at ? new Date(cycle.at).toLocaleTimeString() : '尚无'}
            </span>

            <span className="ml-auto flex items-center gap-1.5 text-slate-500">
                <Info size={12} />
                关闭本页面不影响运行：策略在服务端调度器中推进，本页仅作观测
            </span>
        </div>
    );
};

export default GuardianStrip;
