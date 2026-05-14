/**
 * 回测状态管理 Store (Zustand)
 *
 * 功能：
 * - 管理当前回测状态
 * - 管理回测历史
 * - 管理回测配置
 * - 提供回测操作方法
 */

import { create } from 'zustand';
import { devtools, persist } from 'zustand/middleware';
import type { BacktestConfig, BacktestResult, HistoryFilter } from '../services/backtestService';
import { Strategy } from '../state/atoms';

// ============================================================================
// 类型定义
// ============================================================================

interface BacktestState {
  // ========== 状态 ==========

  // 当前回测
  currentBacktest: BacktestResult | null;
  currentBacktestId: string | null;

  // 回测历史
  backtestHistory: BacktestResult[];
  historyLoading: boolean;
  historyError: string | null;
  historyFilter: HistoryFilter;
  totalHistoryCount: number;

  // 选中的策略
  selectedStrategy: Strategy | null;

  // 回测配置
  config: Partial<BacktestConfig>;

  // 执行状态
  isRunning: boolean;
  progress: number;
  progressMessage: string;

  // WebSocket 连接
  wsConnection: WebSocket | null;
  wsConnected: boolean;

  // 错误信息
  error: string | null;

  // 选中项（用于批量操作和对比）
  selectedBacktestIds: string[];

  // ========== 操作方法 ==========

  // 策略管理
  setStrategy: (strategy: Strategy | null) => void;

  // 配置管理
  updateConfig: (config: Partial<BacktestConfig>) => void;
  resetConfig: () => void;

  // 回测执行
  runBacktest: () => Promise<void>;
  stopBacktest: () => void;

  // 结果管理
  loadBacktestResult: (backtestId: string) => Promise<void>;
  setCurrentBacktest: (backtest: BacktestResult | null) => void;

  // 历史管理
  loadHistory: (userId: string) => Promise<void>;
  refreshHistory: () => Promise<void>;
  setHistoryFilter: (filter: Partial<HistoryFilter>) => void;
  clearHistoryFilter: () => void;

  // 批量操作
  toggleSelection: (backtestId: string) => void;
  selectAll: () => void;
  clearSelection: () => void;
  deleteSelected: () => Promise<void>;

  // 单个删除
  deleteBacktest: (backtestId: string) => Promise<void>;

  // WebSocket 管理
  connectProgress: (backtestId: string) => void;
  disconnectProgress: () => void;

  // 错误处理
  setError: (error: string | null) => void;
  clearError: () => void;
}

// ============================================================================
// 默认配置
// ============================================================================

const defaultConfig: Partial<BacktestConfig> = {
  start_date: '2023-01-01',
  end_date: '2026-01-01',
  initial_capital: 100000,
  commission: 0.001,
  benchmark_symbol: 'SH000300',
  risk_free_rate: 0.02,
  position_sizing: 'fixed',
  max_position_size: 1.0,
  transaction_cost: 0,
  slippage: 0,
};

const defaultHistoryFilter: HistoryFilter = {
  sort_by: 'created_at',
  sort_order: 'desc',
  page: 1,
  page_size: 20,
};

// ============================================================================
// Store 创建
// ============================================================================

