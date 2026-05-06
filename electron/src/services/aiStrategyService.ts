// AI策略服务API接口
import axios from 'axios';
import { authService } from '../features/auth/services/authService';
import {
  StrategyParams, Strategy, BacktestResult,
  StrategyTemplate, TemplateMatch, ValidationResult,
  ParameterValidationResult,
  ProviderPerformance, SystemPerformance, PerformanceAlert,
  FileInfo
} from '../types/strategy';
import { AIStrategyServiceFilesMixin } from './aiStrategyServiceFiles';

// 基础配置 - 使用统一端口配置
import { SERVICE_ENDPOINTS, SERVICE_URLS } from '../config/services';

// 动态解析函数定义，确保在用户配置 IP 后能实时获取最新地址
const resolveAiStrategyBaseURL = () => String(SERVICE_ENDPOINTS.AI_STRATEGY || '').replace(/\/+$/, '');
const resolveBacktestBaseURL = () => String(SERVICE_URLS.ENGINE_SERVICE || '').replace(/\/+$/, '');

// 创建axios实例
const apiClient = axios.create({
  timeout: 300000, // 增加到5分钟，因为DeepSeek API可能需要较长时间
  headers: {
    'Content-Type': 'application/json',
  },
});

// 创建回测服务专用客户端
const backtestClient = axios.create({
  timeout: 180000, // 回测可能需要更长时间
  headers: {
    'Content-Type': 'application/json',
  },
});

// 数据类型定义
export interface StrategyGenerationRequest {
  description: string;
  market?: string;
  risk_level?: 'low' | 'medium' | 'high';
  style?: string;
  user_id?: string;
}

export interface StrategyCodeArtifact {
  filename: string;
  language: string;
  code: string;
}

export interface StrategyMetadata {
  factors: string[];
  risk_controls: string[];
  assumptions: string[];
  notes: string;
}

export interface StrategyGenerationResult {
  strategy_name: string;
  rationale: string;
  artifacts: StrategyCodeArtifact[];
  metadata: StrategyMetadata;
  provider: string;
  generated_at: string;
}

export interface StrategyRecord {
  id: number;
  strategy_id: string;
  user_id?: string;
  name: string;
  description: string;
  market: string;
  risk_level: string;
  provider: string;
  code: string;
  factors?: string;
  risk_controls?: string;
  assumptions?: string;
  notes?: string;
  created_at: string;
}

export interface ProviderInfo {
  name: string;
  display_name: string;
  status: 'available' | 'unavailable';
  description: string;
}

export interface HealthStatus {
  service: string;
  models: Record<string, unknown>;
}

export interface PerformanceMetrics {
  model_name: string;
  avg_response_time: number;
  success_rate: number;
  error_rate: number;
  total_requests: number;
}

// 添加请求和响应拦截器
apiClient.interceptors.request.use(
  (config) => {
    config.baseURL = resolveAiStrategyBaseURL();
    apiClient.defaults.baseURL = config.baseURL;
    // 添加认证 Token
    const token = authService.getAccessToken();
    if (token) {
      if (config.headers && typeof config.headers.set === 'function') {
        config.headers.set('Authorization', `Bearer ${token}`);
      } else {
        config.headers.Authorization = `Bearer ${token}`;
      }
    }
    console.log('API请求:', config.method?.toUpperCase(), config.url);
    return config;
  },
  (error) => {
    console.error('API请求错误:', error);
    return Promise.reject(error);
  }
);

apiClient.interceptors.response.use(
  (response) => {
    console.log('API响应:', response.status, response.config.url);
    return response;
  },
  (error) => {
    console.error('API响应错误:', error);
    if (error.response?.status === 401) {
      console.error('认证失败，请检查认证状态');
    }
    return Promise.reject(error);
  }
);

