import type { RealTradingStatus, TradingPrecheckItem } from '../../../../services/realTradingService';

/** 拓扑节点统一状态：绿 / 黄 / 红 / 灰，替代各处手写的 tone 三元 */
export type NodeState = 'ok' | 'warn' | 'error' | 'unknown';

export interface TopologyNode {
    key: string;
    label: string;
    state: NodeState;
    /** 节点一行关键信息 */
    summary: string;
    /** 点击展开的明细（成员检查项） */
    details: Array<{ label: string; state: NodeState; message: string }>;
}

export type RunState = 'idle' | 'starting' | 'running' | 'observing' | 'stopped' | 'error';

export const RUN_STATE_META: Record<RunState, { label: string; dot: string; banner: string }> = {
    idle: { label: '未启动', dot: 'bg-slate-300', banner: 'bg-slate-50 border-slate-200 text-slate-600' },
    starting: { label: '启动中', dot: 'bg-amber-500 animate-pulse', banner: 'bg-amber-50 border-amber-200 text-amber-700' },
    running: { label: '运行中', dot: 'bg-emerald-500 animate-pulse', banner: 'bg-emerald-50 border-emerald-200 text-emerald-700' },
    observing: { label: '观察态', dot: 'bg-sky-500 animate-pulse', banner: 'bg-sky-50 border-sky-200 text-sky-700' },
    stopped: { label: '已停止', dot: 'bg-slate-300', banner: 'bg-slate-50 border-slate-200 text-slate-500' },
    error: { label: '异常', dot: 'bg-rose-500', banner: 'bg-rose-50 border-rose-200 text-rose-700' },
};

export const nodeDot = (state: NodeState): string => {
    switch (state) {
        case 'ok': return 'bg-emerald-500';
        case 'warn': return 'bg-amber-400';
        case 'error': return 'bg-rose-500';
        default: return 'bg-slate-300';
    }
};

export const nodeText = (state: NodeState): string => {
    switch (state) {
        case 'ok': return 'text-emerald-600';
        case 'warn': return 'text-amber-600';
        case 'error': return 'text-rose-600';
        default: return 'text-slate-400';
    }
};

const itemState = (passed: boolean, detail: string): NodeState => {
    if (!passed) return 'error';
    const d = String(detail || '');
    if (d.includes('观察态') || d.includes('observe_only')) return 'warn';
    return 'ok';
};

const rank: Record<NodeState, number> = { unknown: 0, ok: 1, warn: 2, error: 3 };
const worst = (states: NodeState[]): NodeState => {
    let cur: NodeState = 'unknown';
    for (const s of states) {
        if (rank[s] > rank[cur]) cur = s;
    }
    return cur;
};

type GroupDef = { group: string; label: string; order: number };

/** 把后端 precheck 检查项归并为拓扑输入节点；未知 key 单独成组兜底展示 */
const classify = (key: string, label: string, isSim: boolean): GroupDef => {
    const k = String(key || '').toLowerCase();
    const text = `${k} ${label}`;
    if (/stream|quote|feed|行情/.test(text)) return { group: 'market', label: '实时行情', order: 0 };
    if (/model|inference|signal|模型|推理|信号|batch|批次/.test(text)) return { group: 'model', label: '推理模型', order: 1 };
    if (/redis/.test(k)) return { group: 'redis', label: 'Redis', order: 2 };
    if (/(^|_)db($|_)|postgres|数据库/.test(text)) return { group: 'db', label: '数据库', order: 3 };
    if (/sandbox|沙箱|orchestration|runner|image|容器|k8s|docker|镜像/.test(text)) {
        return isSim
            ? { group: 'sandbox', label: '沙箱进程池', order: 4 }
            : { group: 'runtime', label: '运行容器', order: 4 };
    }
    if (/websocket|连接/.test(text)) return { group: 'ws', label: 'WebSocket', order: 5 };
    return { group: `other:${key}`, label: label || key, order: 9 };
};

/** precheck 明细 → L1 输入节点带 */
export function buildInputNodes(items: TradingPrecheckItem[], isSim: boolean): TopologyNode[] {
    const groups = new Map<string, { def: GroupDef; members: TradingPrecheckItem[] }>();
    for (const item of items || []) {
        if (!item?.key) continue;
        const def = classify(item.key, item.label, isSim);
        const g = groups.get(def.group);
        if (g) g.members.push(item);
        else groups.set(def.group, { def, members: [item] });
    }
    return Array.from(groups.values())
        .sort((a, b) => a.def.order - b.def.order)
        .map(({ def, members }) => {
            const states = members.map((m) => itemState(!!m.passed, m.detail));
            const state = worst(states);
            const bad = members.filter((m) => itemState(!!m.passed, m.detail) !== 'ok');
            const summary = bad.length > 0
                ? (bad[0].detail || bad[0].label || '异常').slice(0, 24)
                : `${members.length} 项正常`;
            return {
                key: def.group,
                label: def.label,
                state,
                summary,
                details: members.map((m) => ({
                    label: m.label,
                    state: itemState(!!m.passed, m.detail),
                    message: m.detail || (m.passed ? '正常' : '未通过'),
                })),
            };
        });
}

/** status → L2 运行状态机 */
export function deriveRunState(status: RealTradingStatus | null): RunState {
    if (!status) return 'idle';
    const s = String(status.status || '').toLowerCase();
    if (s === 'starting') return 'starting';
    if (s === 'running') {
        return status.trading_permission === 'observe_only' ? 'observing' : 'running';
    }
    if (s === 'error') return 'error';
    if (s === 'stopped' || s === 'not_running') return 'stopped';
    return 'idle';
}
