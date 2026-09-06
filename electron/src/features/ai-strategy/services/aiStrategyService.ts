/**
 * AI策略服务
 * 封装所有AI策略相关的API调用
 */

import { BaseService } from '../../../shared/services/baseService';
import { SERVICE_URLS } from '../../../config/services';
import {
  AIStrategyGenerationParams,
  GenerationState,
  StrategyAnalysis,
  StrategyTemplate,
  StrategyExecution,
  StrategyExportConfig
} from '../types/strategy.types';

const validateStockSymbols = (symbols: string[]) => ({
  valid: symbols,
  invalid: []
});

class AIStrategyService extends BaseService {
  constructor() {
    super('AIStrategyService', SERVICE_URLS.AI_STRATEGY);
  }

  /**
   * 枚举映射工具函数
   */
  private mapMarketType(marketType: string): string {
    const mapping: Record<string, string> = {
      'stock': 'CN',
      'futures': 'CN',
      'forex': 'GLOBAL',
      'crypto': 'GLOBAL'
    };
    return mapping[marketType] || 'CN';
  }

  private mapRiskPreference(riskPreference: string): string {
    const mapping: Record<string, string> = {
      'conservative': 'low',
      'moderate': 'medium',
      'aggressive': 'high'
    };
    return mapping[riskPreference] || 'medium';
  }

  private mapInvestmentStyle(investmentStyle: string): string {
    const mapping: Record<string, string> = {
      'value': 'conservative',
      'growth': 'aggressive',
      'balanced': 'balanced',
      'technical': 'custom'
    };
    return mapping[investmentStyle] || 'balanced';
  }

  private mapTimeframe(timeframe: string): string {
    const mapping: Record<string, string> = {
      'intraday': '1h',
      'daily': '1d',
      'weekly': '1w',
      'monthly': '1M'
    };
    return mapping[timeframe] || '1d';
  }

  private mapStrategyType(strategyType?: string): string {
    const mapping: Record<string, string> = {
      'trend_following': 'trend',
      'mean_reversion': 'mean_reversion',
      'arbitrage': 'arbitrage',
      'market_making': 'momentum'
    };
    return strategyType ? mapping[strategyType] || 'trend' : 'trend';
  }

  /**
   * 参数类型转换和验证
   */
  private validateAndConvertParams(params: AIStrategyGenerationParams): any {
    const converted: any = {
      description: params.description,
      market: this.mapMarketType(params.marketType),
      risk_level: this.mapRiskPreference(params.riskPreference),
      style: this.mapInvestmentStyle(params.investmentStyle),
      timeframe: this.mapTimeframe(params.timeframe),
      user_id: 'desktop-user'
    };

    // 数值类型参数转换和验证
    if (params.initialCapital && params.initialCapital > 0) {
      converted.initial_capital = parseFloat(params.initialCapital.toString());
    }

    if (params.maxPositions && params.maxPositions > 0) {
      converted.max_positions = parseInt(params.maxPositions.toString());
    }

    if (params.stopLoss && params.stopLoss > 0) {
      converted.stop_loss = parseFloat(params.stopLoss.toString());
    }

    if (params.takeProfit && params.takeProfit > 0) {
      converted.take_profit = parseFloat(params.takeProfit.toString());
    }

    // 高级参数处理
    if ((params as any).maxDrawdown && (params as any).maxDrawdown > 0 && (params as any).maxDrawdown <= 100) {
      converted.max_drawdown = parseFloat((params as any).maxDrawdown.toString());
    }

    if ((params as any).commissionRate && (params as any).commissionRate >= 0 && (params as any).commissionRate <= 1) {
      converted.commission_rate = parseFloat((params as any).commissionRate.toString());
    }

    if ((params as any).slippage && (params as any).slippage >= 0 && (params as any).slippage <= 1) {
      converted.slippage = parseFloat((params as any).slippage.toString());
    }

    // 股票池参数处理（新增）
    if (params.symbols && Array.isArray(params.symbols) && params.symbols.length > 0) {
      converted.symbols = params.symbols;
      this.logInfo('已添加股票池', { count: params.symbols.length, symbols: params.symbols });
    }

    // 策略类型和周期
    if (params.strategyType) {
      // 后端使用category字段
      converted.category = this.mapStrategyType(params.strategyType);
    }

    if (params.strategyLength) {
      converted.strategy_length = params.strategyLength;
    }

    if (params.backtestPeriod) {
      converted.backtest_period = params.backtestPeriod;
    }

    // 基准指数
    if (params.benchmark) {
      converted.benchmark = params.benchmark;
    }

    // 模板相关
    if (params.templateId) {
      converted.template_id = params.templateId;
    }

    if (params.useTemplate !== undefined) {
      converted.use_template = params.useTemplate;
    }

    // 数组参数处理
    if (params.examples && Array.isArray(params.examples)) {
      converted.examples = params.examples;
    }

    if (params.referenceStrategies && Array.isArray(params.referenceStrategies)) {
      // 后端不支持，暂时忽略
      this.logInfo('referenceStrategies parameter is not supported by backend', {
        strategies: params.referenceStrategies
      });
    }

    return converted;
  }

