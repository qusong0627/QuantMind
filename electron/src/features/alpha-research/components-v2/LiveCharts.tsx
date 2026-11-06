import React, { useRef, useEffect } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from './ui/Card';
import { RealtimeMetrics, LogEntry } from '../types-v2';
import { formatNumber, formatDateTime } from '../utils-v2';
import { Activity } from 'lucide-react';

interface LiveChartsProps {
  metrics: RealtimeMetrics | null;
  logs: LogEntry[];
}

/** Detect if a log line looks like Python/code */
function isCodeLine(msg: string): boolean {
  return /^\s*(import |from |def |class |if |for |while |return |try:|except |with |print\(|# )/.test(msg)
    || /^\s*(df|result|factor|close|open|high|low|volume)\s*[=.]/.test(msg)
    || msg.includes('pd.read_parquet')
    || msg.includes('.to_parquet(')
    || msg.includes('>>> ');
}

/** Detect LLM model call lines */
function isLlmLine(msg: string): boolean {
  return msg.includes('LiteLLM') || msg.includes('Using chat model') || msg.includes('assistant:');
}

/** Detect factor-related lines */
function isFactorLine(msg: string): boolean {
  return msg.includes('factor_name') || msg.includes('Factor') || msg.includes('Persisted factor')
    || msg.includes('Extracted') || msg.includes('IC=') || msg.includes('formulation');
}

/** Highlight key parts of a log line */
function renderLogMessage(msg: string, level: LogEntry['level']) {
  const baseClass = level === 'error' ? 'text-destructive'
    : level === 'warning' ? 'text-warning'
    : level === 'success' ? 'text-green-400'
    : 'text-slate-300';

  // Multi-line code blocks
  if (msg.includes('\n') && (isCodeLine(msg) || msg.length > 200)) {
    return (
      <pre className={`${baseClass} whitespace-pre-wrap break-all text-[11px] leading-relaxed`}>
        {msg}
      </pre>
    );
  }

  // JSON blocks
  if (msg.startsWith('{') || msg.startsWith('[')) {
    try {
      const pretty = JSON.stringify(JSON.parse(msg), null, 2);
      return (
        <pre className="text-cyan-300 whitespace-pre-wrap break-all text-[11px]">
          {pretty.length > 1000 ? pretty.slice(0, 1000) + '\n... (truncated)' : pretty}
        </pre>
      );
    } catch { /* not JSON */ }
  }

  // Highlight specific patterns
  if (isLlmLine(msg)) {
    return <span className="text-violet-300">{msg}</span>;
  }
  if (isFactorLine(msg)) {
    return <span className="text-amber-300">{msg}</span>;
  }
  if (isCodeLine(msg)) {
    return <span className="text-cyan-300 font-mono">{msg}</span>;
  }

  // Default with error highlighting
  if (level === 'error') {
    return <span className="text-red-400">{msg}</span>;
  }
  return <span className={baseClass}>{msg}</span>;
}

export const LiveCharts: React.FC<LiveChartsProps> = ({
  metrics,
  logs,
}) => {
  const logContainerRef = useRef<HTMLDivElement>(null);
  const logEndRef = useRef<HTMLDivElement>(null);
  const isAutoScrollRef = useRef(true);

  // Handle manual scroll to toggle auto-scroll
  const handleScroll = () => {
    if (logContainerRef.current) {
      const { scrollHeight, clientHeight, scrollTop } = logContainerRef.current;
      const distanceToBottom = Math.abs(scrollHeight - clientHeight - scrollTop);
      isAutoScrollRef.current = distanceToBottom < 100;
    }
  };

  useEffect(() => {
    if (isAutoScrollRef.current) {
      requestAnimationFrame(() => {
        if (logContainerRef.current) {
          const { scrollHeight, clientHeight } = logContainerRef.current;
          logContainerRef.current.scrollTo({
            top: scrollHeight - clientHeight,
            behavior: 'smooth'
          });
        }
      });
    }
  }, [logs]);

  const handleMouseLeave = () => {
    if (logContainerRef.current) {
      const { scrollHeight, clientHeight, scrollTop } = logContainerRef.current;
      const distanceToBottom = Math.abs(scrollHeight - clientHeight - scrollTop);
      if (distanceToBottom < 100) {
        isAutoScrollRef.current = true;
      }
    }
  };

  const getLogMark = (level: LogEntry['level']) => {
    switch (level) {
      case 'success': return 'text-emerald-500';
      case 'error': return 'text-red-500';
      case 'warning': return 'text-amber-500';
      default: return 'text-slate-300';
    }
  };

  return (
    <div className="space-y-4">
      {/* 最佳因子指标：挖掘只产出 IC/RankIC/ICIR，收益类指标需等回补任务 */}
      {metrics && (
        <div className="flex flex-wrap items-center gap-x-5 gap-y-1 rounded-xl border border-border/60 bg-white/70 px-4 py-2 text-xs">
          <span className="flex items-center gap-1.5 text-muted-foreground">
            <Activity className="h-3.5 w-3.5 text-primary" />
            最佳因子 RankIC
          </span>
          <span className="font-mono text-sm font-bold text-primary">
            {formatNumber(metrics.rankIc, 4)}
          </span>
          {metrics.factorName && (
            <span className="truncate max-w-[16rem] text-muted-foreground" title={metrics.factorName}>
              {metrics.factorName}
            </span>
          )}
          <span className="ml-auto text-muted-foreground">
            因子数 <span className="font-mono font-semibold text-foreground">{metrics.totalFactors}</span>
          </span>
        </div>
      )}

      {/* Main Charts Grid */}
      <div className="grid grid-cols-1 lg:grid-cols-4 gap-4">

        {/* Real-time Logs (Full Width, taller for big screens) */}
        <Card className="glass card-hover animate-fade-in-left lg:col-span-4 flex flex-col" style={{ height: 'calc(var(--app-h) - 380px)', minHeight: '400px' }}>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm flex items-center justify-between">
              <span>实时日志</span>
              <span className="text-xs text-muted-foreground font-normal">
                {logs.length} 条
              </span>
            </CardTitle>
          </CardHeader>
          <CardContent className="flex-1 min-h-0 p-0 px-1 pb-1">
            <div
              ref={logContainerRef}
              onScroll={handleScroll}
              onMouseLeave={handleMouseLeave}
              className="h-full overflow-y-auto rounded-lg bg-slate-950 p-3 font-mono text-[11px] leading-[1.6] space-y-0.5 border border-slate-800 scroll-smooth"
            >
              {logs.length === 0 ? (
                <div className="flex h-full items-center justify-center text-muted-foreground text-xs">
                  等待日志输出...
                </div>
              ) : (
                <>
                  {logs.map((log) => (
                    <div key={log.id} className="flex gap-2 items-start py-px">
                      <span className="text-slate-600 shrink-0 w-14 text-right select-none">
                        {formatDateTime(log.timestamp).split(' ')[1]?.slice(0, 8) ?? ''}
                      </span>
                      <span className={`shrink-0 w-4 text-center text-[10px] leading-5 ${getLogMark(log.level)}`}>●</span>
                      <span className="flex-1 min-w-0 break-all">
                        {renderLogMessage(log.message, log.level)}
                      </span>
                    </div>
                  ))}
                  <div ref={logEndRef} className="h-px w-full" />
                </>
              )}
            </div>
          </CardContent>
        </Card>
      </div>
    </div>
  );
};
