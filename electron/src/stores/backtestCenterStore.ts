/**
 * 回测中心 Zustand Store
 *
 * 管理：
 * - 当前激活模块
 * - 回测配置
 * - 运行中的回测
 * - 选中的回测（用于对比等）
 * - WebSocket连接
 */

import { create } from 'zustand';
import { devtools, persist } from 'zustand/middleware';
import { BACKTEST_CONFIG } from '../config/backtest';

// WebSocket 类型定义
interface CustomWebSocket extends WebSocket {
  readyState: typeof WebSocket.OPEN | typeof WebSocket.CONNECTING | typeof WebSocket.CLOSING | typeof WebSocket.CLOSED;
}

export type ModuleId =
  | 'quick-backtest'
  | 'expert-mode'
  | 'backtest-history'
  | 'strategy-compare'
  | 'parameter-optimize'
  | 'strategy-management'
  | 'advanced-analysis';

export interface BacktestConfig {
  strategy_code: string;
  symbol: string;
  start_date: string;
  end_date: string;
  initial_capital: number;
  commission: number;
  slippage: number;
  user_id: string;
  engine?: 'qlib';
  qlib_provider_uri?: string;
  qlib_region?: string;
  [key: string]: any;
}

export interface BacktestProgress {
  backtest_id: string;
  status: 'pending' | 'running' | 'completed' | 'failed';
  progress: number;
  message?: string;
}

interface BacktestCenterState {
  // 当前激活的模块
  activeModule: ModuleId;
  setActiveModule: (id: ModuleId) => void;

  // 回测配置
  backtestConfig: Partial<BacktestConfig>;
  updateBacktestConfig: (config: Partial<BacktestConfig>) => void;
  resetBacktestConfig: () => void;

  // 运行中的回测
  runningBacktests: Map<string, BacktestProgress>;
  addRunningBacktest: (id: string, progress: BacktestProgress) => void;
  updateProgress: (id: string, progress: number, message?: string) => void;
  removeRunningBacktest: (id: string) => void;

  // 选中的回测（用于对比、分析等）
  selectedBacktests: string[];
  toggleSelection: (id: string) => void;
  setSelectedBacktests: (ids: string[]) => void;
  clearSelection: () => void;

  // WebSocket连接状态
  wsConnections: Map<string, CustomWebSocket>;
  addWsConnection: (id: string, ws: CustomWebSocket) => void;
  removeWsConnection: (id: string) => void;
  clearAllWsConnections: () => void;

  // 回测历史
  backtestHistory: any[];
  setBacktestHistory: (history: any[]) => void;
  fetchHistory: (userId: string) => Promise<void>;
}

const defaultBacktestConfig: Partial<BacktestConfig> = {
  start_date: BACKTEST_CONFIG.QLIB.DEFAULT_START,
  end_date: BACKTEST_CONFIG.QLIB.DEFAULT_END,
  initial_capital: 100000,
  commission: 0.001,
  slippage: 0.001,
  user_id: 'default_user',
  engine: 'qlib',
  qlib_provider_uri: 'db/qlib_data',
  qlib_region: 'cn',
};

export const useBacktestCenterStore = create<BacktestCenterState>()(
  devtools(
    persist(
      (set, get) => ({
        // 初始状态
        activeModule: 'quick-backtest',
        backtestConfig: defaultBacktestConfig,
        runningBacktests: new Map(),
        selectedBacktests: [],
        wsConnections: new Map(),
        backtestHistory: [],

        // 模块切换
        setActiveModule: (id) => set({ activeModule: id }),

        // ... (保持原有方法)

        // 历史管理
        setBacktestHistory: (history) => set({ backtestHistory: history }),
        fetchHistory: async (userId) => {
          try {
            const { backtestService } = await import('../services/backtestService');
            const history = await backtestService.getHistory(userId);
            set({ backtestHistory: history });
          } catch (error) {
            console.error('Failed to fetch backtest history:', error);
          }
        },

        // 配置管理
        updateBacktestConfig: (config) =>
          set((state) => ({
            backtestConfig: { ...state.backtestConfig, ...config },
          })),

        resetBacktestConfig: () =>
          set({ backtestConfig: defaultBacktestConfig }),

        // 运行中回测管理
        addRunningBacktest: (id, progress) =>
          set((state) => {
            const newMap = new Map(state.runningBacktests);
            newMap.set(id, progress);
            return { runningBacktests: newMap };
          }),

        updateProgress: (id, progress, message) =>
          set((state) => {
            const newMap = new Map(state.runningBacktests);
            const existing = newMap.get(id) as BacktestProgress | undefined;
            if (existing) {
              newMap.set(id, {
                ...existing,
                progress,
                message: message || existing.message,
              } as BacktestProgress);
            }
            return { runningBacktests: newMap };
          }),

        removeRunningBacktest: (id) =>
          set((state) => {
            const newMap = new Map(state.runningBacktests);
            newMap.delete(id);
            return { runningBacktests: newMap };
          }),

        // 选中管理
        toggleSelection: (id) =>
          set((state) => {
            const selected = state.selectedBacktests.includes(id)
              ? state.selectedBacktests.filter((x) => x !== id)
              : [...state.selectedBacktests, id];
            return { selectedBacktests: selected };
          }),

        setSelectedBacktests: (ids) =>
          set({ selectedBacktests: ids }),

        clearSelection: () =>
          set({ selectedBacktests: [] }),

        // WebSocket连接管理
        addWsConnection: (id, ws) =>
          set((state) => {
            const newMap = new Map(state.wsConnections);
            newMap.set(id, ws as CustomWebSocket);
            return { wsConnections: newMap };
          }),

        removeWsConnection: (id) =>
          set((state) => {
            const ws = state.wsConnections.get(id);
            if (ws && ws.readyState === WebSocket.OPEN) {
              ws.close();
            }
            const newMap = new Map(state.wsConnections);
            newMap.delete(id);
            return { wsConnections: newMap };
          }),

        clearAllWsConnections: () =>
          set((state) => {
            state.wsConnections.forEach((ws) => {
              if (ws.readyState === WebSocket.OPEN) {
                ws.close();
              }
            });
            return { wsConnections: new Map() };
          }),
      }),
      {
        name: 'backtest-center-storage',
        partialize: (state) => ({
          activeModule: state.activeModule,
          // backtestConfig 不做持久化：每次打开应用应使用默认配置，
          // 避免旧策略参数被复用导致"不同策略返回相同结果"的假象。
          selectedBacktests: state.selectedBacktests,
        }),
      }
    )
  )
);