  /**
   * 生成AI策略
   */
  async generateStrategy(params: AIStrategyGenerationParams): Promise<GenerationState> {
    try {
      this.logInfo('开始生成AI策略', { params });

      // 验证股票代码格式
      if (params.symbols && params.symbols.length > 0) {
        const { valid, invalid } = validateStockSymbols(params.symbols);
        if (invalid.length > 0) {
          console.error('无效的股票代码', { invalid });
          return {
            status: 'error',
            progress: 0,
            message: `股票代码格式错误: ${invalid.join(', ')}`,
            error: `Invalid stock symbols: ${invalid.join(', ')}`
          };
        }
        params.symbols = valid;
      }

      // 使用参数验证和转换函数
      const convertedParams = this.validateAndConvertParams(params);

      convertedParams.output_format = 'python';
      convertedParams.include_imports = true;
      convertedParams.include_comments = true;

      this.logInfo('转换后的参数', { convertedParams });

      const response = await this.apiClient.post('/api/v1/strategy/generate', convertedParams);

      this.logInfo('AI策略生成成功', response.data);

      const responseData = response.data as any;
      const strategyCode = responseData?.strategy_code || responseData?.code || '';

      return {
        status: 'success',
        progress: 100,
        message: '策略生成成功',
        result: {
          ...(responseData || {}),
          strategy_code: strategyCode,
          framework: 'standard'
        } as any,
      };
    } catch (error) {
      this.handleServiceError(error, 'generateStrategy');
    }
  }

  /**
   * 获取策略生成进度
   */
  async getGenerationProgress(taskId: string): Promise<GenerationState> {
    void taskId;
    throw new Error('进度轮询接口已下线，请改用 /api/v1/strategy/generate/stream (SSE)');
  }

  /**
   * 验证股票代码
   */
  validateStockSymbols(symbols: string[]): { valid: string[]; invalid: string[] } {
    return validateStockSymbols(symbols);
  }

  /**
   * 保存策略
   */
  async saveStrategy(strategy: any): Promise<any> {
    try {
      this.logInfo('保存策略', { strategyId: strategy.id });

      const response = await this.apiClient.post('/api/v1/strategies', strategy);
      this.logInfo('策略保存成功', response.data);
      return response.data;
    } catch (error) {
      this.handleServiceError(error, 'saveStrategy');
    }
  }

  /**
   * 获取策略列表
   */
  async getStrategies(params?: {
    page?: number;
    pageSize?: number;
    status?: string;
    category?: string;
    search?: string;
  }): Promise<{ items: any[]; total: number }> {
    try {
      const response = await this.apiClient.get('/api/v1/strategies', params);
      return response.data as { items: any[]; total: number };
    } catch (error) {
      this.handleServiceError(error, 'getStrategies');
    }
  }

  /**
   * 获取策略详情
   */
  async getStrategy(id: string): Promise<any> {
    try {
      const response = await this.apiClient.get(`/api/v1/strategies/${id}`);
      return response.data;
    } catch (error) {
      this.handleServiceError(error, 'getStrategy');
    }
  }

  /**
   * 更新策略
   */
  async updateStrategy(id: string, updates: any): Promise<any> {
    try {
      const response = await this.apiClient.put(`/api/v1/strategies/${id}`, updates);
      return response.data;
    } catch (error) {
      this.handleServiceError(error, 'updateStrategy');
    }
  }

  /**
   * 删除策略
   */
  async deleteStrategy(id: string): Promise<void> {
    try {
      await this.apiClient.delete(`/api/v1/strategies/${id}`);
      this.logInfo('策略删除成功', { strategyId: id });
    } catch (error) {
      this.handleServiceError(error, 'deleteStrategy');
    }
  }

