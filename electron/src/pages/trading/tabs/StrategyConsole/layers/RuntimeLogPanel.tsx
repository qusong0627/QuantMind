import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ChevronDown, Terminal, ListFilter, Pause, Play, RefreshCw } from 'lucide-react';
import type { ManualExecutionLogEntry } from '../../../../../services/realTradingService';

/** 运行流里的来源标签（后端 `SOURCE_*`，前端只做展示映射）。 */
const SOURCE_LABELS: Record<string, string> = {
    hosted_sim: '模拟托管',
    hosted_runner: '容器 runner',
    manual: '手动单',
    bootstrap: '引导',
    system: '系统',
};

interface StageOption {
    value: string;
    label: string;
}

/** 阶段过滤白名单：与后端写入点用的 stage 取值同集合，新增 stage 时同步这里。 */
export const STAGE_OPTIONS: StageOption[] = [
    { value: '', label: '全部阶段' },
    { value: 'cycle_start', label: '周期开始' },
    { value: 'cycle_end', label: '周期结束' },
    { value: 'cycle_error', label: '周期异常' },
    { value: 'order', label: '下单' },
    { value: 'no_order', label: '未下单' },
    { value: 'skip', label: '跳过' },
    { value: 'signal', label: '信号' },
    { value: 'config_update', label: '配置更新' },
    { value: 'stop', label: '停止' },
    { value: 'bootstrap', label: '引导' },
];

/** 级别配色：错误红、警告琥珀、成功绿、其余中性灰（终端视图里另有一套按级别染色）。 */
export const levelTone = (level: unknown): string => {
    const s = String(level ?? '').toLowerCase();
    if (s === 'error' || s === 'failed') return 'text-rose-600';
    if (s === 'warning' || s === 'warn') return 'text-amber-600';
    if (s === 'success') return 'text-emerald-600';
    if (s === 'debug') return 'text-slate-400';
    return 'text-slate-600';
};

export const levelRowTone = (level: unknown): string => {
    const s = String(level ?? '').toLowerCase();
    if (s === 'error' || s === 'failed') return 'bg-rose-50/60';
    if (s === 'warning' || s === 'warn') return 'bg-amber-50/50';
    return '';
};

const levelBadge = (level: unknown): { text: string; cls: string } | null => {
    const s = String(level ?? '').toLowerCase();
    if (s === 'error' || s === 'failed') return { text: 'ERR', cls: 'bg-rose-100 text-rose-700' };
    if (s === 'warning' || s === 'warn') return { text: 'WARN', cls: 'bg-amber-100 text-amber-700' };
    if (s === 'success') return { text: 'OK', cls: 'bg-emerald-100 text-emerald-700' };
    return null;
};

const fmtTime = (raw?: string): string => {
    if (!raw) return '--:--:--';
    const ms = Date.parse(raw);
    if (!Number.isFinite(ms)) return '--:--:--';
    return new Date(ms).toLocaleTimeString();
};

interface RuntimeLogPanelProps {
    /** 展开后才轮询（收起即停，避免后台空转） */
    open: boolean;
    onToggle: () => void;
    /** 运行中才轮询运行流；未运行时给出引导而不是空转 */
    isRunning: boolean;
}

/**
 * L5 运行日志流（T-RC-14/21）：回答「策略现在在做什么、为什么没做」。
 *
 * 与旧的 `LogPanel` 的关键区别：旧面板只认 `status.latest_hosted_task.task_id`，
 * 而**纯模拟托管链路不落任务行**——于是模拟盘下日志面板永远空白。本面板读
 * `{tenant}:{user}` 维度的运行流，两条链路都有。
 *
 * 「终端视图」（深底、暂停跟随）从无人引用的孤儿组件 `TradingLogs.tsx` 并入此处，
 * 避免留下第二个日志面板指向同一个空壳端点。
 */