// 为回测客户端也添加拦截器
backtestClient.interceptors.request.use(
  (config) => {
    config.baseURL = resolveBacktestBaseURL();
    backtestClient.defaults.baseURL = config.baseURL;
    const token = authService.getAccessToken();
    if (token) {
      if (config.headers && typeof config.headers.set === 'function') {
        config.headers.set('Authorization', `Bearer ${token}`);
      } else {
        config.headers.Authorization = `Bearer ${token}`;
      }
    }
    return config;
  }
);

// API请求封装
class AIStrategyService extends AIStrategyServiceFilesMixin {
  private static instance: AIStrategyService;

  static getInstance(): AIStrategyService {
    if (!AIStrategyService.instance) {
      AIStrategyService.instance = new AIStrategyService();
    }
    return AIStrategyService.instance;
  }

  // 类型转换函数：AIStrategyParams -> StrategyParams
  protected async generateStrategyInternal(params: StrategyParams): Promise<Strategy> {
    console.log('开始生成策略:', params);

    // 验证必要参数
    if (!params.description?.trim()) {
      throw new Error('策略描述不能为空');
    }

    try {
      console.log('发送请求到:', apiClient.defaults.baseURL + '/strategy/generate');

      // 构建股票池上下文信息，帮助LLM更好地理解选股意图
      const stockPoolContext = params.symbols && params.symbols.length > 0
        ? `\n\n股票池信息：\n- 包含 ${params.symbols.length} 只股票\n- 股票代码：${params.symbols.join(', ')}\n- 请基于这些股票生成针对性的交易策略`
        : '';

      // 增强策略描述，将股票池信息注入到提示词中
      const enhancedDescription = params.description + stockPoolContext;

      const requestData = {
        description: enhancedDescription,
        market: params.market || 'CN',
        risk_level: params.riskLevel || 'medium',
        style: params.style || 'custom',
        symbols: params.symbols || [],
        timeframe: params.timeframe || '1d',
        // 基础参数
        initial_capital: params.initialCapital || 100000,
        position_size: params.positionSize || 10,
        max_positions: params.maxPositions || 5,
        stop_loss: params.stopLoss || 5,
        take_profit: params.takeProfit || 20,
        strategy_length: params.strategyLength || 'unlimited',
        backtest_period: params.backtestPeriod || '1year',
        // 新增高级参数
        max_drawdown: params.maxDrawdown || undefined,
        commission_rate: params.commissionRate || undefined,
        slippage: params.slippage || undefined,
        benchmark: params.benchmark || undefined,
        user_id: 'desktop-user'
      };

      console.log('请求数据:', requestData);

      const response = await apiClient.post('/strategy/generate', requestData);

      console.log('API响应状态:', response.status);
      console.log('API响应数据:', response.data);

      // 统一解包 success/data 格式
      const data = response.data?.data ?? response.data;
      const payload = data?.data ?? data;  // 双层 data 容错
      let code = '';
      let rationale = '';
      const metadata = payload?.metadata ?? {};

      // 优先从artifacts中获取代码
      if (payload?.artifacts?.[0]?.code) {
        code = this.normalizeCode(payload.artifacts[0].code);
      } else if (payload?.python_code) {
        code = this.normalizeCode(payload.python_code);
      } else if (payload?.code) {
        code = this.normalizeCode(payload.code);
      }

      // 获取策略说明，处理部分响应的情况
      rationale = payload?.rationale ?? data?.rationale ?? '';
      if (rationale.includes('部分响应') && code) {
        rationale = `AI生成策略：${params.description}`;
      }

      // 确保metadata结构完整
      if (!metadata.factors) metadata.factors = [];
      if (!metadata.risk_controls) metadata.risk_controls = [];
      if (!metadata.assumptions) metadata.assumptions = [];
      if (!metadata.notes) metadata.notes = rationale.includes('部分响应') ? '策略代码已从AI响应中完整提取' : '';

      // 转换后端响应格式到前端格式
      const displayMetadata: StrategyMetadata = {
        factors: metadata?.factors || payload?.factors || [],
        risk_controls: metadata?.risk_controls || payload?.risk_controls || [],
        assumptions: metadata?.assumptions || payload?.assumptions || [],
        notes: metadata?.notes || payload?.notes || ''
      };

      const strategy: Strategy = {
        id: data.persisted_id || payload?.id || this.generateId(),
        name:
          payload?.strategy_name ||
          data?.strategy_name ||
          this.generateStrategyName(params.description),
        description: params.description,
        code,
        parameters: params,
        metadata: {
          createdAt: new Date(),
          updatedAt: new Date(),
          version: 1,
          tags: this.extractTags(params.description, params.riskLevel),
          rationale,
          factors: metadata?.factors || payload?.factors || [],
          riskControls: metadata?.risk_controls || payload?.risk_controls || [],
          assumptions: metadata?.assumptions || payload?.assumptions || [],
          notes: metadata?.notes || payload?.notes
        },
        conversation: [{
          id: this.generateId(),
          role: 'user',
          content: `请生成一个${params.description}策略`,
          timestamp: new Date(),
          type: 'text'
        }, {
          id: this.generateId(),
          role: 'assistant',
          content: this.formatAIResponse(rationale, code, params.description, displayMetadata, params),
          timestamp: new Date(),
          type: 'text'
        }]
      };

      console.log('策略生成成功:', strategy);
      return strategy;
    } catch (error) {
      console.error('生成策略失败:', error);
      if (axios.isAxiosError(error)) {
        const status = error.response?.status;
        const message = error.response?.data?.message || error.message;

        if (status === 401) {
          throw new Error('API认证失败，请检查DeepSeek API密钥配置');
        } else if (status === 429) {
          throw new Error('API请求频率过高，请稍后重试');
        } else if (status && status >= 500) {
          throw new Error('服务器内部错误，请稍后重试');
        } else if (error.code === 'ECONNABORTED' || error.message.includes('timeout')) {
          throw new Error('策略生成超时，AI模型需要较长时间处理，建议稍后重试或使用流式生成');
        } else {
          throw new Error(`策略生成失败: ${message}`);
        }
      }
      throw new Error('策略生成失败，请检查网络连接或稍后重试');
    }
  }