  /**
   * 复制策略
   */
  async duplicateStrategy(id: string, name?: string): Promise<any> {
    try {
      const response = await this.apiClient.post(`/api/v1/strategies/${id}/duplicate`, {
        name,
      });
      return response.data;
    } catch (error) {
      this.handleServiceError(error, 'duplicateStrategy');
    }
  }

  /**
   * 获取策略模板
   */
  async getStrategyTemplates(category?: string): Promise<StrategyTemplate[]> {
    // 统一管理：模板统一经 /api/v1/strategies/templates（与 strategyTemplateService 对齐）
    // 旧前缀 /api/v1/templates 已废弃
    try {
      const params = category ? { category } : {};
      const response = await this.apiClient.get('/api/v1/strategies/templates', params);
      const data: any = response.data;
      if (Array.isArray(data?.templates)) return data.templates as StrategyTemplate[];
      return data as StrategyTemplate[];
    } catch (error) {
      this.handleServiceError(error, 'getStrategyTemplates');
    }
  }

  /**
   * 分析策略
   */
  async analyzeStrategy(strategyId: string, analysisType: string): Promise<StrategyAnalysis> {
    try {
      const response = await this.apiClient.post(`/api/v1/strategies/${strategyId}/analyze`, {
        analysis_type: analysisType,
      });
      return response.data as StrategyAnalysis;
    } catch (error) {
      this.handleServiceError(error, 'analyzeStrategy');
    }
  }

  /**
   * 执行策略
   */
  async executeStrategy(strategyId: string, params?: any): Promise<StrategyExecution> {
    try {
      const response = await this.apiClient.post(`/api/v1/strategies/${strategyId}/execute`, params);
      return response.data as StrategyExecution;
    } catch (error) {
      this.handleServiceError(error, 'executeStrategy');
    }
  }

  /**
   * 获取执行状态
   */
  async getExecutionStatus(executionId: string): Promise<StrategyExecution> {
    try {
      const response = await this.apiClient.get(`/api/v1/strategies/executions/${executionId}`);
      return response.data as StrategyExecution;
    } catch (error) {
      this.handleServiceError(error, 'getExecutionStatus');
    }
  }

  /**
   * 停止策略执行
   */
  async stopExecution(executionId: string): Promise<void> {
    try {
      await this.apiClient.post(`/api/v1/strategies/executions/${executionId}/stop`);
      this.logInfo('策略执行已停止', { executionId });
    } catch (error) {
      this.handleServiceError(error, 'stopExecution');
    }
  }

  /**
   * 导出策略
   */
  async exportStrategy(strategyId: string, config: StrategyExportConfig): Promise<string> {
    try {
      const response = await this.apiClient.post(`/api/v1/strategies/${strategyId}/export`, {
        format: config.format || 'json'
      });
      return (response.data as any).downloadUrl || (response.data as any).data;
    } catch (error) {
      this.handleServiceError(error, 'exportStrategy');
    }
  }

  /**
   * 导入策略
   */
  async importStrategy(file: File): Promise<any> {
    try {
      const formData = new FormData();
      formData.append('file', file);

      const response = await this.apiClient.post('/api/v1/strategies/import', formData, {
        'Content-Type': 'multipart/form-data',
      });
      return response.data;
    } catch (error) {
      this.handleServiceError(error, 'importStrategy');
    }
  }

  /**
   * 分享策略
   */
  async shareStrategy(strategyId: string, visibility: string, description: string): Promise<any> {
    try {
      const response = await this.apiClient.post(`/api/v1/strategies/${strategyId}/share`, {
        visibility,
        description,
      });
      return response.data;
    } catch (error) {
      this.handleServiceError(error, 'shareStrategy');
    }
  }

  /**
   * 获取策略统计信息
   */
  async getStrategyStats(): Promise<{
    total: number;
    active: number;
    draft: number;
    archived: number;
  }> {
    try {
      const response = await this.apiClient.get('/api/v1/strategies/stats');
      return response.data as {
        total: number;
        active: number;
        draft: number;
        archived: number;
      };
    } catch (error) {
      this.handleServiceError(error, 'getStrategyStats');
    }
  }
}

// 导出单例实例
export const aiStrategyService = new AIStrategyService();
export default aiStrategyService;
