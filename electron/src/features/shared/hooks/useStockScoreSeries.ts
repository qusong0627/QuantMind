/**
 * 单股模型分数序列查询（个股终端 / 推理中心共用）。
 *
 * 缓存策略：
 * - `staleTime=60s`：分数按日更新，1 分钟内重复点同一只票不再打后端；
 * - `gcTime=5min`：切走再切回还能命中；
 * - `refetchOnWindowFocus=false`：盘中切窗口不应该重扫 parquet；
 * - `placeholderData=keepPreviousData`：换模型/换票时保留上一张图，用户不会
 *   看到“图先消失再出现”，同时用 `refreshing` 显示背景更新。
 */

import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { modelTrainingService } from '../../../services/modelTrainingService';
import type {
  StockScoreHistoryResponse,
  StockScoreHistoryItem,
  StockScoreModelMeta,
  StockScoreWindow,
} from '../../../services/modelTrainingService';

export interface UseStockScoreSeriesOptions {
  /** 后缀式代码（600519.SH / 0700.HK / AAPL） */
  symbol: string;
  /** CN / HK / US / FUTURES；显式传，避免后端按代码猜市场 */
  market?: string;
  /** 不传 = 用户默认模型 */
  modelId?: string;
  /** 自然日回溯窗口；个股终端 750，推理中心 220 */
  days?: number;
  /** 窗口终点（含）；单票研判传 asOfDate 防前视 */
  endDate?: string;
  /** 是否计算每日截面排名（默认 false，热路径不算） */
  includeRank?: boolean;
  enabled?: boolean;
}

export interface UseStockScoreSeriesResult {
  data: StockScoreHistoryResponse | undefined;
  points: StockScoreHistoryItem[];
  models: StockScoreModelMeta[];
  model: StockScoreModelMeta | null;
  window: StockScoreWindow | null;
  windowWarning: string | null;
  lastScoreDate: string | null;
  staleDays: number | null;
  source: string;
  loading: boolean;
  refreshing: boolean;
  error: Error | null;
  refetch: () => void;
}

export function useStockScoreSeries(
  opts: UseStockScoreSeriesOptions,
): UseStockScoreSeriesResult {
  const query = useQuery({
    queryKey: [
      'stock-score-series',
      opts.market ?? '',
      opts.symbol,
      opts.modelId ?? '__default__',
      opts.days ?? 180,
      opts.endDate ?? '',
      opts.includeRank ? 'rank' : 'plain',
    ],
    queryFn: () =>
      modelTrainingService.getStockScoreSeries({
        symbol: opts.symbol,
        market: opts.market,
        modelId: opts.modelId || undefined,
        days: opts.days ?? 180,
        endDate: opts.endDate || undefined,
        includeRank: opts.includeRank,
      }),
    enabled: Boolean(opts.symbol) && opts.enabled !== false,
    staleTime: 60_000,
    gcTime: 300_000,
    refetchOnWindowFocus: false,
    retry: 1,
    placeholderData: keepPreviousData,
  });

  const data = query.data;
  return {
    data,
    points: data?.items ?? [],
    models: data?.models ?? [],
    model: data?.model ?? null,
    window: data?.window ?? null,
    windowWarning: data?.window_warning ?? null,
    lastScoreDate: data?.last_score_date ?? null,
    staleDays: data?.stale_days ?? null,
    source: data?.source ?? data?.score_source ?? '',
    loading: query.isLoading,
    refreshing: query.isFetching && !query.isLoading,
    error: (query.error as Error) ?? null,
    refetch: () => {
      void query.refetch();
    },
  };
}
