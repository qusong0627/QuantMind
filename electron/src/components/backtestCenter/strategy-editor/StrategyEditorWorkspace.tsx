/**
 * 回测中心「策略编辑」工作台（strategyEditorMode 专用）。
 *
 * 旧实现直接复用 AI-IDE 三栏（240 导航 + 280 文件树 + 500 右栏），在 1440
 * 窗口下 Monaco 只剩 222px。这里重新规划为：
 *   头部：策略文件切换 + 语法状态 + 保存/运行
 *   主区：Monaco（整块画布）+ 318px 回测参数栏（可收起）
 *   底部：可拖拽/最大化的结果坞（净值/月度/K线/成交/年度/日志/存档）
 * 文件树、参数、结果、存档能力一个不少，只是不再同时占着横向空间。
 */
import React, { useEffect, useMemo, useRef, useState } from 'react';
import Editor from '@monaco-editor/react';
import { Tooltip } from 'antd';
import {
  AlertCircle,
  Bot,
  CheckCircle,
  ChevronDown,
  CloudUpload,
  Cpu,
  FileCode2,
  FolderOpen,
  Loader2,
  Play,
  Save,
  SquareTerminal,
  Square,
} from 'lucide-react';
import { isMinibtStrategyCode } from '../../../utils/strategyEditorParams';
import { qlibResultToStrategyLabResult } from '../../../utils/qlibToStrategyLab';
import StrategyFileSwitcher from './StrategyFileSwitcher';
import BacktestSettingsPanel from './BacktestSettingsPanel';
import BacktestDock from './BacktestDock';
import type { DockMode, DockTab, StrategyEditorWorkspaceProps } from './types';
import './strategy-editor.css';

const MONACO_OPTIONS = {
  minimap: { enabled: false },
  fontSize: 13,
  lineHeight: 21,
  fontFamily: "'JetBrains Mono', 'Fira Code', ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
  fontLigatures: true,
  scrollBeyondLastLine: false,
  automaticLayout: true,
  padding: { top: 16, bottom: 16 },
  renderLineHighlight: 'all' as const,
  cursorBlinking: 'smooth' as const,
  smoothScrolling: true,
  tabSize: 4,
  wordWrap: 'off' as const,
  overviewRulerBorder: false,
  hideCursorInOverviewRuler: true,
  bracketPairColorization: { enabled: true },
  scrollbar: { verticalScrollbarSize: 8, horizontalScrollbarSize: 8, useShadows: false },
};

const iconButtonClass =
  'flex h-[30px] items-center gap-1.5 rounded-lg border px-2.5 text-[11.5px] font-semibold transition-all';

const EmptyEditor: React.FC<{ onOpen: () => void; onCreate: () => void }> = ({ onOpen, onCreate }) => (
  <div className="flex h-full flex-col items-center justify-center gap-3 bg-[radial-gradient(circle_at_50%_35%,#fbfdff,#f5f8fc)]">
    <div className="grid h-14 w-14 place-items-center rounded-2xl border border-dashed border-slate-200 bg-white shadow-sm">
      <FileCode2 className="h-6 w-6 text-slate-300" />
    </div>
    <div className="text-center">
      <div className="text-[13px] font-semibold text-slate-700">还没有打开策略</div>
      <div className="mt-0.5 text-[11px] text-slate-400">
        从工作空间或策略中心选一个 .py，或者新建一个文件
      </div>
    </div>
    <div className="mt-1 flex items-center gap-2">
      <button
        type="button"
        onClick={onOpen}
        className="inline-flex h-8 items-center gap-1.5 rounded-lg bg-slate-900 px-3 text-[11.5px] font-semibold text-white transition-colors hover:bg-slate-800"
      >
        <FolderOpen className="h-3.5 w-3.5" />
        打开策略
      </button>
      <button
        type="button"
        onClick={onCreate}
        className="inline-flex h-8 items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 text-[11.5px] font-semibold text-slate-600 transition-colors hover:border-slate-300 hover:bg-slate-50"
      >
        新建文件
      </button>
    </div>
  </div>
);

