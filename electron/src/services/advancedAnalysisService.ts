/**
 * 高级分析API服务
 *
 * 提供前端到后端的分析API调用
 */

import axios, { AxiosInstance } from 'axios';
import { SERVICE_URLS } from '../config/services';
import { authService } from '../features/auth/services/authService';

// 创建axios实例
const apiClient: AxiosInstance = axios.create({
  baseURL: SERVICE_URLS.QLIB_SERVICE,
  timeout: 120000,
  headers: {
    'Content-Type': 'application/json',
  },
});

apiClient.interceptors.request.use(
  (config) => {
    const token = authService.getAccessToken();
    if (token) {
      config.headers.Authorization = `Bearer ${token}`;
    }
    return config;
  },
  (error) => Promise.reject(error)
);

// ============================================================================
// 类型定义
// ============================================================================

export interface BasicRiskMetrics {
  annualized_return: number;
  total_return: number;
  volatility: number;
  max_drawdown: number;
  sharpe_ratio: number;
  calmar_ratio: number;
  sortino_ratio: number;
  var_95: number;
  cvar_95: number;
  skewness: number;
  kurtosis: number;
  omega_ratio: number;
  positive_days_pct: number;
  best_day_return: number;
  worst_day_return: number;
}

export interface TimeSeriesData {
  dates: string[];
  values: number[];
}

export interface HistogramData {
  bins: number[];
  counts: number[];
}

export interface BasicRiskResponse {
  metrics: BasicRiskMetrics;
  daily_returns: TimeSeriesData;
  cumulative_returns: TimeSeriesData;
  drawdown: TimeSeriesData;
  returns_distribution: HistogramData;
  analyzed_at: string;
  data_points: number;
}

export interface MonthlyReturn {
  year: number;
  month: number;
  return_pct: number;
  trading_days: number;
}

export interface PercentileData {
  p01: number;
  p05: number;
  p25: number;
  p50: number;
  p75: number;
  p95: number;
  p99: number;
}

export interface PerformanceResponse {
  monthly_returns: MonthlyReturn[];
  quarterly_returns: Record<string, number>;
  yearly_return: number;
  rolling_sharpe: TimeSeriesData;
  rolling_volatility: TimeSeriesData;
  rolling_return: TimeSeriesData;
  return_percentiles: PercentileData;
  analyzed_at: string;
  rolling_window: number;
}

export interface TradeStatsMetrics {
  win_rate: number;
  profit_loss_ratio: number;
  profit_loss_days_ratio: number;
  avg_holding_days: number;
  trade_frequency: number;
  total_trades: number;
}

export interface TradeStatsResponse {
  metrics: TradeStatsMetrics;
  pnl_distribution: HistogramData;
  holding_days_distribution: HistogramData;
  trade_frequency_series: TimeSeriesData;
  analyzed_at: string;
}

export interface BenchmarkMetrics {
  excess_return: number;
  beta: number;
  alpha: number | null;
  tracking_error: number;
  upside_capture: number;
  downside_capture: number;
  correlation: number;
}

export interface BenchmarkComparisonResponse {
  metrics: BenchmarkMetrics;
  strategy_returns: TimeSeriesData;
  benchmark_returns: TimeSeriesData;
  excess_returns: TimeSeriesData;
  analyzed_at: string;
  benchmark_id: string;
}

export interface PositionSummary {
  symbol: string;
  name?: string;
  weight: number;
  sector?: string;
}

export interface SectorAllocation {
  sector: string;
  weight: number;
  contribution?: number;
}

export interface PositionAnalysisResponse {
  top_holdings: PositionSummary[];
  sector_allocations: SectorAllocation[];
  concentration_hhi: number;
  holdings_count: number;
  analyzed_at: string;
}

// ============================================================================
// 服务类
// ============================================================================

class AdvancedAnalysisService {
  /**
   * 基础风险指标分析
   */
  async analyzeBasicRisk(backtestId: string, userId?: string): Promise<BasicRiskResponse> {
    try {
      const resolvedUserId = resolveUserId(userId);
      const response = await apiClient.post<BasicRiskResponse>(
        '/api/v1/analysis/basic-risk',
        {
          backtest_id: backtestId,
          user_id: resolvedUserId,
        }
      );
      return sanitizeBasicRiskResponse(response.data);
    } catch (error: any) {
      console.error('基础风险分析失败:', error);
      throw new Error(error.response?.data?.detail || error.message || '分析失败');
    }
  }

