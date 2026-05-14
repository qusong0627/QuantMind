/**
 * React Query 回测查询 Hooks
 *
 * 功能：
 * - 封装常用查询操作
 * - 统一配置缓存策略
 * - 自动重试和错误处理
 * - 乐观更新支持
 */

import { useQuery, useMutation, useQueryClient, UseQueryOptions, UseMutationOptions } from '@tanstack/react-query';
import type {
  BacktestConfig,
  BacktestResult,
  HistoryFilter,
  ComparisonResult,
  OptimizationConfig,
  OptimizationProgressOptions,
  OptimizationResult,
  MarketData,
} from '../services/backtestService';

// ============================================================================
// Query Keys
// ============================================================================

export const backtestKeys = {
  all: ['backtest'] as const,
  results: () => [...backtestKeys.all, 'results'] as const,
  result: (id: string) => [...backtestKeys.results(), id] as const,
  history: () => [...backtestKeys.all, 'history'] as const,
  historyFiltered: (userId: string, filter: HistoryFilter) =>
    [...backtestKeys.history(), userId, filter] as const,
  comparison: (id1: string, id2: string) =>
    [...backtestKeys.all, 'comparison', id1, id2] as const,
  comparisonForUser: (userId: string, id1: string, id2: string) =>
    [...backtestKeys.all, 'comparison', userId, id1, id2] as const,
  optimization: (id: string) =>
    [...backtestKeys.all, 'optimization', id] as const,
  marketData: (symbol: string, startDate: string, endDate: string) =>
    [...backtestKeys.all, 'market-data', symbol, startDate, endDate] as const,
};

// ============================================================================
// 查询配置
// ============================================================================

const defaultQueryConfig = {
  staleTime: 5 * 60 * 1000, // 5分钟
  cacheTime: 10 * 60 * 1000, // 10分钟
  retry: 2,
  retryDelay: (attemptIndex: number) => Math.min(1000 * 2 ** attemptIndex, 30000),
  refetchOnWindowFocus: false,
};

const historyQueryConfig = {
  ...defaultQueryConfig,
  staleTime: 1 * 60 * 1000, // 1分钟（历史数据更新频繁）
};

const marketDataQueryConfig = {
  ...defaultQueryConfig,
  staleTime: 60 * 60 * 1000, // 1小时（市场数据相对稳定）
  cacheTime: 2 * 60 * 60 * 1000, // 2小时
};

// ============================================================================
// 查询 Hooks
// ============================================================================

/**
 * 查询回测结果
 */
export function useBacktestResult(
  backtestId: string | null | undefined,
  options?: Omit<UseQueryOptions<BacktestResult, Error>, 'queryKey' | 'queryFn'>
) {
  return useQuery<BacktestResult, Error>({
    queryKey: backtestKeys.result(backtestId || ''),
    queryFn: async () => {
      if (!backtestId) {
        throw new Error('回测ID不能为空');
      }
      const { backtestService } = await import('../services/backtestService');
      return backtestService.getResult(backtestId);
    },
    ...defaultQueryConfig,
    enabled: !!backtestId,
    ...options,
  });
}

/**
 * 查询回测历史
 */
export function useBacktestHistory(
  userId: string,
  filter: HistoryFilter = {},
  options?: Omit<UseQueryOptions<BacktestResult[], Error>, 'queryKey' | 'queryFn'>
) {
  return useQuery<BacktestResult[], Error>({
    queryKey: backtestKeys.historyFiltered(userId, filter),
    queryFn: async () => {
      const { backtestService } = await import('../services/backtestService');
      return backtestService.getHistory(userId, filter);
    },
    ...historyQueryConfig,
    ...options,
  });
}

/**
 * 查询策略对比
 */
export function useBacktestComparison(
  userId: string,
  backtestId1: string | null | undefined,
  backtestId2: string | null | undefined,
  options?: Omit<UseQueryOptions<ComparisonResult, Error>, 'queryKey' | 'queryFn'>
) {
  return useQuery<ComparisonResult, Error>({
    queryKey: backtestKeys.comparisonForUser(userId, backtestId1 || '', backtestId2 || ''),
    queryFn: async () => {
      if (!backtestId1 || !backtestId2) {
        throw new Error('对比需要两个回测ID');
      }
      if (!userId) {
        throw new Error('用户ID不能为空');
      }
      const { backtestService } = await import('../services/backtestService');
      return backtestService.compareBacktests(backtestId1, backtestId2, userId);
    },
    ...defaultQueryConfig,
    enabled: !!backtestId1 && !!backtestId2 && !!userId,
    ...options,
  });
}

/**
 * 查询参数优化结果
 */