export const useBacktestStore = create<BacktestState>()(
  devtools(
    persist(
      (set, get) => ({
        // ========== 初始状态 ==========
        currentBacktest: null,
        currentBacktestId: null,
        backtestHistory: [],
        historyLoading: false,
        historyError: null,
        historyFilter: defaultHistoryFilter,
        totalHistoryCount: 0,
        selectedStrategy: null,
        config: defaultConfig,
        isRunning: false,
        progress: 0,
        progressMessage: '准备开始...',
        wsConnection: null,
        wsConnected: false,
        error: null,
        selectedBacktestIds: [],

        // ========== 策略管理 ==========
        setStrategy: (strategy) => {
          console.log('📝 设置策略:', strategy?.name);
          set({ selectedStrategy: strategy });
        },

        // ========== 配置管理 ==========
        updateConfig: (partialConfig) => {
          console.log('⚙️  更新配置:', partialConfig);
          set((state) => ({
            config: { ...state.config, ...partialConfig },
          }));
        },

        resetConfig: () => {
          console.log('🔄 重置配置');
          set({ config: defaultConfig });
        },

        // ========== 回测执行 ==========
        runBacktest: async () => {
          const { selectedStrategy, config } = get();

          if (!selectedStrategy) {
            set({ error: '请先选择策略' });
            return;
          }

          if (!config.symbol) {
            set({ error: '请输入股票代码' });
            return;
          }

          console.log('🚀 开始回测');

          set({
            isRunning: true,
            progress: 0,
            progressMessage: '提交回测任务...',
            error: null,
            currentBacktest: null,
          });

          try {
            // 构建完整配置
            const backtestConfig: BacktestConfig = {
              strategy_code: selectedStrategy.code,
              symbol: config.symbol!,
              start_date: config.start_date || defaultConfig.start_date!,
              end_date: config.end_date || defaultConfig.end_date!,
              initial_capital: config.initial_capital || defaultConfig.initial_capital!,
              commission: config.commission || defaultConfig.commission!,
              user_id: config.user_id || 'default_user',
              strategy_params: selectedStrategy.parameters,
              ...config,
            };

            // 提交回测
            const { backtestService } = await import('../services/backtestService');
            const result = await backtestService.runBacktest(backtestConfig);

            console.log('✅ 回测任务已提交:', result.backtest_id);

            set({
              currentBacktestId: result.backtest_id,
              progressMessage: '回测已提交，等待开始...',
            });

            // 建立 WebSocket 连接
            get().connectProgress(result.backtest_id);

          } catch (error: any) {
            console.error('❌ 回测失败:', error);
            set({
              isRunning: false,
              error: error.message || '回测失败，请检查服务是否启动',
            });
          }
        },

        stopBacktest: () => {
          console.log('⏹️  停止回测');
          get().disconnectProgress();
          set({
            isRunning: false,
            progress: 0,
            progressMessage: '已停止',
          });
        },

        // ========== 结果管理 ==========
        loadBacktestResult: async (backtestId) => {
          console.log('📊 加载回测结果:', backtestId);

          try {
            const { backtestService } = await import('../services/backtestService');
            const result = await backtestService.getResult(backtestId);
            set({ currentBacktest: result });
          } catch (error: any) {
            console.error('加载回测结果失败:', error);
            set({ error: error.message });
          }
        },

        setCurrentBacktest: (backtest) => {
          set({ currentBacktest: backtest });
        },

        // ========== 历史管理 ==========
        loadHistory: async (userId) => {
          console.log('📜 加载回测历史');

          set({ historyLoading: true, historyError: null });

          try {
            const { historyFilter } = get();
            const { backtestService } = await import('../services/backtestService');
            const history = await backtestService.getHistory(userId, historyFilter);

            set({
              backtestHistory: history,
              totalHistoryCount: history.length,
              historyLoading: false,
            });
          } catch (error: any) {
            console.error('加载历史失败:', error);
            set({
              historyError: error.message,
              historyLoading: false,
            });
          }
        },

        refreshHistory: async () => {
          const { config } = get();
          await get().loadHistory(config.user_id || 'default_user');
        },

        setHistoryFilter: (filter) => {
          console.log('🔍 设置历史过滤:', filter);
          set((state) => ({
            historyFilter: { ...state.historyFilter, ...filter },
          }));
        },

        clearHistoryFilter: () => {
          console.log('🗑️  清除历史过滤');
          set({ historyFilter: defaultHistoryFilter });
        },

        // ========== 批量操作 ==========
        toggleSelection: (backtestId) => {
          set((state) => {
            const selected = state.selectedBacktestIds.includes(backtestId);
            return {
              selectedBacktestIds: selected
                ? state.selectedBacktestIds.filter((id) => id !== backtestId)
                : [...state.selectedBacktestIds, backtestId],
            };
          });
        },

        selectAll: () => {
          const { backtestHistory } = get();
          set({
            selectedBacktestIds: backtestHistory.map((bt) => bt.backtest_id),
          });
        },

        clearSelection: () => {
          set({ selectedBacktestIds: [] });
        },

        deleteSelected: async () => {
          const { selectedBacktestIds, config } = get();

          console.log(`🗑️  删除 ${selectedBacktestIds.length} 个回测`);

          try {
            await Promise.all(
              selectedBacktestIds.map((id) =>
                (async () => {
                  const { backtestService } = await import('../services/backtestService');
                  return backtestService.deleteBacktest(id, config.user_id || 'default');
                })()
              )
            );

            // 刷新历史
            await get().refreshHistory();

            // 清空选择
            set({ selectedBacktestIds: [] });

            console.log('✅ 批量删除成功');
          } catch (error: any) {
            console.error('批量删除失败:', error);
            set({ error: error.message });
          }
        },

        // ========== 单个删除 ==========
        deleteBacktest: async (backtestId) => {
          const { config } = get();
          console.log('🗑️  删除回测:', backtestId);

          try {
            const { backtestService } = await import('../services/backtestService');
            await backtestService.deleteBacktest(backtestId, config.user_id || 'default');

            // 从历史中移除
            set((state) => ({
              backtestHistory: state.backtestHistory.filter(
                (bt) => bt.backtest_id !== backtestId
              ),
            }));

            console.log('✅ 删除成功');
          } catch (error: any) {
            console.error('删除失败:', error);
            set({ error: error.message });
          }
        },

        // ========== WebSocket 管理 ==========
        connectProgress: (backtestId) => {
          console.log('🔗 建立 WebSocket 连接:', backtestId);

          // 先断开旧连接
          get().disconnectProgress();

          const { backtestService } = await import('../services/backtestService');
          const ws = backtestService.connectProgress(backtestId, {
            onProgress: (progress, message) => {
              set({
                progress,
                progressMessage: message,
                wsConnected: true,
              });
            },
            onComplete: (result) => {
              console.log('✅ 回测完成');
              set({
                currentBacktest: result,
                isRunning: false,
                progress: 1.0,
                progressMessage: '回测完成',
              });

              // 刷新历史
              get().refreshHistory();

              // 断开连接
              get().disconnectProgress();
            },
            onError: (error) => {
              console.error('❌ 回测错误:', error);
              set({
                isRunning: false,
                error: error.message,
              });
              get().disconnectProgress();
            },
          });

          set({ wsConnection: ws, wsConnected: true });
        },

        disconnectProgress: () => {
          const { wsConnection } = get();
          if (wsConnection) {
            console.log('🔌 断开 WebSocket 连接');
            wsConnection.close();
            set({ wsConnection: null, wsConnected: false });
          }
        },

        // ========== 错误处理 ==========
        setError: (error) => {
          set({ error });
        },

        clearError: () => {
          set({ error: null });
        },
      }),
      {
        name: 'backtest-storage', // localStorage key
        partialize: (state) => ({
          // 只持久化配置和历史过滤
          config: state.config,
          historyFilter: state.historyFilter,
        }),
      }
    ),
    { name: 'BacktestStore' }
  )
);

// ============================================================================
// 选择器 (Selectors)
// ============================================================================

export const selectIsRunning = (state: BacktestState) => state.isRunning;
export const selectProgress = (state: BacktestState) => state.progress;
export const selectCurrentBacktest = (state: BacktestState) => state.currentBacktest;
export const selectBacktestHistory = (state: BacktestState) => state.backtestHistory;
export const selectSelectedStrategy = (state: BacktestState) => state.selectedStrategy;
export const selectConfig = (state: BacktestState) => state.config;
export const selectError = (state: BacktestState) => state.error;