  // 完善策略（多轮对话）
  async refineStrategy(strategyId: string, feedback: string, currentCode: string, params?: StrategyParams): Promise<{
    code: string;
    explanation: string;
    type: 'text' | 'code';
  }> {
    console.log('完善策略:', { strategyId, feedback });

    // 验证必要参数
    if (!feedback?.trim()) {
      throw new Error('完善建议不能为空');
    }

    try {
      const requestData: Record<string, unknown> = {
        strategy_id: strategyId,
        feedback: feedback,
        current_code: currentCode,
        user_id: 'desktop-user'
      };

      // 如果提供了参数，构建更新后的策略参数
      if (params) {
        requestData.updated_params = {
          market: params.market || 'CN',
          risk_level: params.riskLevel || 'medium',
          symbols: params.symbols || [],
          timeframe: params.timeframe || '1d',
          initial_capital: params.initialCapital || 100000,
          position_size: params.positionSize || 10,
          max_positions: params.maxPositions || 5,
          stop_loss: params.stopLoss || 5,
          take_profit: params.takeProfit || 20,
          strategy_length: params.strategyLength || 'unlimited',
          backtest_period: params.backtestPeriod || '1year',
          // 新增高级参数
          max_drawdown: params.maxDrawdown || undefined,
          commission_rate: params.commissionRate || undefined,
          slippage: params.slippage || undefined,
          benchmark: params.benchmark || undefined
        };
      }

      // 使用新的策略完善接口
      const response = await apiClient.post(`/strategies/${strategyId}/refine`, requestData);

      const data = response.data?.data ?? response.data;

      // 适配新接口的响应格式
      const refinedCode = data?.refined_code || currentCode;
      const explanation = data?.explanation || '策略完善完成';

      return {
        code: refinedCode,
        explanation: explanation,
        type: 'code'
      };
    } catch (error) {
      console.error('完善策略失败:', error);
      if (axios.isAxiosError(error)) {
        const status = error.response?.status;
        const message = error.response?.data?.message || error.message;

        if (status === 404) {
          throw new Error('策略不存在或refine接口不可用');
        } else if (status === 401) {
          throw new Error('API认证失败，请检查DeepSeek API密钥配置');
        } else if (status === 429) {
          throw new Error('API请求频率过高，请稍后重试');
        } else if (status && status >= 500) {
          throw new Error('服务器内部错误，请稍后重试');
        } else {
          throw new Error(`策略完善失败: ${message}`);
        }
      }
      throw new Error('策略完善失败，请检查网络连接');
    }
  }