  /**
   * 绩效分析
   */
  async analyzePerformance(
    backtestId: string,
    rollingWindow: number = 30,
    userId?: string
  ): Promise<PerformanceResponse> {
    try {
      const resolvedUserId = resolveUserId(userId);
      const response = await apiClient.post<PerformanceResponse>(
        '/api/v1/analysis/performance',
        {
          backtest_id: backtestId,
          user_id: resolvedUserId,
          rolling_window: rollingWindow,
        }
      );
      return response.data;
    } catch (error: any) {
      console.error('绩效分析失败:', error);
      throw new Error(error.response?.data?.detail || error.message || '分析失败');
    }
  }

  /**
   * 交易统计
   */
  async analyzeTradeStats(backtestId: string, userId?: string): Promise<TradeStatsResponse> {
    try {
      const resolvedUserId = resolveUserId(userId);
      const response = await apiClient.post<TradeStatsResponse>(
        '/api/v1/analysis/trade-stats',
        {
          backtest_id: backtestId,
          user_id: resolvedUserId,
        }
      );
      return sanitizeTradeStatsResponse(response.data);
    } catch (error: any) {
      console.error('交易统计分析失败:', error);
      throw new Error(error.response?.data?.detail || error.message || '分析失败');
    }
  }

  /**
   * 基准对比
   */
  async compareBenchmark(
    backtestId: string,
    benchmarkId: string,
    userId?: string
  ): Promise<BenchmarkComparisonResponse> {
    try {
      const resolvedUserId = resolveUserId(userId);
      const response = await apiClient.post<BenchmarkComparisonResponse>(
        '/api/v1/analysis/benchmark',
        {
          backtest_id: backtestId,
          user_id: resolvedUserId,
          benchmark_id: benchmarkId,
        }
      );
      return sanitizeBenchmarkResponse(response.data, benchmarkId);
    } catch (error: any) {
      console.error('基准对比分析失败:', error);
      throw new Error(error.response?.data?.detail || error.message || '分析失败');
    }
  }

  /**
   * 持仓分析
   */
  async analyzePosition(backtestId: string, userId?: string): Promise<PositionAnalysisResponse> {
    try {
      const resolvedUserId = resolveUserId(userId);
      const response = await apiClient.post<PositionAnalysisResponse>(
        '/api/v1/analysis/position',
        {
          backtest_id: backtestId,
          user_id: resolvedUserId,
        }
      );
      return sanitizePositionResponse(response.data);
    } catch (error: any) {
      console.error('持仓分析失败:', error);
      throw new Error(error.response?.data?.detail || error.message || '分析失败');
    }
  }
}

export const advancedAnalysisService = new AdvancedAnalysisService();

// ============================================================================
// 响应兜底与校验
// ============================================================================

const EMPTY_HISTOGRAM: HistogramData = { bins: [], counts: [] };
const EMPTY_SERIES: TimeSeriesData = { dates: [], values: [] };

function finiteOrZero(value: unknown): number {
  return Number.isFinite(value) ? Number(value) : 0;
}

function finiteOrNull(value: unknown): number | null {
  return Number.isFinite(value) ? Number(value) : null;
}

function resolveUserId(explicitUserId?: string): string {
  if (explicitUserId?.trim()) return explicitUserId;
  const storedUser = authService.getStoredUser();
  const fallback = storedUser?.id ?? (storedUser as any)?.user_id;
  if (fallback) return String(fallback);
  throw new Error('未登录或用户信息缺失');
}

function sanitizeTradeStatsResponse(raw: TradeStatsResponse): TradeStatsResponse {
  return {
    metrics: {
      win_rate: Number.isFinite(raw?.metrics?.win_rate) ? raw.metrics.win_rate : 0,
      profit_loss_ratio: Number.isFinite(raw?.metrics?.profit_loss_ratio) ? raw.metrics.profit_loss_ratio : 0,
      profit_loss_days_ratio: Number.isFinite(raw?.metrics?.profit_loss_days_ratio) ? raw.metrics.profit_loss_days_ratio : 0,
      avg_holding_days: Number.isFinite(raw?.metrics?.avg_holding_days) ? raw.metrics.avg_holding_days : 0,
      trade_frequency: Number.isFinite(raw?.metrics?.trade_frequency) ? raw.metrics.trade_frequency : 0,
      total_trades: Number.isFinite(raw?.metrics?.total_trades) ? raw.metrics.total_trades : 0,
    },
    pnl_distribution: raw?.pnl_distribution ?? EMPTY_HISTOGRAM,
    holding_days_distribution: raw?.holding_days_distribution ?? EMPTY_HISTOGRAM,
    trade_frequency_series: raw?.trade_frequency_series ?? EMPTY_SERIES,
    analyzed_at: raw?.analyzed_at ?? new Date().toISOString(),
  };
}