const RuntimeLogPanel: React.FC<RuntimeLogPanelProps> = ({ open, onToggle, isRunning }) => {
    const [entries, setEntries] = useState<ManualExecutionLogEntry[]>([]);
    const [follow, setFollow] = useState(true);
    const [view, setView] = useState<'list' | 'terminal'>('list');
    const [stage, setStage] = useState('');
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);

    const cursorRef = useRef('0-0');
    const entriesRef = useRef<ManualExecutionLogEntry[]>([]);
    const followRef = useRef(true);
    const stageRef = useRef('');
    const bottomRef = useRef<HTMLDivElement>(null);
    followRef.current = follow;
    stageRef.current = stage;

    const load = useCallback(async (reset: boolean) => {
        setLoading(true);
        try {
            const { realTradingService } = await import('../../../../../services/realTradingService');
            const result = await realTradingService.getRuntimeLogs({
                afterId: reset ? '0-0' : cursorRef.current,
                limit: 200,
                stage: stageRef.current || undefined,
            });
            cursorRef.current = result.next_id || cursorRef.current;
            const fresh = Array.isArray(result.entries) ? result.entries : [];
            if (reset) {
                entriesRef.current = fresh;
            } else if (fresh.length > 0) {
                // 游标读理论上不重不漏；仍按 id 去重，防止重置竞态下重复渲染
                const seen = new Set(entriesRef.current.map((e) => e.id));
                entriesRef.current = [...entriesRef.current, ...fresh.filter((e) => !seen.has(e.id))];
            }
            setEntries(entriesRef.current);
            setError(null);
        } catch (e) {
            // 日志拉取失败不静默：面板上要看得见，否则用户以为「策略没动静」
            setError(e instanceof Error ? e.message : '日志加载失败');
        } finally {
            setLoading(false);
        }
    }, []);

    // 展开 + 运行中才轮询；暂停跟随即停止轮询（不是只停止滚动）
    useEffect(() => {
        if (!open || !isRunning || !follow) return;
        let cancelled = false;
        let timer: number | undefined;
        const tick = async (reset: boolean) => {
            if (cancelled) return;
            await load(reset);
            if (!cancelled && followRef.current) timer = window.setTimeout(() => void tick(false), 2000);
        };
        void tick(true);
        return () => {
            cancelled = true;
            if (timer) window.clearTimeout(timer);
        };
    }, [open, isRunning, follow, load]);

    // 阶段过滤变化 → 重读（游标必须复位：过滤在服务端读取后做，沿用旧游标会漏掉被滤掉区间的后续条目）
    useEffect(() => {
        if (!open) return;
        cursorRef.current = '0-0';
        entriesRef.current = [];
        setEntries([]);
        void load(true);
    }, [open, stage, load]);

    useEffect(() => {
        if (open && follow && view === 'list') {
            bottomRef.current?.scrollIntoView({ block: 'end' });
        }
    }, [entries, open, follow, view]);

    const manualRefresh = () => {
        cursorRef.current = '0-0';
        entriesRef.current = [];
        void load(true);
    };

    const terminalText = useMemo(
        () => entries.map((e) => `[${fmtTime(e.ts)}] ${String(e.level || 'info').toUpperCase()} ${e.line}`).join('\n'),
        [entries],
    );

    return (
        <section
            data-testid="runtime-log-panel"
            data-open={open ? '1' : '0'}
            data-entries={entries.length}
            className="bg-white rounded-2xl border border-slate-200/80 shadow-xs overflow-hidden"
        >
            <button
                type="button"
                onClick={onToggle}
                className="w-full px-4 py-3 flex items-center justify-between hover:bg-slate-50/60 transition-colors"
            >
                <span className="flex items-center gap-2">
                    <span className="text-[10px] font-black text-slate-400 uppercase tracking-widest">
                        Runtime Logstream
                    </span>
                    <span className="text-[11px] font-bold text-slate-500">运行日志</span>
                    {entries.length > 0 && (
                        <span className="px-1.5 py-0.5 rounded bg-slate-100 text-slate-600 text-[10px] font-bold">
                            {entries.length}
                        </span>
                    )}
                </span>
                <span className="flex items-center gap-1.5 text-[11px] font-bold text-slate-500">
                    {open ? '收起' : '展开'}
                    <ChevronDown size={14} className={`transition-transform ${open ? 'rotate-180' : ''}`} />
                </span>
            </button>

            {open && (
                <>
                    <div className="border-t border-slate-100 px-4 py-2 flex flex-wrap items-center gap-2 bg-slate-50/40">
                        <label className="flex items-center gap-1.5 text-[11px] font-bold text-slate-500">
                            <ListFilter size={12} />
                            <select
                                value={stage}
                                onChange={(e) => setStage(e.target.value)}
                                className="bg-white border border-slate-200 rounded-lg px-2 py-1 text-[11px] font-bold text-slate-700"
                            >
                                {STAGE_OPTIONS.map((o) => (
                                    <option key={o.value} value={o.value}>{o.label}</option>
                                ))}
                            </select>
                        </label>
                        <button
                            type="button"
                            onClick={() => setFollow(!follow)}
                            className={`flex items-center gap-1 px-2.5 py-1 rounded-lg text-[11px] font-bold border transition-colors ${
                                follow
                                    ? 'bg-blue-50 border-blue-200 text-blue-700'
                                    : 'bg-white border-slate-200 text-slate-500 hover:bg-slate-50'
                            }`}
                        >
                            {follow ? <Pause size={11} /> : <Play size={11} />}
                            {follow ? '跟随中' : '已暂停'}
                        </button>
                        <button
                            type="button"
                            onClick={manualRefresh}
                            className="flex items-center gap-1 px-2.5 py-1 rounded-lg text-[11px] font-bold border border-slate-200 bg-white text-slate-600 hover:bg-slate-50"
                        >
                            <RefreshCw size={11} className={loading ? 'animate-spin' : ''} />
                            刷新
                        </button>
                        <button
                            type="button"
                            onClick={() => setView(view === 'list' ? 'terminal' : 'list')}
                            className={`flex items-center gap-1 px-2.5 py-1 rounded-lg text-[11px] font-bold border transition-colors ${
                                view === 'terminal'
                                    ? 'bg-slate-800 border-slate-700 text-white'
                                    : 'bg-white border-slate-200 text-slate-600 hover:bg-slate-50'
                            }`}
                        >
                            <Terminal size={11} />
                            终端视图
                        </button>
                        {!isRunning && (
                            <span className="text-[11px] font-bold text-slate-400 ml-auto">
                                策略未运行 · 显示最近记录
                            </span>
                        )}
                    </div>

                    {view === 'terminal' ? (
                        <div className="bg-slate-900 p-4 max-h-80 overflow-y-auto custom-scrollbar">
                            {entries.length === 0 ? (
                                <div className="text-slate-500 text-xs font-mono text-center py-12">
                                    暂无运行日志
                                </div>
                            ) : (
                                <pre className="whitespace-pre-wrap text-[11px] leading-relaxed font-mono text-slate-300">
                                    {terminalText}
                                </pre>
                            )}
                        </div>
                    ) : (
                        <div className="max-h-80 overflow-y-auto custom-scrollbar divide-y divide-slate-50">
                            {error ? (
                                <div className="px-4 py-8 text-center text-xs text-rose-600 font-bold">
                                    日志加载失败：{error}
                                    <button
                                        type="button"
                                        onClick={manualRefresh}
                                        className="ml-2 underline font-bold"
                                    >
                                        重试
                                    </button>
                                </div>
                            ) : entries.length === 0 ? (
                                <div className="px-4 py-10 text-center text-xs text-slate-400">
                                    {isRunning
                                        ? '策略运行中，等待第一个周期写入日志…'
                                        : '暂无运行日志：启动策略后，每个调仓周期的进展会记录在这里'}
                                </div>
                            ) : (
                                entries.map((entry) => {
                                    const badge = levelBadge(entry.level);
                                    const source = SOURCE_LABELS[String(entry.source || '')] ?? '';
                                    return (
                                        <div
                                            key={entry.id}
                                            className={`px-4 py-1.5 flex items-start gap-2 text-[11px] hover:bg-slate-50/80 ${levelRowTone(entry.level)}`}
                                        >
                                            <span className="font-mono text-slate-400 shrink-0 pt-px">{fmtTime(entry.ts)}</span>
                                            {badge && (
                                                <span className={`px-1 py-px rounded text-[9px] font-black shrink-0 ${badge.cls}`}>
                                                    {badge.text}
                                                </span>
                                            )}
                                            <span className={`flex-1 min-w-0 whitespace-pre-wrap break-words ${levelTone(entry.level)}`}>
                                                {entry.line}
                                            </span>
                                            {source && (
                                                <span className="text-[9px] font-bold text-slate-400 shrink-0 pt-px">{source}</span>
                                            )}
                                        </div>
                                    );
                                })
                            )}
                            <div ref={bottomRef} />
                        </div>
                    )}
                </>
            )}
        </section>
    );
};

export default RuntimeLogPanel;