export function useOptimizationResult(
  optimizationId: string | null | undefined,
  options?: Omit<UseQueryOptions<OptimizationResult, Error>, 'queryKey' | 'queryFn'>
) {
  return useQuery<OptimizationResult, Error>({
    queryKey: backtestKeys.optimization(optimizationId || ''),
    queryFn: async () => {
      if (!optimizationId) {
        throw new Error('优化ID不能为空');
      }
      const { backtestService } = await import('../services/backtestService');
      return backtestService.getOptimizationResult(optimizationId);
    },
    ...defaultQueryConfig,
    enabled: !!optimizationId,
    ...options,
  });
}

/**
 * 查询市场数据
 */
export function useMarketData(
  symbol: string | null | undefined,
  startDate: string,
  endDate: string,
  options?: Omit<UseQueryOptions<MarketData, Error>, 'queryKey' | 'queryFn'>
) {
  return useQuery<MarketData, Error>({
    queryKey: backtestKeys.marketData(symbol || '', startDate, endDate),
    queryFn: async () => {
      if (!symbol) {
        throw new Error('股票代码不能为空');
      }
      const { backtestService } = await import('../services/backtestService');
      return backtestService.getMarketData(symbol, startDate, endDate);
    },
    ...marketDataQueryConfig,
    enabled: !!symbol && !!startDate && !!endDate,
    ...options,
  });
}

// ============================================================================
// 变更 Hooks (Mutations)
// ============================================================================

/**
 * 运行回测
 */
export function useRunBacktest(
  options?: UseMutationOptions<BacktestResult, Error, BacktestConfig>
) {
  const queryClient = useQueryClient();

  return useMutation<BacktestResult, Error, BacktestConfig>({
    mutationFn: (config) => import('../services/backtestService').then(m => m.backtestService.runBacktest(config)),
    onSuccess: (data) => {
      console.log('✅ 回测任务已提交:', data.backtest_id);

      // 立即添加到缓存
      queryClient.setQueryData(backtestKeys.result(data.backtest_id), data);

      // 使历史列表失效，触发重新获取
      queryClient.invalidateQueries({ queryKey: backtestKeys.history() });
    },
    onError: (error) => {
      console.error('❌ 运行回测失败:', error);
    },
    ...options,
  });
}

/**
 * 删除回测
 */
export function useDeleteBacktest(
  userId: string,
  options?: UseMutationOptions<void, Error, { id: string; userId?: string }, { previousHistory: [any, any][] }>
) {
  const queryClient = useQueryClient();

  return useMutation<void, Error, { id: string; userId?: string }, { previousHistory: [any, any][] }>({
    mutationFn: ({ id, userId: targetUserId }) => import('../services/backtestService').then(m => m.backtestService.deleteBacktest(id, targetUserId || userId)),
    onMutate: async (backtestId) => {
      // 取消正在进行的查询
      await queryClient.cancelQueries({ queryKey: backtestKeys.history() });

      // 保存之前的值（用于回滚）
      const previousHistory = queryClient.getQueriesData({ queryKey: backtestKeys.history() });

      // 乐观更新：从缓存中移除
      queryClient.setQueriesData<BacktestResult[]>(
        { queryKey: backtestKeys.history() },
        (old) => old?.filter((bt) => bt.backtest_id !== backtestId.id) || []
      );

      return { previousHistory };
    },
    onError: (error, { id: _backtestId }, context) => {
      console.error('❌ 删除回测失败:', error);

      // 回滚
      if (context?.previousHistory) {
        context.previousHistory.forEach(([key, value]) => {
          queryClient.setQueryData(key, value);
        });
      }
    },
    onSuccess: (_, { id: backtestId }) => {
      console.log('✅ 删除回测成功:', backtestId);

      // 移除该回测的缓存
      queryClient.removeQueries({ queryKey: backtestKeys.result(backtestId) });

      // 使历史列表失效
      queryClient.invalidateQueries({ queryKey: backtestKeys.history() });
    },
    ...options,
  });
}

/**
 * 批量删除回测
 */
