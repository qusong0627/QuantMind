import React, { useCallback, useEffect, useRef, useState } from 'react';
import { ChevronDown, Square } from 'lucide-react';

interface LogPanelProps {
    taskId: string | null;
    open: boolean;
    onToggle: () => void;
}

/**
 * L4 日志折叠层：默认收起，展开后才 2s 轮询日志流，收起即停。
 */
const LogPanel: React.FC<LogPanelProps> = ({ taskId, open, onToggle }) => {
    const [lines, setLines] = useState<string[]>([]);
    const cursorRef = useRef('0-0');
    const linesRef = useRef<string[]>([]);

    const loadLogs = useCallback(async (reset: boolean) => {
        if (!taskId) return;
        try {
            const { realTradingService } = await import('../../../../../services/realTradingService');
            const result = await realTradingService.getManualExecutionLogs(taskId, reset ? '0-0' : cursorRef.current, 200);
            cursorRef.current = result.next_id || cursorRef.current;
            const fresh = (result.entries || []).map((entry) => {
                const ts = entry.ts ? new Date(entry.ts).toLocaleTimeString() : '--:--:--';
                return `[${ts}] ${entry.line}`;
            });
            const next = reset ? fresh : [...linesRef.current];
            if (!reset) {
                for (const line of fresh) {
                    if (!next.includes(line)) next.push(line);
                }
            }
            linesRef.current = next;
            setLines(next);
        } catch (e) {
            console.warn('[TopologyConsole] logs failed', e);
        }
    }, [taskId]);

    useEffect(() => {
        if (!open || !taskId) return;
        cursorRef.current = '0-0';
        linesRef.current = [];
        setLines([]);
        let cancelled = false;
        let timer: number | undefined;
        const poll = async (reset: boolean) => {
            if (cancelled) return;
            await loadLogs(reset);
            if (!cancelled) timer = window.setTimeout(() => void poll(false), 2000);
        };
        void poll(true);
        return () => {
            cancelled = true;
            if (timer) window.clearTimeout(timer);
        };
    }, [open, taskId, loadLogs]);

    return (
        <section className="bg-white rounded-2xl border border-slate-200/80 shadow-xs overflow-hidden">
            <button
                type="button"
                onClick={onToggle}
                className="w-full px-4 py-3 flex items-center justify-between hover:bg-slate-50/60 transition-colors"
            >
                <span className="text-[10px] font-black text-slate-400 uppercase tracking-widest">Execution Logstream</span>
                <span className="flex items-center gap-1.5 text-[11px] font-bold text-slate-500">
                    {open ? '收起' : '展开'}
                    <ChevronDown size={14} className={`transition-transform ${open ? 'rotate-180' : ''}`} />
                </span>
            </button>
            {open && (
                <div className="border-t border-slate-100 p-4 max-h-72 overflow-y-auto font-mono text-[10px] text-slate-600 custom-scrollbar bg-white">
                    {!taskId ? (
                        <div className="text-slate-400 text-center text-xs py-12">暂无最新任务运行日志</div>
                    ) : lines.length === 0 ? (
                        <div className="text-slate-400 animate-pulse text-center mt-20 flex items-center justify-center gap-2">
                            <Square size={12} /> Waiting for logs...
                        </div>
                    ) : (
                        lines.map((line, i) => (
                            <div key={i} className="hover:bg-slate-50 px-2 py-0.5 whitespace-pre-wrap">{line}</div>
                        ))
                    )}
                </div>
            )}
        </section>
    );
};

export default LogPanel;
