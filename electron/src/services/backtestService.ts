/**
 * 回测服务 - 统一封装所有回测相关 API
 *
 * 功能覆盖：
 * - 运行回测
 * - 获取回测结果
 * - 历史记录查询
 * - 策略对比
 * - 参数优化
 * - 报告导出
 * - 市场数据查询
 * - 回测删除
 */

import axios, { AxiosInstance } from 'axios';
import { SERVICE_URLS } from '../config/services';
import { authService } from '../features/auth/services/authService';

// ============================================================================
// 类型定义
// ============================================================================

/** 回测请求配置 */
export interface BacktestConfig {
  // 必填参数
  strategy_code?: string;
  strategy_id?: string;
  symbol: string;
  start_date: string;
  end_date: string;
  initial_capital: number;
  commission: number;
  user_id: string;
  engine?: 'qlib';
  qlib_provider_uri?: string;
  qlib_region?: string;
  strategy_type?: string;
  seed?: number;
  deal_price?: 'open' | 'close';
  is_third_party?: boolean;
  buy_cost?: number;
  sell_cost?: number;
  dynamic_position?: boolean;
  style?: string;
  market_state_symbol?: string;
  market_state_window?: number;
  strategy_total_position?: number;

  // 可选参数
  strategy_params?: Record<string, any>;
  benchmark_symbol?: string;
  risk_free_rate?: number;
  position_sizing?: 'fixed' | 'percent' | 'kelly';
  max_position_size?: number;
  stop_loss?: number;
  take_profit?: number;
  rebalance_frequency?: 'daily' | 'weekly' | 'monthly';
  transaction_cost?: number;
  slippage?: number;
  enable_optimization?: boolean;
  optimization_params?: Record<string, any>;
}

/** 回测结果 */
export interface BacktestResult {
  // 基础信息
  backtest_id: string;
  task_id?: string;
  user_id?: string;
  status: 'pending' | 'running' | 'completed' | 'failed';
  created_at: string;
  completed_at?: string;
  error_message?: string;
  full_error?: string;
  progress?: number;

  // 配置信息
  symbol?: string;
  strategy_name?: string;
  start_date?: string;
  end_date?: string;
  initial_capital?: number;
  benchmark_symbol?: string;
  data_source?: string;

  // 收益指标
  final_capital?: number;
  total_return?: number;
  annual_return?: number;
  cumulative_return?: number;

  // 风险指标
  max_drawdown?: number;
  max_drawdown_duration?: number;
  volatility?: number;
  downside_deviation?: number;
  var_95?: number;
  cvar_95?: number;

  // 风险调整收益
  sharpe_ratio?: number;
  sortino_ratio?: number;
  calmar_ratio?: number;

  // 相对基准指标
  alpha?: number;
  beta?: number;
  correlation?: number;
  tracking_error?: number;
  information_ratio?: number;
  benchmark_return?: number;

  // 交易统计
  total_trades?: number;
  win_rate?: number;
  profit_factor?: number;
  avg_win?: number;
  avg_loss?: number;
  avg_holding_period?: number;

  // 统计分析
  skewness?: number;
  kurtosis?: number;

  // 图表数据
  equity_curve?: Array<{ date: string; value: number }>;
  drawdown_curve?: Array<{ date: string; value: number }>;
  trades?: Trade[];
  trade_list?: Trade[];
  config?: {
    initial_capital?: number;
    [key: string]: any;
  };
  monthly_returns?: Array<{ month: string; return: number }>;
  yearly_returns?: Array<{ year: string; return: number }>;

  // 极值分析与归因 (新增)
  style_attribution?: {
    portfolio: Record<string, number>;
    benchmark: Record<string, number>;
  };
  factor_metrics?: {
    rank_ic?: number;
    icir?: number;
    rank_ic_std?: number;
    [key: string]: any;
  };
  stratified_returns?: Array<{
    group: number;
    total_return: number;
    annual_return: number;
  }>;
  rebalance_suggestions?: Array<{
    symbol: string;
    action: 'buy' | 'sell' | 'hold';
    current_weight: number;
    target_weight: number;
    weight_diff: number;
    estimated_amount?: number;
  }>;

  // 极值
  best_month?: { month: string; return: number };
  worst_month?: { month: string; return: number };
}

/** 交易记录 */
export interface Trade {
  date: string;
  symbol: string;
  action: 'buy' | 'sell';
  price: number;
  quantity: number;
  amount: number;
  totalAmount?: number;
  total_amount?: number;
  adj_price?: number;
  adj_quantity?: number;
  factor?: number;
  commission?: number;
  pnl?: number;
  balance?: number;
  equity_after?: number;
}

/** 历史查询过滤器 */
export interface HistoryFilter {
  status?: 'pending' | 'running' | 'completed' | 'failed';
  symbol?: string;
  start_date?: string;
  end_date?: string;
  sort_by?: 'created_at' | 'total_return' | 'sharpe_ratio' | 'max_drawdown';
  sort_order?: 'asc' | 'desc';
  page?: number;
  page_size?: number;
}

/** 策略对比结果 */
export interface ComparisonResult {
  backtest1: BacktestResult;
  backtest2: BacktestResult;
  metrics_comparison: {
    metric: string;
    value1: number;
    value2: number;
    difference: number;
    percentage_diff: number;
    better: 1 | 2 | 'equal';
  }[];
  insights: string[];
}

/** 参数优化配置 */
export interface OptimizationConfig {
  strategy_code: string;
  symbol: string;
  start_date: string;
  end_date: string;
  initial_capital: number;
  user_id: string;

  // 优化目标
  optimization_target: 'sharpe_ratio' | 'total_return' | 'calmar_ratio' | 'min_drawdown';

  // 参数范围
  param_ranges: {
    name: string;
    type: 'int' | 'float' | 'choice';
    min?: number;
    max?: number;
    step?: number;
    choices?: any[];
  }[];

  // 遗传算法配置
  population_size?: number;
  generations?: number;
  mutation_rate?: number;
  crossover_rate?: number;
}

/** Qlib 参数优化配置 */
export interface QlibOptimizationConfig {
  symbol?: string;
  start_date: string;
  end_date: string;
  initial_capital: number;
  user_id: string;
  qlib_strategy_type: string;
  qlib_strategy_params?: Record<string, any>;
  benchmark_symbol?: string;
  param_ranges: Array<{
    name: string;
    min: number;
    max: number;
    step: number;
  }>;
  optimization_target: string;
  population_size?: number;
  generations?: number;
  mutation_rate?: number;
  max_parallel?: number;

  // 费率参数
  commission?: number;
  min_commission?: number;
  stamp_duty?: number;
  transfer_fee?: number;
}