  // 运行回测
  async runBacktest(strategy: Strategy, options?: {
    startDate?: string;
    endDate?: string;
    initialCapital?: number;
  }): Promise<{ id: string }> {
    console.log('开始回测:', { strategyId: strategy.id, options, strategyParams: strategy.parameters });

    if (!strategy.code?.trim()) {
      throw new Error('策略代码不能为空');
    }

    try {
      const symbols: string[] = (strategy.parameters?.symbols && Array.isArray(strategy.parameters.symbols) && strategy.parameters.symbols.length > 0)
        ? strategy.parameters.symbols
        : ['000001.SZ'];

      const backtestRequest: Record<string, unknown> = {
        strategy_code: strategy.code,
        symbol: symbols[0],
        start_date: options?.startDate || '2023-01-01',
        end_date: options?.endDate || '2026-01-01',
        initial_capital: options?.initialCapital || strategy.parameters?.initialCapital || 100000,
        commission: strategy.parameters?.commissionRate ?? 0.0003,
        slippage: strategy.parameters?.slippage ?? 0.001,
        user_id: 'desktop-user',
        benchmark_symbol: strategy.parameters?.benchmark || 'SPY',
        strategy_params: {},
      };

      console.log('回测请求参数:', backtestRequest);

      const response = await backtestClient.post('/run', backtestRequest);
      const data = response.data;
      const id = data?.backtest_id || data?.id || this.generateId();
      return { id };
    } catch (error) {
      console.error('回测提交失败:', error);
      if (axios.isAxiosError(error)) {
        const status = error.response?.status;
        const message = error.response?.data?.message || error.message;

        if (status === 400) {
          throw new Error('回测参数错误，请检查策略代码和参数');
        } else if (status === 404) {
          throw new Error('回测服务不可用，请确认回测服务已启动');
        } else if (status && status >= 500) {
          throw new Error('回测服务内部错误，请稍后重试');
        } else {
          throw new Error(`回测失败: ${message}`);
        }
      }
      throw new Error('回测失败，请检查网络连接和回测服务状态');
    }
  }

  // 获取回测结果并转换为前端格式
  async getBacktestResult(backtestId: string): Promise<{ status: string; result?: BacktestResult }> {
    try {
      const resp = await backtestClient.get(`/results/${backtestId}`);
      const data = resp.data;
      const status = data?.status || 'unknown';
      if (status !== 'completed') {
        return { status };
      }
      const equity = Array.isArray(data.equity_curve) ? data.equity_curve.map((e: unknown) => {
        const ev = e as { date: string; value: number };
        return { date: ev.date, value: ev.value };
      }) : [];
      const result: BacktestResult = {
        id: backtestId,
        strategyId: '',
        performance: {
          totalReturn: data.total_return ?? 0,
          annualizedReturn: data.annual_return ?? 0,
          maxDrawdown: data.max_drawdown ?? 0,
          sharpeRatio: data.sharpe_ratio ?? 0,
          winRate: data.win_rate ?? 0,
          profitFactor: data.profit_factor ?? 0,
          calmarRatio: data.calmar_ratio ?? 0,
          sortinoRatio: data.sortino_ratio ?? 0,
        },
        trades: [],
        equity,
        charts: { equity: '', drawdown: '', returns: '', monthly: '' },
        parameters: {
          description: '',
          market: 'CN' as const,
          riskLevel: 'medium' as const,
          style: 'custom' as const,
          strategyLength: 'unlimited' as const,
          backtestPeriod: '1year' as const,
          timeframe: '1d' as const,
          symbols: [],
          initialCapital: 100000,
          positionSize: 0.1,
          maxPositions: 5,
          stopLoss: 5,
          takeProfit: 20,
        },
        statistics: {
          totalTrades: 0,
          winningTrades: 0,
          losingTrades: 0,
          avgWin: 0,
          avgLoss: 0,
          largestWin: 0,
          largestLoss: 0,
          avgTradeDuration: 0,
          profitFactor: 0,
        },
      };
      return { status, result };
    } catch (error) {
      console.error('获取回测结果失败:', error);
      throw error;
    }
  }