export function useBatchDeleteBacktests(
  userId: string,
  options?: UseMutationOptions<void, Error, { id: string; userId?: string }[], { previousHistory: [any, any][] }>
) {
  const queryClient = useQueryClient();

  return useMutation<void, Error, { id: string; userId?: string }[], { previousHistory: [any, any][] }>({
    mutationFn: async (targets) => {
      await Promise.all(targets.map((t) => import('../services/backtestService').then(m => m.backtestService.deleteBacktest(t.id, t.userId || userId))));
    },
    onMutate: async (targets) => {
      await queryClient.cancelQueries({ queryKey: backtestKeys.history() });

      const previousHistory = queryClient.getQueriesData({ queryKey: backtestKeys.history() });

      const backtestIds = targets.map(t => t.id);
      // 乐观更新：从缓存中移除所有选中的回测
      queryClient.setQueriesData<BacktestResult[]>(
        { queryKey: backtestKeys.history() },
        (old) => old?.filter((bt) => !backtestIds.includes(bt.backtest_id)) || []
      );

      return { previousHistory };
    },
    onError: (error, backtestIds, context) => {
      console.error('❌ 批量删除失败:', error);

      // 回滚
      if (context?.previousHistory) {
        context.previousHistory.forEach(([key, value]) => {
          queryClient.setQueryData(key, value);
        });
      }
    },
    onSuccess: (_, targets) => {
      console.log('✅ 批量删除成功:', targets.length);

      // 移除所有被删除回测的缓存
      targets.forEach((t) => {
        queryClient.removeQueries({ queryKey: backtestKeys.result(t.id) });
      });

      // 使历史列表失效
      queryClient.invalidateQueries({ queryKey: backtestKeys.history() });
    },
    ...options,
  });
}

/**
 * 参数优化
 */
export function useOptimizeParameters(
  options?: UseMutationOptions<OptimizationResult, Error, OptimizationMutationInput>
) {
  const queryClient = useQueryClient();

  return useMutation<OptimizationResult, Error, OptimizationMutationInput>({
    mutationFn: ({ config, progress }) => import('../services/backtestService').then(m => m.backtestService.optimizeParameters(config, progress)),
    onSuccess: (data) => {
      console.log('✅ 参数优化已提交:', data.optimization_id);

      // 添加到缓存
      queryClient.setQueryData(
        backtestKeys.optimization(data.optimization_id),
        data
      );
    },
    onError: (error) => {
      console.error('❌ 参数优化失败:', error);
    },
    ...options,
  });
}

export interface OptimizationMutationInput {
  config: OptimizationConfig;
  progress?: OptimizationProgressOptions;
}

/**
 * 导出 CSV
 */
export function useExportCSV(
  options?: UseMutationOptions<Blob, Error, { backtestId: string; filename?: string }>
) {
  return useMutation<Blob, Error, { backtestId: string; filename?: string }>({
    mutationFn: async ({ backtestId, filename }) => {
      const { backtestService } = await import('../services/backtestService');
      const blob = await backtestService.exportCSV(backtestId);
      backtestService.downloadFile(blob, filename || `backtest-${backtestId}.csv`);
      return blob;
    },
    onSuccess: () => {
      console.log('✅ CSV 导出成功');
    },
    onError: (error) => {
      console.error('❌ CSV 导出失败:', error);
    },
    ...options,
  });
}

/**
 * 导出 JSON 原始文件
 */
export function useExportJSON(
  options?: UseMutationOptions<Blob, Error, { backtestId: string; filename?: string }>
) {
  return useMutation<Blob, Error, { backtestId: string; filename?: string }>({
    mutationFn: async ({ backtestId, filename }) => {
      const { backtestService } = await import('../services/backtestService');
      const blob = await backtestService.exportJSON(backtestId);
      backtestService.downloadFile(blob, filename || `backtest-${backtestId}.json`);
      return blob;
    },
    onSuccess: () => {
      console.log('✅ JSON 导出成功');
    },
    onError: (error) => {
      console.error('❌ JSON 导出失败:', error);
    },
    ...options,
  });
}

// 兼容旧命名
export const useExportPDF = useExportCSV;
export const useExportExcel = useExportCSV;

// ============================================================================
// 工具 Hooks
// ============================================================================

/**
 * 预加载回测结果
 */
export function usePrefetchBacktestResult() {
  const queryClient = useQueryClient();

  return (backtestId: string) => {
      queryClient.prefetchQuery({
      queryKey: backtestKeys.result(backtestId),
      queryFn: async () => {
        const { backtestService } = await import('../services/backtestService');
        return backtestService.getResult(backtestId);
      },
      ...defaultQueryConfig,
    });
  };
}

/**
 * 预加载市场数据
 */
export function usePrefetchMarketData() {
  const queryClient = useQueryClient();

  return (symbol: string, startDate: string, endDate: string) => {
    queryClient.prefetchQuery({
      queryKey: backtestKeys.marketData(symbol, startDate, endDate),
      queryFn: async () => {
        const { backtestService } = await import('../services/backtestService');
        return backtestService.getMarketData(symbol, startDate, endDate);
      },
      ...marketDataQueryConfig,
    });
  };
}

/**
 * 清除所有回测缓存
 */
export function useClearBacktestCache() {
  const queryClient = useQueryClient();

  return () => {
    queryClient.removeQueries({ queryKey: backtestKeys.all });
    console.log('🗑️  已清除所有回测缓存');
  };
}