/** 参数优化结果 */
export interface OptimizationResult {
  optimization_id: string;
  status: 'pending' | 'running' | 'completed' | 'failed';
  progress?: number;

  // 最优参数
  best_params?: Record<string, any>;
  best_fitness?: number;

  // 优化历史
  fitness_history?: number[];
  generation_stats?: {
    generation: number;
    best_fitness: number;
    avg_fitness: number;
    worst_fitness: number;
  }[];

  // 所有评估结果
  all_results?: {
    params: Record<string, any>;
    fitness: number;
    metrics: Partial<BacktestResult>;
  }[];

  // 完成信息
  completed_at?: string;
  total_evaluations?: number;
  error_message?: string;
}

export interface OptimizationTaskResponse {
  optimization_id: string;
  task_id: string;
  status: string;
  created_at?: string;
}

export interface OptimizationRunStatus {
  optimization_id?: string;
  progress: number;
  status: string;
  message?: string;
  total_tasks?: number;
  completed_count?: number;
  failed_count?: number;
  current_params?: Record<string, any>;
  best_params?: Record<string, any>;
  best_metric_value?: number;
  result_summary?: Record<string, any>;
}

export interface OptimizationHistoryItem {
  optimization_id: string;
  task_id?: string;
  mode: string;
  user_id?: string;
  tenant_id: string;
  status: 'pending' | 'queued' | 'running' | 'completed' | 'failed' | 'cancelled';
  created_at: string;
  updated_at: string;
  completed_at?: string;
  optimization_target?: string;
  total_tasks: number;
  completed_count: number;
  failed_count: number;
  current_params?: Record<string, any>;
  best_params?: Record<string, any>;
  best_metric_value?: number;
  config_snapshot: Record<string, any>;
  error_message?: string;
  can_apply: boolean;
}

export interface OptimizationHistoryDetail extends OptimizationHistoryItem {
  base_request: Record<string, any>;
  param_ranges: Array<Record<string, any>>;
  result_summary: Record<string, any>;
  all_results: Array<{
    params: Record<string, any>;
    metrics: Partial<BacktestResult>;
  }>;
}

interface TaskStatusResponse<T = any> {
  task_id: string;
  state: string;
  info?: T;
  ready: boolean;
  successful: boolean;
  failed: boolean;
}

interface BacktestLogsResponse {
  backtest_id: string;
  logs: string[];
  next_index: number;
  total_length?: number;
}

export interface OptimizationProgressOptions {
  onProgress?: (progress: number, status?: string, info?: OptimizationRunStatus) => void;
  onLog?: (message: string) => void;
  onTaskCreated?: (taskId: string) => void;
  pollIntervalMs?: number;
  signal?: AbortSignal;
}

export interface QlibHealthStatus {
  status?: string;
  service?: string;
  redis_ok?: boolean;
  [key: string]: any;
}

/** 市场数据 */
export interface MarketData {
  symbol: string;
  data: {
    date: string;
    open: number;
    high: number;
    low: number;
    close: number;
    volume: number;
  }[];
  start_date: string;
  end_date: string;
  data_source: string;
}

/** WebSocket 进度回调 */
export interface ProgressCallbacks {
  onProgress?: (progress: number, message: string) => void;
  onComplete?: (result: BacktestResult) => void;
  onError?: (error: Error) => void;
  onLog?: (message: string) => void;
}

// ============================================================================
// 回测服务类
// ============================================================================

class BacktestService {
  private client: AxiosInstance;
  private baseUrl: string;
  public wsUrl: string;
  private optimizationResults: Map<string, OptimizationResult> = new Map();
  private exportRouteAvailable: boolean | null = null;