  // 保存策略
  async saveStrategy(strategy: Strategy): Promise<void> {
    console.log('保存策略:', strategy.id);

    if (!strategy.code?.trim()) {
      throw new Error('策略代码不能为空');
    }

    try {
      await apiClient.post('/strategies', {
        id: strategy.id,
        name: strategy.name,
        description: strategy.description,
        code: strategy.code,
        parameters: strategy.parameters,
        metadata: strategy.metadata,
        created_at: strategy.metadata.createdAt.toISOString()
      });

      // 保存到本地存储作为备份
      this.saveToLocalStorage(strategy);
    } catch (error) {
      console.error('保存策略失败:', error);
      if (axios.isAxiosError(error)) {
        const status = error.response?.status;
        const message = error.response?.data?.message || error.message;

        if (status === 400) {
          throw new Error('策略数据格式错误');
        } else if (status && status >= 500) {
          throw new Error('保存服务内部错误，请稍后重试');
        } else {
          throw new Error(`保存失败: ${message}`);
        }
      }
      // 网络错误时保存到本地存储
      this.saveToLocalStorage(strategy);
      throw new Error('保存策略失败，已保存到本地缓存');
    }
  }

  // 流式生成策略（SSE）
  async generateStrategyStream(params: StrategyParams): Promise<Strategy> {
    console.log('开始流式生成策略:', params);

    if (!params.description?.trim()) {
      throw new Error('策略描述不能为空');
    }

    return new Promise((resolve, reject) => {
      // 构建查询参数
      const queryParams = new URLSearchParams({
        description: params.description,
        market: params.market || 'CN',
        risk_level: params.riskLevel || 'medium',
        style: params.style || 'simple',
        symbols: (params.symbols || []).join(','),
        timeframe: params.timeframe || '1d',
        initial_capital: String(params.initialCapital || 100000),
        position_size: String(params.positionSize || 10),
        max_positions: String(params.maxPositions || 5),
        stop_loss: String(params.stopLoss || 5),
        take_profit: String(params.takeProfit || 20),
        strategy_length: params.strategyLength || 'unlimited',
        backtest_period: params.backtestPeriod || '1year',
        user_id: 'desktop-user'
      });

      // 由于原生 EventSource 不支持 Header，我们将 Token 放入 Query 参数中
      const token = authService.getAccessToken();
      if (token) {
        queryParams.append('token', token);
      }
      
      const eventSource = new EventSource(`${resolveAiStrategyBaseURL()}/strategy/generate/stream?${queryParams.toString()}`);

      const strategy: Strategy = {
        id: this.generateId(),
        name: this.generateStrategyName(params.description),
        description: params.description,
        code: '',
        parameters: params,
        metadata: {
          createdAt: new Date(),
          updatedAt: new Date(),
          version: 1,
          tags: this.extractTags(params.description, params.riskLevel),
          rationale: '',
          factors: [],
          riskControls: [],
          assumptions: [],
          notes: ''
        },
        conversation: []
      };

      eventSource.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          console.log('收到SSE数据:', data);

          if (data.type === 'progress') {
            // 处理进度更新
            console.log('生成进度:', data.message);
            // 这里可以触发UI更新回调
          } else if (data.type === 'code_chunk') {
            // 处理代码片段
            strategy.code += data.content + '\n';
          } else if (data.type === 'rationale') {
            // 处理策略说明
            strategy.metadata.rationale += data.content;
          } else if (data.type === 'metadata') {
            // 处理元数据
            Object.assign(strategy.metadata, data.content);
          } else if (data.type === 'complete') {
            // 生成完成
            console.log('策略生成完成:', strategy);
            eventSource.close();
            resolve(strategy);
          } else if (data.type === 'error') {
            // 处理错误
            console.error('策略生成错误:', data.message);
            eventSource.close();
            reject(new Error(data.message));
          }
        } catch (error) {
          console.error('解析SSE数据失败:', error);
          eventSource.close();
          reject(error);
        }
      };

      eventSource.onerror = (error) => {
        console.error('SSE连接错误:', error);
        eventSource.close();
        reject(new Error('流式生成策略失败，请检查网络连接'));
      };

      // 设置超时
      setTimeout(() => {
        if (eventSource.readyState !== EventSource.CLOSED) {
          console.warn('SSE连接超时');
          eventSource.close();
          reject(new Error('策略生成超时，AI模型需要较长时间处理，请稍后重试'));
        }
      }, 300000); // 5分钟超时
    });
  }

  // 生成策略（兼容旧接口）
  async generateStrategyLegacy(request: StrategyGenerationRequest): Promise<StrategyGenerationResult> {
    try {
      const response = await apiClient.post('/strategy/generate', request);
      return response.data;
    } catch (error) {
      console.error('生成策略失败:', error);
      throw error;
    }
  }

  async getStrategies(params: {
    offset?: number;
    limit?: number;
    keyword?: string;
    user_id?: string;
  } = {}): Promise<{
    strategies: StrategyRecord[];
    total: number;
    offset: number;
    limit: number;
  }> {
    const searchParams = new URLSearchParams();
    Object.entries(params).forEach(([key, value]) => {
      if (value !== undefined) {
        searchParams.append(key, value.toString());
      }
    });

    const endpoint = `/strategies${searchParams.toString() ? `?${searchParams.toString()}` : ''}`;

    try {
      const response = await apiClient.get(endpoint);
      const data = response.data?.data ?? response.data;
      return data;
    } catch (error) {
      console.error('获取策略历史失败:', error);
      return {
        strategies: [],
        total: 0,
        offset: 0,
        limit: 10
      };
    }
  }

  // 获取可用的AI模型提供商
  async getProviders(): Promise<ProviderInfo[]> {
    try {
      const response = await apiClient.get('/providers');
      return response.data?.data ?? response.data;
    } catch (error) {
      console.error('获取AI模型提供商失败:', error);
      if (axios.isAxiosError(error)) {
        const status = error.response?.status;
        if (status && status >= 500) {
          throw new Error('获取提供商信息失败，请稍后重试');
        }
      }
      throw new Error('获取AI模型提供商失败，请检查网络连接');
    }
  }

  // 健康检查
  async getHealth(): Promise<HealthStatus> {
    try {
      const response = await apiClient.get('/health');
      return response.data?.data ?? response.data;
    } catch (error) {
      console.error('健康检查失败:', error);
      if (axios.isAxiosError(error)) {
        const status = error.response?.status;
        if (status && status >= 500) {
          throw new Error('健康检查失败，服务异常');
        }
      }
      throw new Error('健康检查失败，请检查网络连接');
    }
  }

  // 获取性能指标
  async getPerformanceMetrics(): Promise<PerformanceMetrics[]> {
    try {
      const response = await apiClient.get('/performance');
      return response.data?.data ?? response.data;
    } catch (error) {
      console.error('获取性能指标失败:', error);
      if (axios.isAxiosError(error)) {
        const status = error.response?.status;
        if (status && status >= 500) {
          throw new Error('获取性能指标失败，服务异常');
        }
      }
      throw new Error('获取性能指标失败，请检查网络连接');
    }
  }

}

// 导出单例实例
export const aiStrategyService = AIStrategyService.getInstance();
