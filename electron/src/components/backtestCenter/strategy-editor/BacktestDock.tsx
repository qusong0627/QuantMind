/**
 * 回测结果坞（策略编辑底部）。
 *
 * 交互模型参考 IDE 的底部面板：折叠时是一行「最近一次 + KPI 摘要」，展开后
 * 变成净值/月度/K线/成交/年度/日志/存档的结果面板；高度可拖拽，可一键最大化
 * 占满编辑器区（做深度复盘时不必挤在 300px 里）。
 */
import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Empty, Tooltip } from 'antd';
import {
  ChevronDown,
  ChevronUp,
  ClipboardCopy,
  Eraser,
  Loader2,
  Maximize2,
  Minimize2,
  Save,
  Square,
  Trash2,
} from 'lucide-react';
import type { StrategyLabRunResult } from '../../../features/strategy-lab/types';
import type { SavedBacktestRun } from '../../../utils/strategyEditorSavedRuns';
import BacktestResultView from './BacktestResultView';
import {
  extractSavedRunMetrics,
  formatPct,
  formatSignedPct,
  normalizeDrawdown,
  signedToneClass,
} from './resultMetrics';
import type { DockMode, DockTab, FinalResultSummary, ResultsState, RunState } from './types';

interface Props {
  mode: DockMode;
  onModeChange: (mode: DockMode) => void;
  height: number;
  onHeightChange: (height: number) => void;
  maxHeight: number;
  tab: DockTab;
  onTabChange: (tab: DockTab) => void;
  run: RunState;
  results: ResultsState;
  result: StrategyLabRunResult | null;
  symbol?: string;
  market?: string;
}

const RESULT_TABS: Array<{ key: DockTab; label: string }> = [
  { key: 'net', label: '净值' },
  { key: 'monthly', label: '月度' },
  { key: 'kline', label: 'K线' },
  { key: 'trades', label: '成交·持仓' },
  { key: 'yearly', label: '年度' },
];

const isErrorLine = (line: string) =>
  /^\[?(error|traceback|exception|fatal)\]?/i.test(line.trim()) ||
  /Traceback \(most recent call last\)/.test(line) ||
  /^[A-Za-z_]+Error:/.test(line.trim());

const formatTime = (iso: string) => {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString('zh-CN', { hour12: false });
};

const SummaryChips: React.FC<{ summary: FinalResultSummary }> = ({ summary }) => {
  const items = [...summary.metrics, ...summary.tradeStats].slice(0, 8);
  if (!items.length) return null;
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {items.map((item) => {
        const numeric = Number(String(item.value).replace('%', ''));
        const colored = item.label.includes('收益') || item.label.includes('回撤');
        return (
          <span
            key={`${item.label}-${item.value}`}
            className="inline-flex items-center gap-1 rounded-md border border-slate-200/70 bg-white px-1.5 py-0.5 text-[9.5px] text-slate-500"
          >
            {item.label}
            <b className={`font-mono font-semibold ${colored ? signedToneClass(numeric) : 'text-slate-700'}`}>
              {item.value}
            </b>
          </span>
        );
      })}
    </div>
  );
};