  private csvEscape(value: unknown): string {
    if (value === null || value === undefined) return '';
    const str = String(value);
    if (/[",\n]/.test(str)) {
      return `"${str.replace(/"/g, '""')}"`;
    }
    return str;
  }

  private toFiniteNumber(value: unknown): number | null {
    const n = Number(value);
    return Number.isFinite(n) ? n : null;
  }

  private buildQuickTradeRows(result: BacktestResult): Array<{
    date: string;
    symbol: string;
    action: string;
    displayPrice: number;
    qtyInt: number;
    amount: number;
    commission: number;
    equityBalance: number | null;
  }> {
    const trades = Array.isArray((result as any)?.trades)
      ? ((result as any).trades as Trade[])
      : Array.isArray((result as any)?.trade_list)
        ? ((result as any).trade_list as Trade[])
        : [];
    const equityCurve = Array.isArray(result.equity_curve) ? result.equity_curve : [];

    const equityByDate = new Map<string, number>();
    for (const point of equityCurve) {
      const dateKey = String((point as any)?.date || '').slice(0, 10);
      const val = this.toFiniteNumber((point as any)?.value);
      if (dateKey && val !== null) {
        equityByDate.set(dateKey, val);
      }
    }

    const initialCapital =
      this.toFiniteNumber(result.initial_capital) ??
      this.toFiniteNumber((result as any)?.config?.initial_capital);
    let runningBalance: number | null = initialCapital;

    const normalizeQty = (symbol: string, qty: number): number => {
      const qtyInt = Math.round(qty);
      const upper = String(symbol || '').toUpperCase();
      if ((upper.startsWith('SH') || upper.startsWith('SZ') || upper.startsWith('BJ')) && qtyInt >= 100) {
        const lotRounded = Math.round(qtyInt / 100) * 100;
        if (Math.abs(qtyInt - lotRounded) <= 2) return lotRounded;
      }
      return qtyInt;
    };

    return trades.map((t) => {
      const factor = this.toFiniteNumber((t as any)?.factor);
      const hasValidFactor = factor !== null && factor > 0;

      const explicitPrice = this.toFiniteNumber((t as any)?.price);
      const explicitQty = this.toFiniteNumber((t as any)?.quantity);
      let adjPrice = this.toFiniteNumber((t as any)?.adj_price);
      let adjQty = this.toFiniteNumber((t as any)?.adj_quantity);

      if (adjPrice === null) adjPrice = explicitPrice;
      if (adjQty === null) adjQty = explicitQty;

      let displayPrice = explicitPrice ?? 0;
      let displayQty = explicitQty ?? 0;
      const hasExplicitPrice = explicitPrice !== null;
      const hasExplicitQty = explicitQty !== null;
      if (!hasExplicitPrice && hasValidFactor && adjPrice !== null && adjQty !== null) {
        displayPrice = adjPrice / factor;
      }
      if (!hasExplicitQty && hasValidFactor && adjPrice !== null && adjQty !== null) {
        displayQty = adjQty * factor;
      }

      const qtyInt = normalizeQty(String((t as any)?.symbol || ''), displayQty);
      const amount =
        this.toFiniteNumber((t as any)?.totalAmount ?? (t as any)?.total_amount ?? (t as any)?.amount) ??
        displayPrice * displayQty;
      const commission = this.toFiniteNumber((t as any)?.commission) ?? 0;

      const actionRaw = String((t as any)?.action || '').toLowerCase();
      const isBuy = actionRaw === 'buy';
      const isSell = actionRaw === 'sell';

      const hasBalance = this.toFiniteNumber((t as any)?.balance) !== null;
      const hasEquityAfter = this.toFiniteNumber((t as any)?.equity_after) !== null;
      if (runningBalance !== null && !hasBalance && !hasEquityAfter) {
        if (isBuy) runningBalance -= amount + commission;
        if (isSell) runningBalance += amount - commission;
      }

      const tradeDate = String((t as any)?.date || '');
      const tradeDay = tradeDate.slice(0, 10);
      const equityOnDate = equityByDate.get(tradeDay);
      const equityBalance =
        (equityOnDate ?? this.toFiniteNumber((t as any)?.equity_after) ?? this.toFiniteNumber((t as any)?.balance)) ??
        runningBalance;

      return {
        date: tradeDate,
        symbol: String((t as any)?.symbol || ''),
        action: isBuy ? '买入' : '卖出',
        displayPrice,
        qtyInt,
        amount,
        commission,
        equityBalance,
      };
    });
  }

  private buildCsvFromBacktestResult(result: BacktestResult): string {
    const lines: string[] = [];
    lines.push('日期,代码,方向,成交价,成交量,成交金额,手续费,权益余额');

    const rows = this.buildQuickTradeRows(result);
    for (const row of rows) {
      lines.push(
        [
          this.csvEscape(row.date || ''),
          this.csvEscape(row.symbol || ''),
          this.csvEscape(row.action),
          this.csvEscape(Number(row.displayPrice || 0).toFixed(2)),
          this.csvEscape(String(Number(row.qtyInt || 0))),
          this.csvEscape(Number(row.amount || 0).toFixed(2)),
          this.csvEscape(Number(row.commission || 0).toFixed(2)),
          this.csvEscape(
            Number.isFinite(row.equityBalance as number) ? Number(row.equityBalance).toFixed(2) : ''
          ),
        ].join(',')
      );
    }

    return lines.join('\n');
  }

  private buildUniverse(symbol: string): string {
    if (!symbol) return 'csi300';
    const normalized = symbol.trim();
    if (!normalized) return 'csi300';
    // 服务器相对路径股票池（如 instruments/csi300.txt）直接透传
    if (normalized.includes('/') || normalized.toLowerCase().endsWith('.txt')) {
      return normalized;
    }
    // 内置股票池关键词直接透传
    if (['all', 'csi300', 'csi500', 'csi800', 'csi1000'].includes(normalized.toLowerCase())) {
      return normalized;
    }
    const symbols = symbol
      .split(',')
      .map((item) => item.trim())
      .filter(Boolean);
    if (!symbols.length) return 'csi300';
    return symbols
      .map((sym) => {
        const parts = sym.split('.');
        return parts.length === 2 ? `${parts[1]}${parts[0]}` : sym;
      })
      .join(' ');
  }

  /**
   * 标准化 UserID 为 8 位数格式
   */
  private normalizeUserId(userId: string): string {
    const trimmed = String(userId || '').trim();
    if (!trimmed || trimmed === 'default_user' || trimmed === 'default') {
      return 'default';
    }
    if (/^\d+$/.test(trimmed)) {
      return trimmed.padStart(8, '0');
    }
    return trimmed;
  }

  constructor() {
    this.baseUrl = `${SERVICE_URLS.QLIB_SERVICE}/api/v1/qlib`;

    // 优先从环境变量获取 WebSocket 基址，否则根据 QLIB_SERVICE 自动推导
    const envWsUrl = (import.meta as any).env?.VITE_WS_BASE_URL;
    if (envWsUrl) {
      this.wsUrl = envWsUrl.replace(/\/+$/, '');
    } else {
      const qlibUrl = SERVICE_URLS.QLIB_SERVICE;
      const wsProtocol = qlibUrl.startsWith('https') ? 'wss' : 'ws';
      const hostPath = qlibUrl.replace(/^https?:\/\//, '');
      this.wsUrl = `${wsProtocol}://${hostPath}`;
    }

    // 创建 Axios 实例
    this.client = axios.create({
      baseURL: this.baseUrl,
      timeout: 180000, // 3分钟超时
      headers: {
        'Content-Type': 'application/json',
      },
    });

    // 响应拦截器 - 统一错误处理
    this.client.interceptors.response.use(
      (response) => response,
      async (error) => {
        // 交由 authService 统一处理 401 Token 刷新与重试
        if (error.response?.status === 401) {
          return authService.handle401Error(error, this.client);
        }

        const requestUrl = String(error?.config?.url || '');
        const status = Number(error?.response?.status || 0);
        const isExpectedExportFallbackError =
          requestUrl.includes('/export/') && (status === 404 || status === 502);
        if (!isExpectedExportFallbackError) {
          console.error('Backtest API Error:', error);
        }

        if (error.response) {
          // 服务器返回错误 - 确保 detail 被序列化为字符串
          const rawDetail = error.response.data?.detail ?? error.response.statusText;
          let message = '';
          if (typeof rawDetail === 'string') {
            message = rawDetail;
          } else if (Array.isArray(rawDetail)) {
            // FastAPI/Pydantic 常见 422 结构
            const first = rawDetail[0];
            if (first?.msg) {
              const loc = Array.isArray(first.loc) ? first.loc.join('.') : '';
              message = loc ? `${loc}: ${first.msg}` : first.msg;
            } else {
              message = JSON.stringify(rawDetail);
            }
          } else {
            message = JSON.stringify(rawDetail);
          }
          const enrichedError = new Error(`API Error (${error.response.status}): ${message}`) as Error & {
            status?: number;
            raw?: any;
          };
          enrichedError.status = Number(error.response.status);
          enrichedError.raw = error;
          throw enrichedError;
        } else if (error.request) {
          // 请求发送但没有收到响应
          const networkError = new Error('网络错误: 无法连接到回测服务，请检查服务是否启动') as Error & {
            status?: number;
            raw?: any;
          };
          networkError.raw = error;
          throw networkError;
        } else {
          // 其他错误
          const requestError = new Error(`请求错误: ${error.message}`) as Error & {
            status?: number;
            raw?: any;
          };
          requestError.raw = error;
          throw requestError;
        }
      }
    );

    this.client.interceptors.request.use((config) => {
      const token = authService.getAccessToken();
      if (token) {
        config.headers.Authorization = `Bearer ${token}`;
      }
      return config;
    });
  }

  // ==========================================================================
  // 核心回测功能
  // ==========================================================================

  /**
   * 运行回测
   * @param config 回测配置
   * @returns 回测结果（初始状态）
   */
  async runBacktest(config: BacktestConfig): Promise<BacktestResult> {
    console.log('🚀 提交回测任务:', config);

    if (!config.user_id?.trim()) {
      throw new Error('用户ID不能为空');
    }

    const strategyParams = config.strategy_params || {};
    const tenantId = authService.getTenantId() || 'default'; // 从 authService 获取租户 ID

    const buyCost = Number(
      config.buy_cost ?? strategyParams.buy_cost ?? strategyParams.open_cost
    );
    const sellCost = Number(
      config.sell_cost ?? strategyParams.sell_cost ?? strategyParams.close_cost
    );
    const hasBuyCost = Number.isFinite(buyCost);
    const hasSellCost = Number.isFinite(sellCost);

    const dynamicPositionRaw =
      config.dynamic_position ?? strategyParams.dynamic_position;
    const dynamicPosition =
      typeof dynamicPositionRaw === 'string'
        ? ['1', 'true', 'yes', 'on'].includes(dynamicPositionRaw.toLowerCase())
        : Boolean(dynamicPositionRaw);

    const marketStateWindow = Number(
      config.market_state_window ?? strategyParams.market_state_window
    );
    const strategyTotalPosition = Number(
      config.strategy_total_position ?? strategyParams.strategy_total_position
    );

    const payload: Record<string, any> = {
      strategy_type: config.strategy_type || 'TopkDropout',
      strategy_params: {
        ...strategyParams,
        signal: strategyParams.signal || '<PRED>',
      },
      start_date: config.start_date,
      end_date: config.end_date,
      initial_capital: config.initial_capital,
      benchmark: config.benchmark_symbol || 'SH000300',
      universe: this.buildUniverse(config.symbol),
      // 基础费率，后端会据此计算详细费用
      commission: config.commission ?? 0.00025,
      user_id: this.normalizeUserId(config.user_id),
      tenant_id: tenantId,
      seed: config.seed,
      deal_price: config.deal_price || 'close',
      is_third_party: config.is_third_party ?? (config.strategy_type === 'CustomStrategy'),
      dynamic_position: dynamicPosition,
      market_state_symbol:
        config.market_state_symbol ?? strategyParams.market_state_symbol,
      style: config.style ?? strategyParams.style,
    };

    if (hasBuyCost) {
      payload.buy_cost = buyCost;
    }
    if (hasSellCost) {
      payload.sell_cost = sellCost;
    }
    if (Number.isFinite(marketStateWindow) && marketStateWindow > 0) {
      payload.market_state_window = marketStateWindow;
    }
    if (
      Number.isFinite(strategyTotalPosition) &&
      strategyTotalPosition >= 0 &&
      strategyTotalPosition <= 1
    ) {
      payload.strategy_total_position = strategyTotalPosition;
    }

    if (config.strategy_code?.trim()) {
      payload.strategy_content = config.strategy_code;
    }

    // 使用异步模式提交：立即返回 task_id，由前端轮询/WebSocket 获取结果。
    // 避免 WeightStrategy / 大股票池 等场景的同步回测超过网关超时（504）。
    const response = await this.client.post<BacktestResult>('/backtest', payload, {
      params: { async_mode: true },
    });

    console.log('✅ 回测任务已提交:', response.data);
    return response.data;
  }

  /**
   * 记录前端错误到后端日志 (logs/backtest_worker.log)
   */
  async logError(errorData: {
    backtest_id?: string;
    message: string;
    stack?: string;
    user_id?: string;
  }): Promise<void> {
    try {
      const tenantId = authService.getTenantId() || 'default';
      await this.client.post('/log_error', {
        ...errorData,
        tenant_id: tenantId,
      });
    } catch (e) {
      // 记录日志失败不应阻塞业务，仅在控制台警告
      console.warn('Failed to send error log to backend:', e);
    }
  }

  /**
   * 获取回测结果 (支持摘要模式以加速加载)
   * @param backtestId 回测ID
   * @param excludeTrades 是否排除交易清单以加速初期渲染 (默认为 true)
   * @returns 回测结果
   */
  async getResult(backtestId: string, excludeTrades: boolean = true): Promise<BacktestResult> {
    console.log(`📊 获取回测结果 (${excludeTrades ? '摘要模式' : '完整模式'}):`, backtestId);
    const tenantId = authService.getTenantId() || 'default';
    const response = await this.client.get<BacktestResult>(`/results/${backtestId}`, {
      params: { 
        tenant_id: tenantId,
        exclude_trades: excludeTrades 
      },
    });
    return response.data;
  }

  /**
   * 获取回测的详细交易清单和持仓
   * @param backtestId 回测ID
   */
  async getTrades(backtestId: string): Promise<{ trades: Trade[]; positions: any[] }> {
    console.log('📜 获取回测交易清单:', backtestId);
    const tenantId = authService.getTenantId() || 'default';
    const response = await this.client.get<any>(`/results/${backtestId}/trades`, {
      params: { tenant_id: tenantId },
    });
    return response.data;
  }

  // ==========================================================================
  // 历史记录管理
  // ==========================================================================

  /**
   * 获取回测历史
   * @param userId 用户ID
   * @param filters 过滤条件
   * @returns 回测历史列表
   */
  async getHistory(
    userId: string,
    filters?: HistoryFilter
  ): Promise<BacktestResult[]> {
    const targetUserId = this.normalizeUserId(userId);
    console.log('📜 获取回测历史:', { targetUserId, filters });

    const tenantId = authService.getTenantId(); // 获取当前租户ID
    const params = new URLSearchParams();
    params.append('tenant_id', tenantId || 'default'); // 注入必填的 tenant_id

    if (filters?.status) params.append('status', filters.status);
    if (filters?.symbol) params.append('symbol', filters.symbol);
    if (filters?.start_date) params.append('start_date', filters.start_date);
    if (filters?.end_date) params.append('end_date', filters.end_date);
    if (filters?.sort_by) params.append('sort_by', filters.sort_by);
    if (filters?.sort_order) params.append('sort_order', filters.sort_order);
    if (filters?.page) params.append('page', filters.page.toString());
    if (filters?.page_size) params.append('page_size', filters.page_size.toString());

    try {
      const url = `/history/${targetUserId}?${params.toString()}`;
      const response = await this.client.get<any>(url);

      // 后端返回格式为 {page, page_size, total, backtests}
      const data = response.data;
      const list: BacktestResult[] = Array.isArray(data?.backtests) ? data.backtests : [];
      const total = data?.total || 0;

      console.log(`✅ 获取到 ${list.length} 条历史记录 (Total: ${total}, user_id: ${targetUserId})`);
      return list;
    } catch (error: any) {
      console.error(`❌ 获取历史记录失败 (user_id: ${targetUserId}):`, error);
      throw error;
    }
  }

  /**
   * 删除回测记录
   * @param backtestId 回测ID
   */
  async deleteBacktest(backtestId: string, userId: string): Promise<void> {
    const targetUserId = this.normalizeUserId(userId);
    console.log('🗑️  删除回测记录:', { backtestId, targetUserId });

    const tenantId = authService.getTenantId() || 'default';
    await this.client.delete(`/results/${backtestId}`, {
      params: {
        user_id: targetUserId,
        tenant_id: tenantId
      },
    });

    console.log('✅ 回测记录已删除');
  }

  // ==========================================================================
  // 策略对比
  // ==========================================================================

  /**
   * 对比两个回测结果
   * @param backtestId1 第一个回测ID
   * @param backtestId2 第二个回测ID
   * @returns 对比结果
   */
  async compareBacktests(
    backtestId1: string,
    backtestId2: string,
    userId: string
  ): Promise<ComparisonResult> {
    console.log('⚖️  对比回测:', { backtestId1, backtestId2, userId });
    const tenantId = authService.getTenantId() || 'default';

    const response = await this.client.get<ComparisonResult>(
      `/compare/${backtestId1}/${backtestId2}`,
      { params: { user_id: this.normalizeUserId(userId), tenant_id: tenantId } }
    );

    console.log('✅ 对比完成');
    return response.data;
  }

  // ==========================================================================
  // 参数优化
  // ==========================================================================

  /**
   * 启动参数优化
   * @param config 优化配置
   * @returns 优化结果（初始状态）
   */
  async optimizeParameters(
    config: OptimizationConfig,
    options?: OptimizationProgressOptions
  ): Promise<OptimizationResult> {
    console.log('🔧 启动参数优化:', config);

    const response = await this.client.post<OptimizationTaskResponse>(
      '/optimize',
      {
        base_request: {
          strategy_type: 'TopkDropout',
          strategy_params: {
            topk: 50,
            n_drop: 10,
            signal: '<PRED>',
          },
          strategy_content: config.strategy_code,
          start_date: config.start_date,
          end_date: config.end_date,
          initial_capital: config.initial_capital,
          benchmark: 'SH000300',
          universe: this.buildUniverse(config.symbol),
          // 简单模式仅支持佣金费率
          commission: 0.00025,
          min_commission: 5.0,
          user_id: config.user_id,
        },
        param_ranges: config.param_ranges.map((param) => ({
          name: param.name,
          min: param.min,
          max: param.max,
          step: param.step,
        })),
        optimization_target: config.optimization_target,
        max_parallel: 5,
      },
      { params: { async_mode: true } }
    );

    const taskInfo = response.data;
    if (!taskInfo?.task_id) {
      throw new Error('优化任务提交失败，未返回 task_id');
    }

    if (options?.onTaskCreated) {
      options.onTaskCreated(taskInfo.task_id);
    }

    const resultData = await this.pollOptimizationTask<any>(
      taskInfo.task_id,
      options?.onProgress,
      options?.pollIntervalMs
    );

    const bestFitness = resultData?.all_results?.length
      ? Math.max(
        ...resultData.all_results.map(
          (item: any) => item.metrics?.[resultData.target_metric] || 0
        )
      )
      : undefined;

    const normalized: OptimizationResult = {
      optimization_id: resultData.optimization_id,
      status: 'completed',
      progress: 1,
      best_params: resultData.best_params,
      best_fitness: bestFitness,
      generation_stats: [],
    };

    if (normalized.optimization_id) {
      this.optimizationResults.set(normalized.optimization_id, normalized);
    }

    console.log('✅ 优化任务已提交:', normalized);
    return normalized;
  }

  /**
   * 启动 Qlib 参数优化
   */
  async optimizeQlibParameters(
    config: QlibOptimizationConfig,
    options?: OptimizationProgressOptions
  ): Promise<any> {
    console.log('🔧 启动 Qlib 参数优化:', config);

    const baseRequest = {
      strategy_type: config.qlib_strategy_type,
      strategy_params: {
        ...config.qlib_strategy_params,
        signal: '<PRED>',
      },
      start_date: config.start_date,
      end_date: config.end_date,
      initial_capital: config.initial_capital,
      benchmark: config.benchmark_symbol || 'SH000300',
      universe: this.buildUniverse(config.symbol || ''),

      // 使用标准 A 股费率字段
      commission: config.commission ?? 0.00025,
      min_commission: config.min_commission ?? 5.0,
      stamp_duty: config.stamp_duty ?? 0.0005,
      transfer_fee: config.transfer_fee ?? 0.00001,
      min_transfer_fee: 0.0, // 目前前端暂未配置该项，后端默认为 0.01 或可显式传递 0

      user_id: config.user_id,
    };

    if (config.generations || config.population_size) {
      const response = await this.client.post<OptimizationTaskResponse>(
        '/optimize/genetic',
        {
          base_request: baseRequest,
          param_ranges: config.param_ranges,
          optimization_target: config.optimization_target,
          population_size: config.population_size,
          generations: config.generations,
          mutation_rate: config.mutation_rate ?? 0.1,
          max_parallel: config.max_parallel ?? 5,
        },
        { params: { async_mode: true } }
      );
      const taskInfo = response.data;
      if (!taskInfo?.task_id) {
        throw new Error('遗传优化提交失败，未返回 task_id');
      }
      if (options?.onTaskCreated) {
        options.onTaskCreated(taskInfo.task_id);
      }
      return await this.pollOptimizationTask(
        taskInfo.task_id,
        options?.onProgress,
        options?.pollIntervalMs,
        options?.onLog,
        options?.signal
      );
    }

    const response = await this.client.post<OptimizationTaskResponse>(
      '/optimize',
      {
        base_request: baseRequest,
        param_ranges: config.param_ranges,
        optimization_target: config.optimization_target,
        max_parallel: config.max_parallel ?? 5,
      },
      { params: { async_mode: true } }
    );
    const taskInfo = response.data;
    if (!taskInfo?.task_id) {
      throw new Error('参数优化提交失败，未返回 task_id');
    }

    if (options?.onTaskCreated) {
      options.onTaskCreated(taskInfo.task_id);
    }

    return await this.pollOptimizationTask(
      taskInfo.task_id,
      options?.onProgress,
      options?.pollIntervalMs,
      options?.onLog,
      options?.signal
    );
  }

  /**
   * 获取优化结果
   * @param optimizationId 优化ID
   * @returns 优化结果
   */
  async getOptimizationResult(optimizationId: string): Promise<OptimizationResult> {
    console.log('📊 获取优化结果:', optimizationId);

    const cached = this.optimizationResults.get(optimizationId);
    if (cached) {
      return cached;
    }

    throw new Error('优化结果不存在或未缓存');
  }

  async getOptimizationHistory(limit = 20): Promise<OptimizationHistoryItem[]> {
    const tenantId = authService.getTenantId() || 'default';
    const response = await this.client.get<OptimizationHistoryItem[]>('/optimization/history', {
      params: { limit, tenant_id: tenantId },
    });
    return Array.isArray(response.data) ? response.data : [];
  }

  async clearOptimizationHistory(): Promise<boolean> {
    const response = await this.client.delete('/optimization/history/clear');
    return response.data?.success;
  }

  async getOptimizationDetail(optimizationId: string): Promise<OptimizationHistoryDetail> {
    const tenantId = authService.getTenantId() || 'default';
    const response = await this.client.get<OptimizationHistoryDetail>(
      `/optimization/${optimizationId}`,
      { params: { tenant_id: tenantId } }
    );
    return response.data;
  }

  // ==========================================================================
  // 报告导出
  // ==========================================================================

  /**
   * 保存策略
   * @param strategyData 策略数据
   * @returns 保存结果
   */
  async saveStrategy(strategyData: {
    code: string;
    name: string;
    description?: string;
    category?: string;
    tags?: string[];
    parameters?: Record<string, any>;
  }): Promise<{ strategy_id: string; message: string }> {
    console.log('💾 保存策略:', strategyData.name);

    // strategies API 在 /api/v1/strategies，不在 /api/v1/qlib/strategies
    const token = authService.getAccessToken();
    const response = await this.client.post<{ strategy_id: string; message: string }>(
      `${SERVICE_URLS.QLIB_SERVICE}/api/v1/strategies`,
      strategyData,
      { baseURL: '', headers: token ? { Authorization: `Bearer ${token}` } : {} }
    );

    console.log('✅ 策略保存成功:', response.data);
    return response.data;
  }

  /**
   * 导出回测结果（CSV 格式）
   * @param backtestId 回测ID
   * @returns CSV Blob
   */
  async exportCSV(backtestId: string): Promise<Blob> {
    console.log('📄 导出回测数据 (CSV):', backtestId);
    const tenantId = authService.getTenantId() || 'default';

    // 已确认网关导出路由不可用时，直接走本地生成，避免重复 502 噪音
    if (this.exportRouteAvailable === false) {
      const result = await this.getResult(backtestId, false);
      let csvSource: any = result;
      const trades = Array.isArray((csvSource as any)?.trades) ? (csvSource as any).trades : [];
      if (!trades.length) {
        try {
          const detail = await this.getTrades(backtestId);
          csvSource = {
            ...csvSource,
            trades: Array.isArray(detail?.trades) ? detail.trades : [],
            positions: Array.isArray(detail?.positions) ? detail.positions : (csvSource as any)?.positions,
          };
        } catch (err) {
          console.warn('本地导出补取交易明细失败:', err);
        }
      }
      const csv = this.buildCsvFromBacktestResult(csvSource);
      return new Blob(['\uFEFF' + csv], { type: 'text/csv;charset=utf-8' });
    }

    try {
      const response = await this.client.get(`/export/${backtestId}/csv`, {
        responseType: 'blob',
        params: { tenant_id: tenantId },
      });
      this.exportRouteAvailable = true;
      console.log('✅ CSV 已生成');
      return response.data;
    } catch (error: any) {
      const status = Number(error?.status || error?.response?.status || 0);
      // 部分网关仅保留历史 /pdf 转发规则，失败时自动回退
      if (status === 404 || status === 502) {
        console.warn('CSV 路径不可用，回退到兼容导出路径 /pdf');
        try {
          const fallback = await this.client.get(`/export/${backtestId}/pdf`, {
            responseType: 'blob',
            params: { tenant_id: tenantId },
          });
          this.exportRouteAvailable = true;
          console.log('✅ CSV 已生成 (fallback:/pdf)');
          return fallback.data;
        } catch (fallbackError: any) {
          const fallbackStatus = Number(
            fallbackError?.status || fallbackError?.response?.status || 0
          );
          if (fallbackStatus === 404 || fallbackStatus === 502) {
            this.exportRouteAvailable = false;
            console.warn('导出接口不可用，回退到前端本地 CSV 生成');
            const result = await this.getResult(backtestId, false);
            let csvSource: any = result;
            const trades = Array.isArray((csvSource as any)?.trades) ? (csvSource as any).trades : [];
            if (!trades.length) {
              try {
                const detail = await this.getTrades(backtestId);
                csvSource = {
                  ...csvSource,
                  trades: Array.isArray(detail?.trades) ? detail.trades : [],
                  positions: Array.isArray(detail?.positions) ? detail.positions : (csvSource as any)?.positions,
                };
              } catch (err) {
                console.warn('本地导出补取交易明细失败:', err);
              }
            }
            const csv = this.buildCsvFromBacktestResult(csvSource);
            return new Blob(['\uFEFF' + csv], { type: 'text/csv;charset=utf-8' });
          }
          throw fallbackError;
        }
      }
      throw error;
    }
  }

  /**
   * 导出回测结果（JSON 原始文件）
   * @param backtestId 回测ID
   * @returns JSON Blob
   */
  async exportJSON(backtestId: string): Promise<Blob> {
    console.log('🧾 导出回测原始数据 (JSON):', backtestId);
    const result = await this.getResult(backtestId, false);
    const json = JSON.stringify(result, null, 2);
    return new Blob([json], { type: 'application/json;charset=utf-8' });
  }

  // 兼容旧调用名称
  async exportPDF(backtestId: string): Promise<Blob> {
    return this.exportCSV(backtestId);
  }

  async exportExcel(backtestId: string): Promise<Blob> {
    return this.exportCSV(backtestId);
  }

  async getQlibHealth(): Promise<QlibHealthStatus> {
    const response = await this.client.get<QlibHealthStatus>('/health');
    return response.data;
  }

  // ==========================================================================
  // 市场数据
  // ==========================================================================

  /**
   * 获取市场数据
   * @param symbol 股票代码
   * @param startDate 开始日期
   * @param endDate 结束日期
   * @returns 市场数据
   */
  async getMarketData(
    symbol: string,
    startDate?: string,
    endDate?: string
  ): Promise<MarketData> {
    console.log('📈 获取市场数据:', { symbol, startDate, endDate });

    const params = new URLSearchParams();
    if (startDate) params.append('start_date', startDate);
    if (endDate) params.append('end_date', endDate);

    const url = `/market-data/${symbol}${params.toString() ? '?' + params.toString() : ''}`;
    const response = await this.client.get<MarketData>(url);

    console.log(`✅ 获取到 ${response.data.data.length} 条市场数据`);
    return response.data;
  }

  // ==========================================================================
  // WebSocket 实时进度
  // ==========================================================================

  /**
   * 连接 WebSocket 监听回测进度
   * @param backtestId 回测ID
   * @param callbacks 回调函数
   * @returns WebSocket 连接
   */
  connectProgress(
    backtestId: string,
    callbacks: ProgressCallbacks
  ): WebSocket {
    const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const token = authService.getAccessToken();
    const tenantId = authService.getTenantId() || 'default';
    const storedUser = authService.getStoredUser() as any;
    const userId = this.normalizeUserId(String(storedUser?.id ?? storedUser?.user_id ?? ''));
    const query = new URLSearchParams();
    if (token) query.set('token', token);
    if (tenantId) query.set('tenant_id', tenantId);
    if (userId) query.set('user_id', userId);
    const wsUrl = `${wsProtocol}//${this.wsUrl}/ws/backtest/${backtestId}?${query.toString()}`;

    console.log('🔗 连接 WebSocket:', wsUrl);

    const ws = new WebSocket(wsUrl);

    ws.onopen = () => {
      console.log('✅ WebSocket 已连接');
    };

    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);

        // 处理日志消息
        if (data.type === 'log') {
          if (callbacks.onLog) callbacks.onLog(data.message);
          return;
        }

        // 兼容后端当前推送格式：{backtest_id, status, progress, ...}
        const status = data.status as string | undefined;
        const progress = typeof data.progress === 'number' ? data.progress : undefined;

        if (status === 'completed' && callbacks.onComplete) {
          callbacks.onComplete(data);
        } else if (status === 'failed' && callbacks.onError) {
          const err = new Error(data.error_message || '回测失败');
          (err as any).traceback = data.full_error || data.error_message;
          callbacks.onError(err);
        } else if (progress !== undefined && callbacks.onProgress) {
          callbacks.onProgress(progress, data.message || '');
        }
      } catch (error) {
        console.error('解析 WebSocket 消息失败:', error);
      }
    };

