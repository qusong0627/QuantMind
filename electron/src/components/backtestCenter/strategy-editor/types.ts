/**
 * 回测中心「策略编辑」工作台的共享类型。
 *
 * 该目录只服务 `strategyEditorMode`（回测中心内嵌形态）；AI-IDE 全屏页仍走
 * `pages/AIIDEPage.tsx` 的三栏布局。状态仍由 AIIDEPage 持有，这里只做展示层，
 * 避免把 2600 行的页面再撑大。
 */
import type React from 'react';
import type { BacktestResult } from '../../../services/backtestService';
import type { QlibBacktestResult } from '../../../types/backtest/qlib';
import type { StrategyLabRunResult } from '../../../features/strategy-lab/types';
import type { SavedBacktestRun } from '../../../utils/strategyEditorSavedRuns';
import type { StrategyEditorBacktestParams } from '../../../utils/strategyEditorParams';

export interface StrategyFileItem {
  id: string;
  name: string;
  type: string;
  path: string;
  is_dir?: boolean;
}

export interface RemoteStrategyItem {
  id: string;
  name: string;
  description?: string | null;
  tags?: string[];
  created_at?: string | null;
  updated_at?: string | null;
}

export interface RunProgress {
  percent: number;
  message: string;
}

export type DockTab = 'net' | 'monthly' | 'kline' | 'trades' | 'yearly' | 'logs' | 'saved';

export type DockMode = 'collapsed' | 'open' | 'max';

export interface FinalResultSummary {
  backtestId?: string;
  strategyName?: string;
  symbol?: string;
  benchmarkSymbol?: string;
  status?: string;
  executionTime?: string;
  metrics: Array<{ label: string; value: string }>;
  tradeStats: Array<{ label: string; value: string }>;
  points: Array<{ label: string; value: string }>;
}

export interface FileWorkspaceState {
  tab: 'local' | 'remote';
  localFiles: StrategyFileItem[];
  remoteFiles: RemoteStrategyItem[];
  isLoadingLocal: boolean;
  isLoadingRemote: boolean;
  currentDir: string;
  parentDir: string | null;
  searchQuery: string;
  selectedFile: StrategyFileItem | null;
  selectedRemote: RemoteStrategyItem | null;
  onTabChange: (tab: 'local' | 'remote') => void;
  onSearchQueryChange: (query: string) => void;
  onSelectFile: (file: StrategyFileItem) => void;
  onSelectRemote: (strategy: RemoteStrategyItem) => void;
  onEnterDirectory: (dir: string) => void;
  onGoParent: () => void;
  onRefresh: () => void;
  onCreateFile: () => void;
  onCreateFolder: () => void;
  onRenameFile: (file: StrategyFileItem, event: React.MouseEvent) => void;
  onDeleteFile: (path: string, event: React.MouseEvent) => void;
  onUploadToCloud: () => void;
}

export interface EditorState {
  code: string;
  onChange: (code: string) => void;
  onMount: (editor: unknown, monaco: unknown) => void;
  theme: string;
  isSaving: boolean;
  isCheckingSyntax: boolean;
  syntaxError: string | null;
  isDirty: boolean;
  canRun: boolean;
  onSave: () => void;
}

export interface ParamsState {
  value: StrategyEditorBacktestParams;
  onChange: (patch: Partial<StrategyEditorBacktestParams>) => void;
  onApply: () => void;
  onApplyAndRun: () => void;
  market: string;
}

export interface RunState {
  isRunning: boolean;
  progress: RunProgress | null;
  logs: string[];
  errors: string[];
  onStop: () => void;
  onRun: () => void;
  onClearLogs: () => void;
  onCopyLogs: (which: 'result' | 'error') => void;
  finalSummary: FinalResultSummary | null;
}

export interface ResultsState {
  minibtResult: StrategyLabRunResult | null;
  qlibResult: BacktestResult | QlibBacktestResult | null;
  savedRuns: SavedBacktestRun[];
  onSave: () => void;
  onLoadSaved: (run: SavedBacktestRun) => void;
  onDeleteSaved: (id: string) => void;
}

export interface StrategyEditorWorkspaceProps {
  files: FileWorkspaceState;
  editor: EditorState;
  params: ParamsState;
  run: RunState;
  results: ResultsState;
  meta: {
    strategyName?: string | null;
    defaultModelName?: string;
    marketLabel: string;
    /** 当前市场（CN/HK/US/…），用于买入持有基准取数。 */
    market: string;
    /** minibt 代码里的 SYMBOL；截面策略为空。 */
    strategySymbol?: string;
    onOpenQuantBot: () => void;
  };
  createDialog: {
    mode: 'file' | 'folder' | null;
    name: string;
    onNameChange: (name: string) => void;
    onConfirm: () => void;
    onCancel: () => void;
  };
}