const RunConsole: React.FC<{ run: RunState }> = ({ run }) => {
  const [pane, setPane] = useState<'result' | 'error'>(run.errors.length > 0 ? 'error' : 'result');
  const bottomRef = useRef<HTMLDivElement>(null);
  const lines = pane === 'result' ? run.logs : run.errors;
  const count = pane === 'result' ? run.logs.length : run.errors.length;

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: 'end' });
  }, [lines.length, pane]);

  // 运行中出现错误时自动切到「错误」页（旧 AI-IDE 的 appendErrorLine 也会切 tab）
  const prevErrorCount = useRef(run.errors.length);
  useEffect(() => {
    if (run.errors.length > prevErrorCount.current) setPane('error');
    prevErrorCount.current = run.errors.length;
  }, [run.errors.length]);

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex shrink-0 items-center gap-2 px-3 py-1.5">
        <div className="flex rounded-lg bg-slate-100/80 p-0.5">
          <button
            type="button"
            onClick={() => setPane('result')}
            className={`rounded-md px-2.5 py-1 text-[10.5px] font-semibold transition-all ${
              pane === 'result' ? 'bg-white text-slate-900 shadow-sm' : 'text-slate-500 hover:text-slate-700'
            }`}
          >
            输出 {run.logs.length}
          </button>
          <button
            type="button"
            onClick={() => setPane('error')}
            className={`rounded-md px-2.5 py-1 text-[10.5px] font-semibold transition-all ${
              pane === 'error' ? 'bg-white text-red-600 shadow-sm' : 'text-slate-500 hover:text-slate-700'
            }`}
          >
            错误 {run.errors.length}
          </button>
        </div>
        {run.isRunning && (
          <span className="flex items-center gap-1 text-[9.5px] text-blue-600">
            <Loader2 className="h-3 w-3 animate-spin" />
            正在执行…
          </span>
        )}
        <span className="flex-1" />
        <Tooltip title="复制当前日志">
          <button
            type="button"
            onClick={() => run.onCopyLogs(pane === 'result' ? 'result' : 'error')}
            aria-label="复制当前日志"
            className="flex h-6 w-6 items-center justify-center rounded-md text-slate-300 transition-colors hover:bg-slate-50 hover:text-slate-600"
          >
            <ClipboardCopy className="h-3 w-3" />
          </button>
        </Tooltip>
        <Tooltip title="清空日志">
          <button
            type="button"
            onClick={run.onClearLogs}
            aria-label="清空日志"
            className="flex h-6 w-6 items-center justify-center rounded-md text-slate-300 transition-colors hover:bg-slate-50 hover:text-slate-600"
          >
            <Eraser className="h-3 w-3" />
          </button>
        </Tooltip>
        <span className="font-mono text-[9px] text-slate-300">{count} 行</span>
      </div>
      <div className="se-scroll min-h-0 flex-1 overflow-auto px-2 pb-2">
        {pane === 'result' && run.finalSummary && (
          <div className="mb-1.5 rounded-lg border border-slate-100 bg-slate-50/60 px-2 py-1.5">
            <div className="mb-1 flex items-center gap-2 text-[9px] font-bold uppercase tracking-wider text-slate-400">
              本次执行摘要
              {run.finalSummary.executionTime && (
                <span className="font-mono text-[9px] font-normal text-slate-400">
                  耗时 {run.finalSummary.executionTime}
                </span>
              )}
            </div>
            <SummaryChips summary={run.finalSummary} />
          </div>
        )}
        {lines.length === 0 ? (
          <div className="flex h-full items-center justify-center text-[10.5px] text-slate-400">
            {run.isRunning ? '等待输出…' : pane === 'result' ? '暂无运行日志，按 F5 运行当前策略' : '没有错误记录'}
          </div>
        ) : (
          lines.map((line, index) => (
            <div
              key={`${index}-${line.slice(0, 16)}`}
              className={`se-log-line ${(pane === 'error' || isErrorLine(line)) ? 'se-log-error' : ''}`}
            >
              <span className="se-log-index">{index + 1}</span>
              <span className="min-w-0 flex-1">{line}</span>
            </div>
          ))
        )}
        <div ref={bottomRef} />
      </div>
    </div>
  );
};

const SavedRuns: React.FC<{
  savedRuns: SavedBacktestRun[];
  onLoadSaved: (run: SavedBacktestRun) => void;
  onDeleteSaved: (id: string) => void;
}> = ({ savedRuns, onLoadSaved, onDeleteSaved }) => {
  if (!savedRuns.length) {
    return (
      <Empty
        image={Empty.PRESENTED_IMAGE_SIMPLE}
        description={<span className="text-[11px] text-slate-400">还没有存档。回测完成后点「保存本次」。</span>}
        className="!my-6"
      />
    );
  }
  return (
    <div className="se-scroll h-full overflow-auto px-3 py-2">
      <div className="mb-1.5 text-[9px] text-slate-400">
        存档在本机浏览器（最多 20 条），正式台账仍以「回测历史」为准
      </div>
      <div className="space-y-1.5">
        {savedRuns.map((run) => {
          const metrics = extractSavedRunMetrics(run);
          return (
            <div
              key={run.id}
              className="group flex items-center gap-2 rounded-lg border border-slate-100 bg-white px-2.5 py-1.5 transition-colors hover:border-blue-200 hover:bg-blue-50/30"
            >
              <span
                className={`shrink-0 rounded px-1.5 py-0.5 text-[9px] font-bold ${
                  run.kind === 'minibt'
                    ? 'bg-emerald-50 text-emerald-600'
                    : 'bg-blue-50 text-blue-600'
                }`}
              >
                {run.kind}
              </span>
              <div className="min-w-0 flex-1">
                <div className="truncate text-[11px] font-medium text-slate-700">{run.name}</div>
                <div className="font-mono text-[9px] text-slate-400">{formatTime(run.savedAt)}</div>
              </div>
              <div className="hidden shrink-0 items-center gap-2 text-[9.5px] md:flex">
                <span className={`se-mono font-semibold ${signedToneClass(metrics.cumReturn)}`}>
                  {formatSignedPct(metrics.cumReturn)}
                </span>
                <span className="se-mono text-slate-500">夏普 {metrics.sharpe === null ? '—' : metrics.sharpe.toFixed(2)}</span>
                <span className="se-mono text-emerald-600">回撤 {formatPct(normalizeDrawdown(metrics.maxDrawdown))}</span>
              </div>
              <button
                type="button"
                onClick={() => onLoadSaved(run)}
                className="shrink-0 rounded-md border border-slate-200 px-2 py-1 text-[10px] font-medium text-slate-600 transition-colors hover:border-blue-300 hover:text-blue-600"
              >
                加载
              </button>
              <button
                type="button"
                onClick={() => onDeleteSaved(run.id)}
                className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md text-slate-300 opacity-0 transition-all hover:bg-red-50 hover:text-red-500 group-hover:opacity-100"
                title="删除存档"
              >
                <Trash2 className="h-3 w-3" />
              </button>
            </div>
          );
        })}
      </div>
    </div>
  );
};