    ws.onerror = (error) => {
      console.error('WebSocket 错误:', error);
      if (callbacks.onError) {
        callbacks.onError(new Error('WebSocket 连接错误'));
      }
    };

    ws.onclose = () => {
      console.log('🔌 WebSocket 已断开');
    };

    return ws;
  }

  /**
   * 轮询回测状态（无 WS 场景）
   */
  pollStatus(
    backtestId: string,
    callbacks: {
      onProgress?: (progress: number, status?: string, message?: string) => void;
      onComplete?: (result: BacktestResult) => void;
      onError?: (error: Error) => void;
      onLog?: (message: string) => void;
    },
    intervalMs = 2000
  ): () => void {
    let cancelled = false;
    let notFoundStreak = 0;
    let logIndex = 0;
    const maxNotFoundStreak = 5;

    const isTerminalPollError = (err: unknown): boolean => {
      const status = Number((err as any)?.response?.status);
      if ([401, 403, 404].includes(status)) {
        return true;
      }
      const message = err instanceof Error ? err.message : String(err || '');
      return (
        message.includes('API Error (401)') ||
        message.includes('API Error (403)') ||
        message.includes('API Error (404)')
      );
    };

    const poll = async () => {
      if (cancelled) return;
      try {
        const tenantId = authService.getTenantId() || 'default';
        const response = await this.client.get<any>(`/backtest/${backtestId}/status`, {
          params: { tenant_id: tenantId },
        });
        const status = response.data?.status;
        const progress = typeof response.data?.progress === 'number' ? response.data.progress : 0;
        const message = response.data?.message;

        if (callbacks.onLog) {
          try {
            const logData = await this.getBacktestLogs(backtestId, logIndex);
            if (logData.logs && logData.logs.length > 0) {
              logData.logs.forEach((log) => callbacks.onLog?.(log));
              logIndex = logData.next_index;
            }
          } catch (logErr) {
            console.warn('Backtest log polling failed', logErr);
          }
        }

        if (status === 'not_found') {
          notFoundStreak += 1;
          callbacks.onProgress?.(0, status, message);
          if (notFoundStreak >= maxNotFoundStreak) {
            callbacks.onError?.(new Error('回测任务状态丢失，请重试或检查后端任务队列'));
            return;
          }
          setTimeout(poll, intervalMs);
          return;
        }
        notFoundStreak = 0;
        callbacks.onProgress?.(progress, status, message);
        if (status === 'completed') {
          if (callbacks.onLog) {
            try {
              const logData = await this.getBacktestLogs(backtestId, logIndex);
              if (logData.logs && logData.logs.length > 0) {
                logData.logs.forEach((log) => callbacks.onLog?.(log));
                logIndex = logData.next_index;
              }
            } catch (logErr) {
              console.warn('Final backtest log polling failed', logErr);
            }
          }
          // 完成态需要完整交易明细，否则结果页只能拿到摘要数据，调仓交易日会显示为空。
          const result = await this.getResult(backtestId, false);
          callbacks.onComplete?.(result);
          return;
        }
        if (status === 'failed') {
          if (callbacks.onLog) {
            try {
              const logData = await this.getBacktestLogs(backtestId, logIndex);
              if (logData.logs && logData.logs.length > 0) {
                logData.logs.forEach((log) => callbacks.onLog?.(log));
                logIndex = logData.next_index;
              }
            } catch (logErr) {
              console.warn('Final backtest log polling failed', logErr);
            }
          }
          const err = new Error(response.data?.error_message || '回测失败');
          (err as any).traceback = response.data?.full_error || response.data?.error_message;
          callbacks.onError?.(err);
          return;
        }
        setTimeout(poll, intervalMs);
      } catch (error: any) {
        if (cancelled) return;
        if (isTerminalPollError(error)) {
          callbacks.onError?.(error);
          return;
        }
        // 非终态错误（网络抖动/服务短暂不可用）持续重试，直到后端返回 completed/failed
        callbacks.onProgress?.(0, 'retrying');
        setTimeout(poll, intervalMs);
      }
    };

    poll();
    return () => {
      cancelled = true;
    };
  }

  /**
   * 获取回测日志
   */
  async getBacktestLogs(
    backtestId: string,
    startIndex: number = 0
  ): Promise<BacktestLogsResponse> {
    try {
      const tenantId = authService.getTenantId() || 'default';
      const response = await this.client.get<BacktestLogsResponse>(
        `/logs/${backtestId}`,
        { params: { start_index: startIndex, tenant_id: tenantId } }
      );
      return response.data;
    } catch (error) {
      console.warn('Fetch backtest logs failed:', error);
      return { backtest_id: backtestId, logs: [], next_index: startIndex };
    }
  }

  /**
   * 获取优化任务日志
   */
  async getOptimizationLogs(
    optimizationId: string,
    startIndex: number = 0
  ): Promise<{ logs: string[]; next_index: number }> {
    try {
      const tenantId = authService.getTenantId() || 'default';
      const response = await this.client.get<{ logs: string[]; next_index: number }>(
        `/logs/${optimizationId}`,
        { params: { start_index: startIndex, tenant_id: tenantId } }
      );
      return response.data;
    } catch (error) {
      console.warn('Fetch logs failed:', error);
      return { logs: [], next_index: startIndex };
    }
  }

  /**
   * 停止任务
   */
  async stopTask(taskId: string): Promise<void> {
    await this.client.post(`/task/${taskId}/stop`);
  }

  async watchOptimizationTask<T>(
    taskId: string,
    options?: OptimizationProgressOptions
  ): Promise<T> {
    return this.pollOptimizationTask(
      taskId,
      options?.onProgress,
      options?.pollIntervalMs,
      options?.onLog,
      options?.signal
    );
  }

  /**
   * 轮询参数优化任务状态
   */
  private async pollOptimizationTask<T>(
    taskId: string,
    onProgress?: (progress: number, status?: string, info?: OptimizationRunStatus) => void,
    pollIntervalMs = 2000,
    onLog?: (message: string) => void,
    signal?: AbortSignal
  ): Promise<T> {
    const abortError = () => {
      const err = new Error('优化任务已取消');
      err.name = 'AbortError';
      return err;
    };

    if (signal?.aborted) {
      throw abortError();
    }

    // 获取 optimization_id
    let optimizationId: string | undefined;
    try {
      const initialStatus = await this.client.get<TaskStatusResponse>(`/task/${taskId}/status`, {
        signal,
      });
      const info = initialStatus.data?.info as any;
      if (info && info.optimization_id) {
        optimizationId = info.optimization_id;
      }
    } catch (e) {
      console.warn('Failed to fetch initial task status', e);
    }

    // 日志轮询索引
    let logIndex = 0;

    return new Promise((resolve, reject) => {
      const poll = async () => {
        if (signal?.aborted) {
          reject(abortError());
          return;
        }
        try {
          // 1. 获取任务状态
          const response = await this.client.get<TaskStatusResponse>(
            `/task/${taskId}/status`,
            { signal }
          );
          const state = response.data?.state;
          const info = response.data?.info || {};
          const progress =
            typeof (info as any).progress === 'number'
              ? (info as any).progress
              : 0;
          const message =
            (info as any).message || (info as any).status || state;

          onProgress?.(progress, message, {
            optimization_id: optimizationId || (info as any).optimization_id,
            progress,
            status: String((info as any).status || state || ''),
            message,
            total_tasks: typeof (info as any).total_tasks === 'number' ? (info as any).total_tasks : undefined,
            completed_count: typeof (info as any).completed_count === 'number' ? (info as any).completed_count : undefined,
            failed_count: typeof (info as any).failed_count === 'number' ? (info as any).failed_count : undefined,
            current_params: (info as any).current_params,
            best_params: (info as any).best_params,
            best_metric_value: typeof (info as any).best_metric_value === 'number'
              ? (info as any).best_metric_value
              : undefined,
            result_summary: (info as any).result_summary,
          });

          // 如果还没拿到 optimizationId，尝试从本次轮询结果中获取
          if (!optimizationId && info && (info as any).optimization_id) {
            optimizationId = (info as any).optimization_id;
            console.log(`[Poll] Got optimization_id: ${optimizationId}`);
          }

          // 2. 轮询日志 (如果有 optimizationId)
          if (optimizationId && onLog) {
            try {
              const logData = await this.getOptimizationLogs(optimizationId, logIndex);
              if (logData.logs && logData.logs.length > 0) {
                console.log(`[Poll] Fetched ${logData.logs.length} logs for ${optimizationId}`);
                logData.logs.forEach(log => onLog(log));
                logIndex = logData.next_index;
              } else {
                // console.log(`[Poll] No new logs for ${optimizationId}`);
              }
            } catch (logErr) {
              console.warn('Log polling failed', logErr);
            }
          }

          if (state === 'SUCCESS') {
            const result = (info as any).result ?? info;
            resolve(result as T);
            return;
          }
          if (state === 'FAILURE') {
            reject(new Error((info as any).error || '优化任务失败'));
            return;
          }
          setTimeout(poll, pollIntervalMs);
        } catch (error) {
          if (signal?.aborted) {
            reject(abortError());
            return;
          }
          reject(error);
        }
      };

      poll();
    });
  }

  // ==========================================================================
  // 工具方法
  // ==========================================================================

  /**
   * 下载文件
   * @param blob 文件 Blob
   * @param filename 文件名
   */
  downloadFile(blob: Blob, filename: string): void {
    const url = window.URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    window.URL.revokeObjectURL(url);
  }

  /**
   * 下载 CSV 报告（便捷方法）
   * @param backtestId 回测ID
   */
  async downloadCSV(backtestId: string): Promise<void> {
    const blob = await this.exportCSV(backtestId);
    this.downloadFile(blob, `backtest_report_${backtestId}.csv`);
  }

  /**
   * 下载 JSON 原始文件（便捷方法）
   * @param backtestId 回测ID
   */
  async downloadJSON(backtestId: string): Promise<void> {
    const blob = await this.exportJSON(backtestId);
    this.downloadFile(blob, `backtest_raw_${backtestId}.json`);
  }

  // 兼容旧调用名称
  async downloadPDF(backtestId: string): Promise<void> {
    await this.downloadCSV(backtestId);
  }

  async downloadExcel(backtestId: string): Promise<void> {
    await this.downloadCSV(backtestId);
  }

  /**
   * 获取 Qlib 数据日期范围
   * 用于前端日期选择器限制
   */
  async getQlibDataRange(): Promise<{
    exists: boolean;
    min_date: string | null;
    max_date: string | null;
    total_trading_days: number;
  }> {
    try {
      const userApiUrl = SERVICE_URLS.USER_SERVICE;
      const token = authService.getAccessToken();
      const response = await axios.get(`${userApiUrl}/api/v1/models/qlib-data-range`, {
        headers: {
          Authorization: `Bearer ${token}`,
        },
      });
      return response.data;
    } catch (e) {
      console.warn('Failed to fetch qlib data range:', e);
      return { exists: false, min_date: null, max_date: null, total_trading_days: 0 };
    }
  }
}

// 导出单例实例
export const backtestService = new BacktestService();

// 默认导出服务类
export default BacktestService;