const CreateDialog: React.FC<{
  mode: 'file' | 'folder';
  name: string;
  onNameChange: (name: string) => void;
  onConfirm: () => void;
  onCancel: () => void;
}> = ({ mode, name, onNameChange, onConfirm, onCancel }) => {
  const inputRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    inputRef.current?.focus();
  }, []);
  return (
    <div className="fixed inset-0 z-[1200] flex items-center justify-center bg-slate-900/25 backdrop-blur-[2px]">
      <div className="w-[372px] overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-2xl">
        <div className="border-b border-slate-100 px-4 py-3">
          <div className="text-[13px] font-bold text-slate-800">
            {mode === 'folder' ? '新建文件夹' : '新建策略文件'}
          </div>
          <div className="mt-0.5 text-[10.5px] text-slate-400">
            {mode === 'folder' ? '在当前目录下创建文件夹' : '文件会创建在当前工作空间目录，可省略 .py 后缀'}
          </div>
        </div>
        <div className="p-4">
          <input
            ref={inputRef}
            value={name}
            onChange={(event) => onNameChange(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') onConfirm();
              if (event.key === 'Escape') onCancel();
            }}
            placeholder={mode === 'folder' ? '文件夹名称' : '策略文件名，如 momentum_t20'}
            className="h-9 w-full rounded-lg border border-slate-200 bg-slate-50/60 px-3 text-[12px] text-slate-800 outline-none transition-colors focus:border-blue-400 focus:bg-white focus:ring-4 focus:ring-blue-500/10"
          />
        </div>
        <div className="flex justify-end gap-2 border-t border-slate-100 bg-slate-50/50 px-4 py-2.5">
          <button
            type="button"
            onClick={onCancel}
            className="h-7 rounded-md border border-slate-200 bg-white px-3 text-[11px] font-medium text-slate-600 hover:bg-slate-50"
          >
            取消
          </button>
          <button
            type="button"
            onClick={onConfirm}
            className="h-7 rounded-md bg-blue-600 px-3 text-[11px] font-semibold text-white hover:bg-blue-700"
          >
            确认创建
          </button>
        </div>
      </div>
    </div>
  );
};