export const BacktestDock: React.FC<Props> = ({
  mode,
  onModeChange,
  height,
  onHeightChange,
  maxHeight,
  tab,
  onTabChange,
  run,
  results,
  result,
  symbol,
  market,
}) => {
  const dragRef = useRef<{ startY: number; startHeight: number } | null>(null);
  const [dragging, setDragging] = useState(false);
  const isOpen = mode !== 'collapsed';
  const m = result?.metrics;

  useEffect(() => {
    if (!dragging) return;
    const handleMove = (event: PointerEvent) => {
      const drag = dragRef.current;
      if (!drag) return;
      const next = drag.startHeight + (drag.startY - event.clientY);
      onHeightChange(Math.max(180, Math.min(maxHeight - 140, next)));
    };
    const handleUp = () => {
      dragRef.current = null;
      setDragging(false);
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
    };
    window.addEventListener('pointermove', handleMove);
    window.addEventListener('pointerup', handleUp);
    window.addEventListener('pointercancel', handleUp);
    return () => {
      window.removeEventListener('pointermove', handleMove);
      window.removeEventListener('pointerup', handleUp);
      window.removeEventListener('pointercancel', handleUp);
    };
  }, [dragging, maxHeight, onHeightChange]);

  const tabs = useMemo(() => {
    const list: Array<{ key: DockTab; label: string; badge?: number }> = [];
    if (result) list.push(...RESULT_TABS);
    list.push({ key: 'logs', label: '运行日志', badge: run.logs.length + run.errors.length });
    list.push({ key: 'saved', label: '存档', badge: results.savedRuns.length });
    return list;
  }, [result, run.logs.length, run.errors.length, results.savedRuns.length]);

  const activeTab = tabs.some((item) => item.key === tab) ? tab : tabs[0]?.key ?? 'logs';
  const failed = result?.status === 'failed';
  const statusText = run.isRunning
    ? `运行中 ${run.progress?.percent ?? 0}%`
    : failed
      ? '回测失败'
      : result
        ? `回测完成 · ${result.elapsed_sec ? `${result.elapsed_sec.toFixed(1)}s` : result.status}`
        : run.errors.length
          ? '上次运行有错误'
          : '尚未运行';

  const openTab = (next: DockTab) => {
    onTabChange(next);
    if (mode === 'collapsed') onModeChange('open');
  };

  return (
    <div
      className="se-card se-dock relative flex shrink-0 flex-col overflow-hidden"
      style={mode === 'max' ? { flex: 1, minHeight: 0 } : { height: isOpen ? height : 44 }}
      data-testid="strategy-editor-dock"
      data-mode={mode}
    >
      {isOpen && mode !== 'max' && (
        <div
          className="se-dock-drag shrink-0"
          onPointerDown={(event) => {
            dragRef.current = { startY: event.clientY, startHeight: height };
            setDragging(true);
            document.body.style.cursor = 'ns-resize';
            document.body.style.userSelect = 'none';
          }}
          title="拖拽调整结果面板高度"
        />
      )}

      {/* 状态 / 页签条 */}
      <div className={`flex shrink-0 items-center gap-2 pl-2.5 pr-[168px] ${isOpen ? 'h-[38px] border-b border-slate-100' : 'h-[42px]'}`}>
        <span className="se-run-dot" data-state={run.isRunning ? 'running' : failed || run.errors.length ? 'error' : result ? 'done' : 'idle'} />
        <div className="order-1 flex min-w-0 items-center gap-2">
          <span className={`shrink-0 text-[10.5px] font-semibold ${run.isRunning ? 'text-blue-600' : failed ? 'text-red-600' : result ? 'text-slate-700' : 'text-slate-400'}`}>
            {statusText}
          </span>
          {!isOpen && m && !failed && (
            <span className="hidden min-w-0 items-center gap-2 text-[9.5px] lg:flex">
              <span className={`se-mono font-semibold ${signedToneClass(m.cum_return)}`}>{formatSignedPct(m.cum_return)}</span>
              <span className="text-slate-300">·</span>
              <span className="se-mono text-slate-500">夏普 {m.sharpe.toFixed(2)}</span>
              <span className="text-slate-300">·</span>
              <span className="se-mono text-emerald-600">回撤 {formatPct(normalizeDrawdown(m.max_drawdown))}</span>
            </span>
          )}
          {!isOpen && run.isRunning && run.progress?.message && (
            <span className="truncate text-[9.5px] text-slate-400">{run.progress.message}</span>
          )}
        </div>

        {isOpen && (
          <div className="order-3 se-scroll flex min-w-0 flex-1 items-center overflow-x-auto">
            {tabs.map((item) => (
              <button
                key={item.key}
                type="button"
                className="se-tab"
                data-active={activeTab === item.key}
                onClick={() => onTabChange(item.key)}
              >
                {item.label}
                {typeof item.badge === 'number' && item.badge > 0 && (
                  <span className="rounded bg-slate-100 px-1 py-0.5 font-mono text-[8.5px] font-semibold text-slate-500">
                    {item.badge}
                  </span>
                )}
              </button>
            ))}
          </div>
        )}

        <div className="order-2 flex shrink-0 items-center gap-1">
          {run.isRunning && (
            <button
              type="button"
              onClick={run.onStop}
              aria-label="停止运行"
              className="inline-flex h-6 items-center gap-1 rounded-md border border-red-100 bg-red-50 px-2 text-[10px] font-semibold text-red-600 transition-colors hover:bg-red-100"
            >
              <Square className="h-2.5 w-2.5 fill-current" />
              停止
            </button>
          )}
          {!isOpen && (
            <>
              {result && (
                <button
                  type="button"
                  onClick={() => openTab('net')}
                  className="rounded-md px-2 py-1 text-[10px] font-semibold text-blue-600 transition-colors hover:bg-blue-50"
                >
                  查看结果
                </button>
              )}
              <button
                type="button"
                onClick={() => openTab('logs')}
                className="rounded-md px-2 py-1 text-[10px] font-medium text-slate-500 transition-colors hover:bg-slate-50 hover:text-slate-700"
              >
                日志
              </button>
            </>
          )}
          {isOpen && (
            <Tooltip title="保存本次结果到本机存档">
              <button
                type="button"
                disabled={!result}
                onClick={results.onSave}
                aria-label="保存本次结果"
                className="inline-flex h-6 items-center gap-1 rounded-md border border-slate-200 px-2 text-[10px] font-medium text-slate-600 transition-colors hover:border-blue-300 hover:text-blue-600 disabled:cursor-not-allowed disabled:opacity-40"
              >
                <Save className="h-3 w-3" />
                保存本次
              </button>
            </Tooltip>
          )}
          <Tooltip title={mode === 'max' ? '还原分栏' : '最大化结果面板'}>
            <button
              type="button"
              onClick={() => onModeChange(mode === 'max' ? 'open' : 'max')}
              aria-label={mode === 'max' ? '还原分栏' : '最大化结果面板'}
              className="flex h-6 w-6 items-center justify-center rounded-md text-slate-400 transition-colors hover:bg-slate-50 hover:text-slate-700"
            >
              {mode === 'max' ? <Minimize2 className="h-3.5 w-3.5" /> : <Maximize2 className="h-3.5 w-3.5" />}
            </button>
          </Tooltip>
          <Tooltip title={isOpen ? '收起结果面板' : '展开结果面板'}>
            <button
              type="button"
              onClick={() => onModeChange(isOpen ? 'collapsed' : 'open')}
              aria-label={isOpen ? '收起结果面板' : '展开结果面板'}
              className="flex h-6 w-6 items-center justify-center rounded-md text-slate-400 transition-colors hover:bg-slate-50 hover:text-slate-700"
            >
              {isOpen ? <ChevronDown className="h-4 w-4" /> : <ChevronUp className="h-4 w-4" />}
            </button>
          </Tooltip>
        </div>
      </div>

      {/* 运行进度 */}
      {run.isRunning && (
        <div className="absolute inset-x-0 top-0 h-[2px] bg-slate-100">
          <div
            className="h-full bg-blue-500 transition-all duration-300"
            style={{ width: `${Math.max(2, run.progress?.percent ?? 0)}%` }}
          />
        </div>
      )}

      {/* 内容 */}
      {isOpen && (
        <div className="min-h-0 flex-1 pb-8">
          {activeTab === 'logs' ? (
            <RunConsole run={run} />
          ) : activeTab === 'saved' ? (
            <SavedRuns
              savedRuns={results.savedRuns}
              onLoadSaved={results.onLoadSaved}
              onDeleteSaved={results.onDeleteSaved}
            />
          ) : result ? (
            <BacktestResultView result={result} tab={activeTab} symbol={symbol} market={market} />
          ) : (
            <div className="flex h-full items-center justify-center text-[11px] text-slate-400">
              运行一次回测，净值、月度与成交会显示在这里
            </div>
          )}
        </div>
      )}
    </div>
  );
};

export default BacktestDock;