function sanitizeBasicRiskResponse(raw: BasicRiskResponse): BasicRiskResponse {
  return {
    metrics: {
      annualized_return: finiteOrZero(raw?.metrics?.annualized_return),
      total_return: finiteOrZero(raw?.metrics?.total_return),
      volatility: finiteOrZero(raw?.metrics?.volatility),
      max_drawdown: finiteOrZero(raw?.metrics?.max_drawdown),
      sharpe_ratio: finiteOrZero(raw?.metrics?.sharpe_ratio),
      calmar_ratio: finiteOrZero(raw?.metrics?.calmar_ratio),
      sortino_ratio: finiteOrZero(raw?.metrics?.sortino_ratio),
      var_95: finiteOrZero(raw?.metrics?.var_95),
      cvar_95: finiteOrZero(raw?.metrics?.cvar_95),
      skewness: finiteOrZero(raw?.metrics?.skewness),
      kurtosis: finiteOrZero(raw?.metrics?.kurtosis),
      omega_ratio: finiteOrZero(raw?.metrics?.omega_ratio),
      positive_days_pct: finiteOrZero(raw?.metrics?.positive_days_pct),
      best_day_return: finiteOrZero(raw?.metrics?.best_day_return),
      worst_day_return: finiteOrZero(raw?.metrics?.worst_day_return),
    },
    daily_returns: sanitizeSeries(raw?.daily_returns),
    cumulative_returns: sanitizeSeries(raw?.cumulative_returns),
    drawdown: sanitizeSeries(raw?.drawdown),
    returns_distribution: sanitizeHistogram(raw?.returns_distribution),
    analyzed_at: raw?.analyzed_at ?? new Date().toISOString(),
    data_points: finiteOrZero(raw?.data_points),
  };
}

function sanitizeSeries(raw?: TimeSeriesData): TimeSeriesData {
  if (!raw || !Array.isArray(raw.dates) || !Array.isArray(raw.values)) {
    return EMPTY_SERIES;
  }
  const size = Math.min(raw.dates.length, raw.values.length);
  return {
    dates: raw.dates.slice(0, size),
    values: raw.values.slice(0, size).map(finiteOrZero),
  };
}

function sanitizeHistogram(raw?: HistogramData): HistogramData {
  if (!raw || !Array.isArray(raw.bins) || !Array.isArray(raw.counts)) {
    return EMPTY_HISTOGRAM;
  }
  const size = Math.min(raw.bins.length, raw.counts.length);
  return {
    bins: raw.bins.slice(0, size).map(finiteOrZero),
    counts: raw.counts.slice(0, size).map(finiteOrZero),
  };
}

function sanitizeBenchmarkResponse(
  raw: BenchmarkComparisonResponse,
  benchmarkId: string
): BenchmarkComparisonResponse {
  return {
    metrics: {
      excess_return: Number.isFinite(raw?.metrics?.excess_return) ? raw.metrics.excess_return : 0,
      beta: Number.isFinite(raw?.metrics?.beta) ? raw.metrics.beta : 0,
      alpha: finiteOrNull(raw?.metrics?.alpha),
      tracking_error: Number.isFinite(raw?.metrics?.tracking_error) ? raw.metrics.tracking_error : 0,
      upside_capture: Number.isFinite(raw?.metrics?.upside_capture) ? raw.metrics.upside_capture : 0,
      downside_capture: Number.isFinite(raw?.metrics?.downside_capture) ? raw.metrics.downside_capture : 0,
      correlation: Number.isFinite(raw?.metrics?.correlation) ? raw.metrics.correlation : 0,
    },
    strategy_returns: raw?.strategy_returns ?? EMPTY_SERIES,
    benchmark_returns: raw?.benchmark_returns ?? EMPTY_SERIES,
    excess_returns: raw?.excess_returns ?? EMPTY_SERIES,
    analyzed_at: raw?.analyzed_at ?? new Date().toISOString(),
    benchmark_id: raw?.benchmark_id ?? benchmarkId,
  };
}

function sanitizePositionResponse(raw: PositionAnalysisResponse): PositionAnalysisResponse {
  return {
    top_holdings: Array.isArray(raw?.top_holdings) ? raw.top_holdings : [],
    sector_allocations: Array.isArray(raw?.sector_allocations) ? raw.sector_allocations : [],
    concentration_hhi: Number.isFinite(raw?.concentration_hhi) ? raw.concentration_hhi : 0,
    holdings_count: Number.isFinite(raw?.holdings_count) ? raw.holdings_count : 0,
    analyzed_at: raw?.analyzed_at ?? new Date().toISOString(),
  };
}