export const StrategyEditorWorkspace: React.FC<StrategyEditorWorkspaceProps> = ({
  files,
  editor,
  params,
  run,
  results,
  meta,
  createDialog,
}) => {
  const [fileMenuOpen, setFileMenuOpen] = useState(false);
  const [paramsCollapsed, setParamsCollapsed] = useState(false);
  const [dockMode, setDockMode] = useState<DockMode>('collapsed');
  const [dockTab, setDockTab] = useState<DockTab>('logs');
  const [dockHeight, setDockHeight] = useState(352);
  const [bodyHeight, setBodyHeight] = useState(720);
  const [cursor, setCursor] = useState({ line: 1, column: 1 });
  const bodyRef = useRef<HTMLDivElement>(null);

  const unifiedResult = useMemo(
    () =>
      results.minibtResult ??
      (results.qlibResult ? qlibResultToStrategyLabResult(results.qlibResult) : null),
    [results.minibtResult, results.qlibResult],
  );

  const hasFile = files.tab === 'local' ? Boolean(files.selectedFile) : Boolean(files.selectedRemote);
  const currentName =
    files.tab === 'local' ? files.selectedFile?.name ?? '' : files.selectedRemote?.name ?? '';
  const currentPath =
    files.tab === 'local'
      ? `工作空间 · ${files.currentDir || '根目录'}`
      : files.selectedRemote
        ? `云端策略 · 更新 ${String(files.selectedRemote.updated_at || '—').slice(0, 10)}`
        : '策略中心';
  const engineLabel = isMinibtStrategyCode(editor.code)
    ? 'minibt'
    : String(editor.code || '').includes('STRATEGY_CONFIG')
      ? 'STRATEGY_CONFIG'
      : 'Python';

  useEffect(() => {
    const element = bodyRef.current;
    if (!element || typeof ResizeObserver === 'undefined') return;
    // 拖拽上限要用「内容盒高度」：body 的 padding-bottom 是给底部导航让位的，
    // 把 padding 算进可用高度会让结果坞拖进导航层。
    const measure = () => {
      const style = window.getComputedStyle(element);
      const padTop = Number.parseFloat(style.paddingTop) || 0;
      const padBottom = Number.parseFloat(style.paddingBottom) || 0;
      setBodyHeight(Math.max(0, element.clientHeight - padTop - padBottom));
    };
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    measure();
    return () => observer.disconnect();
  }, []);

  const prevRunningRef = useRef(run.isRunning);
  useEffect(() => {
    if (run.isRunning && !prevRunningRef.current) {
      setDockTab('logs');
      setDockMode((mode) => (mode === 'max' ? mode : 'open'));
    }
    prevRunningRef.current = run.isRunning;
  }, [run.isRunning]);

  const resultKey = unifiedResult?.run_id ?? null;
  const lastResultKeyRef = useRef<string | null>(null);
  useEffect(() => {
    if (resultKey && resultKey !== lastResultKeyRef.current) {
      lastResultKeyRef.current = resultKey;
      setDockTab('net');
      setDockMode((mode) => (mode === 'collapsed' ? 'open' : mode));
    }
  }, [resultKey]);

  const runDisabledReason = !hasFile
    ? '先选择一个策略文件'
    : editor.syntaxError
      ? `存在语法错误：${editor.syntaxError}`
      : run.isRunning
        ? '正在运行'
        : '';

  return (
    <div className="se-workspace" data-testid="strategy-editor-workspace">
      {/* ── 头部 ─────────────────────────────────────────────── */}
      <header className="flex h-[56px] shrink-0 items-center gap-3 border-b border-[#e4eaf3] bg-white/85 px-3 backdrop-blur">
        <div className="se-mark">
          <SquareTerminal className="h-4 w-4" />
        </div>
        <div className="hidden leading-tight xl:block">
          <div className="se-kicker">Strategy Workbench</div>
          <div className="text-[12.5px] font-bold tracking-tight text-slate-800">策略编辑</div>
        </div>
        <div className="mx-0.5 h-6 w-px bg-slate-200/80" />

        <StrategyFileSwitcher
          files={files}
          open={fileMenuOpen}
          onOpenChange={setFileMenuOpen}
          trigger={
            <button
              type="button"
              className="group flex h-[40px] min-w-0 max-w-[430px] items-center gap-2 rounded-[10px] border border-transparent px-2 text-left transition-colors hover:border-slate-200 hover:bg-slate-50/80"
              data-testid="strategy-file-switcher"
            >
              <FileCode2 className={`h-4 w-4 shrink-0 ${hasFile ? 'text-blue-600' : 'text-slate-300'}`} />
              <span className="flex min-w-0 flex-col leading-tight">
                <span className="flex items-center gap-1.5">
                  <span className={`truncate text-[12.5px] font-semibold ${hasFile ? 'text-slate-800' : 'text-slate-400'}`}>
                    {hasFile ? currentName : '选择策略文件'}
                  </span>
                  {editor.isDirty && hasFile && (
                    <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-amber-400" title="有未保存修改" />
                  )}
                  <span
                    className={`shrink-0 rounded border px-1 py-px text-[8.5px] font-bold ${
                      files.tab === 'local'
                        ? 'border-slate-200 bg-slate-50 text-slate-400'
                        : 'border-indigo-100 bg-indigo-50 text-indigo-500'
                    }`}
                  >
                    {files.tab === 'local' ? '本地' : '云端'}
                  </span>
                </span>
                <span className="truncate font-mono text-[9.5px] text-slate-400">
                  {hasFile ? currentPath : `${files.tab === 'local' ? '工作空间' : '策略中心'} · 点击切换`}
                </span>
              </span>
              <ChevronDown className="h-3.5 w-3.5 shrink-0 text-slate-300 transition-colors group-hover:text-slate-500" />
            </button>
          }
        />

        <span className="flex-1" />

        {/* 语法状态 */}
        {hasFile && (
          <span
            className={`inline-flex shrink-0 items-center gap-1 rounded-md border px-1.5 py-1 text-[9.5px] font-medium ${
              editor.isCheckingSyntax
                ? 'border-slate-100 bg-slate-50 text-slate-400'
                : editor.syntaxError
                  ? 'border-red-100 bg-red-50 text-red-600'
                  : 'border-emerald-100 bg-emerald-50 text-emerald-600'
            }`}
            title={editor.syntaxError || undefined}
          >
            {editor.isCheckingSyntax ? (
              <Loader2 className="h-2.5 w-2.5 animate-spin" />
            ) : editor.syntaxError ? (
              <AlertCircle className="h-2.5 w-2.5" />
            ) : (
              <CheckCircle className="h-2.5 w-2.5" />
            )}
            {editor.isCheckingSyntax ? '校验中' : editor.syntaxError ? '语法异常' : '语法正常'}
          </span>
        )}

        <div className="flex shrink-0 items-center gap-1.5">
          <Tooltip title="AI 写策略请到 QuantBot；策略编辑只做回测与复盘">
            <button
              type="button"
              onClick={meta.onOpenQuantBot}
              className={`${iconButtonClass} border-slate-200 bg-white text-slate-500 hover:border-blue-200 hover:bg-blue-50 hover:text-blue-600`}
            >
              <Bot className="h-3.5 w-3.5" />
              <span className="hidden xl:inline">AI 写策略</span>
            </button>
          </Tooltip>
          <button
            type="button"
            onClick={() => editor.onSave()}
            disabled={!hasFile || editor.isSaving}
            className={`${iconButtonClass} border-slate-200 bg-white text-slate-600 hover:border-slate-300 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-45`}
            title="保存代码 (Ctrl/⌘+S)"
          >
            {editor.isSaving ? <CheckCircle className="h-3.5 w-3.5 text-emerald-500" /> : <Save className="h-3.5 w-3.5" />}
            {editor.isSaving ? '已保存' : '保存'}
          </button>
          {run.isRunning ? (
            <button
              type="button"
              onClick={run.onStop}
              className={`${iconButtonClass} border-red-600 bg-red-600 text-white hover:bg-red-700`}
            >
              <Square className="h-3 w-3 fill-current" />
              停止
            </button>
          ) : (
            <Tooltip title={runDisabledReason || '运行当前策略 (F5)'}>
              <span>
                <button
                  type="button"
                  onClick={run.onRun}
                  disabled={!editor.canRun}
                  className={`${iconButtonClass} ${
                    editor.canRun
                      ? 'border-blue-600 bg-blue-600 text-white shadow-sm hover:bg-blue-700 active:scale-[0.99]'
                      : 'cursor-not-allowed border-slate-200 bg-slate-100 text-slate-400'
                  }`}
                  data-testid="strategy-run-button"
                >
                  <Play className="h-3 w-3 fill-current" />
                  运行
                  <span className="ml-0.5 rounded bg-white/15 px-1 font-mono text-[8.5px] font-normal">F5</span>
                </button>
              </span>
            </Tooltip>
          )}
        </div>
      </header>

      {/* ── 主体 ─────────────────────────────────────────────── */}
      <div
        ref={bodyRef}
        className="flex min-h-0 flex-1 flex-col px-3 pt-2"
        // 底部导航是 64px 悬浮层（absolute），页面 frame 已有 24px 外边距；
        // 全出血工作台必须自己让出剩余高度，否则结果坞的折叠条会被 Dock 盖住。
        style={{ paddingBottom: 'max(8px, calc(var(--dock-height) - 24px))' }}
      >
        <div
          className="flex min-h-0 flex-1 gap-2.5 pt-2"
          style={dockMode === 'max' ? { display: 'none' } : undefined}
        >
          {/* 编辑器 */}
          <div className="se-card flex min-w-0 flex-1 flex-col overflow-hidden">
            <div className="flex h-[34px] shrink-0 items-center justify-between border-b border-slate-100 px-3">
              <div className="flex min-w-0 items-center gap-2">
                <FileCode2 className="h-3.5 w-3.5 shrink-0 text-blue-500/80" />
                <span className="truncate text-[11px] font-semibold text-slate-600">
                  {hasFile ? currentName : '未打开文件'}
                </span>
                {editor.isDirty && hasFile && (
                  <span className="text-[9px] font-medium text-amber-500">未保存</span>
                )}
                <span className="shrink-0 rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[8.5px] text-slate-500">
                  {engineLabel}
                </span>
              </div>
              <div className="flex shrink-0 items-center gap-2">
                {meta.defaultModelName && (
                  <span
                    className="hidden max-w-[200px] items-center gap-1 truncate rounded-md bg-slate-50 px-1.5 py-0.5 text-[9px] text-slate-400 xl:flex"
                    title={`执行默认模型：${meta.defaultModelName}`}
                  >
                    <Cpu className="h-2.5 w-2.5" />
                    {meta.defaultModelName}
                  </span>
                )}
                {files.tab === 'local' && files.selectedFile && (
                  <button
                    type="button"
                    onClick={files.onUploadToCloud}
                    className="inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[9.5px] font-medium text-indigo-500 transition-colors hover:bg-indigo-50 hover:text-indigo-600"
                  >
                    <CloudUpload className="h-3 w-3" />
                    发布云端
                  </button>
                )}
              </div>
            </div>

            <div className="relative min-h-0 flex-1">
              {hasFile ? (
                <Editor
                  height="100%"
                  defaultLanguage="python"
                  theme={editor.theme}
                  value={editor.code}
                  onChange={(value) => editor.onChange(value || '')}
                  onMount={(instance, monaco) => {
                    editor.onMount(instance, monaco);
                    instance.onDidChangeCursorPosition((event) => {
                      setCursor({ line: event.position.lineNumber, column: event.position.column });
                    });
                  }}
                  options={MONACO_OPTIONS}
                />
              ) : (
                <EmptyEditor
                  onOpen={() => setFileMenuOpen(true)}
                  onCreate={files.onCreateFile}
                />
              )}
            </div>

            <div className="flex h-[22px] shrink-0 items-center justify-between border-t border-slate-100 bg-slate-50/50 px-3 font-mono text-[9px] text-slate-400">
              <span className="flex items-center gap-2">
                <span>Python</span>
                <span className="text-slate-300">·</span>
                <span>{engineLabel}</span>
                <span className="text-slate-300">·</span>
                <span>UTF-8</span>
              </span>
              <span className="flex items-center gap-2">
                <span className="text-slate-300">研究回测 · 不构成收益承诺</span>
                <span className="text-slate-300">·</span>
                {meta.marketLabel && <span>{meta.marketLabel}</span>}
                <span className="text-slate-300">·</span>
                <span>Ln {cursor.line}, Col {cursor.column}</span>
              </span>
            </div>
          </div>

          <BacktestSettingsPanel
            params={params}
            code={editor.code}
            collapsed={paramsCollapsed}
            onCollapsedChange={setParamsCollapsed}
          />
        </div>

        <div className={`pt-2 ${dockMode === 'max' ? 'flex min-h-0 flex-1' : 'shrink-0'}`}>
          <div className={dockMode === 'max' ? 'flex min-h-0 w-full flex-1' : 'w-full'}>
            <BacktestDock
              mode={dockMode}
              onModeChange={setDockMode}
              height={dockHeight}
              onHeightChange={setDockHeight}
              maxHeight={bodyHeight}
              tab={dockTab}
              onTabChange={setDockTab}
              run={run}
              results={results}
              result={unifiedResult}
              symbol={meta.strategySymbol}
              market={meta.market}
            />
          </div>
        </div>
      </div>

      {createDialog.mode && (
        <CreateDialog
          mode={createDialog.mode}
          name={createDialog.name}
          onNameChange={createDialog.onNameChange}
          onConfirm={createDialog.onConfirm}
          onCancel={createDialog.onCancel}
        />
      )}
    </div>
  );
};

export default StrategyEditorWorkspace;
