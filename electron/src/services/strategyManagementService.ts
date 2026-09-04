/**
 * 策略管理服务
 * 负责策略文件的上传、保存、加载和验证
 */

import axios, { AxiosInstance } from 'axios';
import { SERVICE_URLS } from '../config/services';
import { authService } from '../features/auth/services/authService';
import {
  StrategyFile,
  StrategyValidationResult,
  StrategyConversionRequest,
  StrategyConversionResponse,
} from '../types/backtest/strategy';

class StrategyManagementService {
  private client: AxiosInstance;

  constructor() {
    this.client = axios.create({
      timeout: 30000,
      headers: { 'Content-Type': 'application/json' },
    });
    this.client.interceptors.request.use((config) => {
      config.baseURL = String(SERVICE_URLS.API_GATEWAY || '').replace(/\/+$/, '');
      const token = authService.getAccessToken();
      if (token) {
        config.headers.Authorization = `Bearer ${token}`;
      }
      const tenantId = authService.getTenantId?.() || localStorage.getItem('tenant_id') || 'default';
      config.headers['X-Tenant-Id'] = tenantId;
      return config;
    });
    this.client.interceptors.response.use(
      (response) => response,
      async (error) => authService.handle401Error(error, this.client)
    );
  }

  private unwrapResponse<T>(payload: any): T {
    if (payload && typeof payload === 'object') {
      if ('data' in payload) return payload.data as T;
    }
    return payload as T;
  }

  private toStrategyFile(item: any): StrategyFile {
    const config = item?.config && typeof item.config === 'object' ? item.config : {};
    const code = String(item?.code || config.code || '');
    const name = String(item?.name || item?.strategy_name || '未命名策略');
    const isQlibFormat = this.checkQlibFormat(code);
    return {
      id: String(item?.id ?? ''),
      name,
      source: item?.is_system ? 'template' : 'personal',
      code,
      description: item?.description || '',
      created_at: item?.created_at,
      updated_at: item?.updated_at,
      is_qlib_format: isQlibFormat,
      language: isQlibFormat ? 'qlib' : 'python',
      tags: Array.isArray(item?.tags) ? item.tags : [],
      cos_url: item?.cos_url || undefined,
      is_verified: !!item?.is_verified,
      is_system: !!item?.is_system,
      parameters: item?.parameters && typeof item.parameters === 'object' ? item.parameters : {},
      execution_config: item?.execution_config || config.execution_config,
      live_trade_config: item?.live_trade_config || config.live_trade_config,
      execution_defaults: item?.execution_defaults,
      live_defaults: item?.live_defaults,
      live_config_tips: Array.isArray(item?.live_config_tips) ? item.live_config_tips : [],
    };
  }

  /**
   * 验证策略代码
   */
  async validateStrategy(code: string): Promise<StrategyValidationResult> {
    try {
      // TODO: 实现后端API调用
      // const response = await this.client.post('/api/v1/strategies/validate', { code });
      // return response.data;

      // 临时前端验证逻辑
      const errors: any[] = [];
      const warnings: any[] = [];

      // 检查是否为Qlib格式
      const isQlibFormat = this.checkQlibFormat(code);

      if (!isQlibFormat) {
        errors.push({
          type: 'compatibility',
          message: '检测到非Qlib格式策略代码，请先执行策略转换',
          severity: 'error',
        });
      }

      // 基础语法检查
      if (!code || code.trim().length === 0) {
        errors.push({
          type: 'syntax',
          message: '策略代码不能为空',
          severity: 'error',
        });
      }

      // 检查Python语法错误（简单检查）
      if (code.includes('def ') && !code.includes(':')) {
        errors.push({
          type: 'syntax',
          line: 1,
          message: 'Python函数定义缺少冒号',
          severity: 'error',
        });
      }

      return {
        is_valid: errors.length === 0,
        is_qlib_format: isQlibFormat,
        errors,
        warnings,
        suggestions: isQlibFormat ? [] : ['使用"策略转换"功能将Python代码转换为Qlib格式'],
      };
    } catch (error: any) {
      console.error('[StrategyManagementService] Validation error:', error);
      return {
        is_valid: false,
        is_qlib_format: false,
        errors: [{
          type: 'syntax',
          message: error.message || '验证失败',
          severity: 'error',
        }],
        warnings: [],
      };
    }
  }

  /**
   * 检查是否为Qlib格式
   */
  private checkQlibFormat(code: string): boolean {
    // 检查Qlib特征
    const qlibPatterns = [
      /from\s+qlib/i,
      /import\s+qlib/i,
      /qlib\.init/i,
      /TopkDropoutStrategy/i,
      /WeightStrategy/i,
      /qlib\.contrib/i,
    ];

    // 兼容 Native 模板（STRATEGY_CONFIG + Redis*Strategy）
    const nativeQlibPatterns = [
      /STRATEGY_CONFIG\s*=\s*\{/i,
      /["']class["']\s*:\s*["']Redis[A-Za-z0-9_]*Strategy["']/i,
      /Redis[A-Za-z0-9_]*Strategy/i,
      /["']signal["']\s*:\s*["']<PRED>["']/i,
    ];

    return [...qlibPatterns, ...nativeQlibPatterns].some(pattern => pattern.test(code));
  }

  /**
   * 转换策略为Qlib格式
   */
  async convertToQlib(_request: StrategyConversionRequest): Promise<StrategyConversionResponse> {
    try {
      // TODO: 实现后端API调用
      // const response = await this.client.post('/api/v1/strategies/convert', request);
      // return response.data;

      // 临时返回提示信息
      return {
        success: false,
        errors: ['策略转换功能正在开发中，请直接使用Qlib格式策略'],
        warnings: [
          '目前支持的Qlib策略类型：',
          '1. TopkDropoutStrategy - 选股策略',
          '2. WeightStrategy - 权重分配策略',
        ],
      };
    } catch (error: any) {
      console.error('[StrategyManagementService] Conversion error:', error);
      return {
        success: false,
        errors: [error.message || '转换失败'],
      };
    }
  }

  /**
   * 从代码中提取配置 (用于模拟盘验证)
   */
  async extractConfig(code: string): Promise<any> {
    try {
      const response = await this.client.post('/api/v1/strategy/extract-config', { code });
      return this.unwrapResponse<any>(response.data);
    } catch (error: any) {
      console.error('[StrategyManagementService] Extract error:', error);
      throw new Error(error.response?.data?.detail || error.message || '提取配置失败，请检查代码规范');
    }
  }

  /**
   * 保存策略到个人中心
   */
  async saveStrategy(strategy: Omit<StrategyFile, 'id' | 'created_at' | 'updated_at'> & { parameters?: any }): Promise<StrategyFile> {
    try {
      // payload 格式与后端 SaveStrategyRequest 对齐
      const payload = {
        name: strategy.name,
        description: strategy.description || '',
        code: strategy.code,
        category: 'manual_created',
        author: '用户',
        tags: strategy.tags || [],
        parameters: strategy.parameters || {},
      };
      const response = await this.client.post('/api/v1/strategies', payload);
      // 后端返回 { strategy_id, message, cos_url, ... }，构造 StrategyFile 返回
      const created = this.unwrapResponse<any>(response.data);
      return {
        id: String(created?.strategy_id ?? ''),
        name: strategy.name,
        source: 'personal',
        code: strategy.code,
        description: strategy.description || '',
        is_qlib_format: this.checkQlibFormat(strategy.code),
        language: this.checkQlibFormat(strategy.code) ? 'qlib' : 'python',
        tags: strategy.tags || [],
        cos_url: created?.cos_url || undefined,  // 透传 COS URL
      };
    } catch (error: any) {
      console.error('[StrategyManagementService] Save error:', error);
      throw new Error(error.response?.data?.detail || error.message || '保存策略失败');
    }
  }

  /**
   * 从个人中心加载策略列表
   */
  async loadStrategies(_userId?: string, market?: string): Promise<StrategyFile[]> {
    try {
      const response = await this.client.get('/api/v1/strategies', {
        // CN/A 也要传：后端会把港股模板(market=HK)排除在 A 股视图外，缺省则全量返回
        params: market ? { market } : undefined,
      });
      const data = this.unwrapResponse<any>(response.data);
      // 后端 StrategyListResponse 格式: { total: number, strategies: [...] }
      const items = Array.isArray(data?.strategies)
        ? data.strategies
        : Array.isArray(data?.items)
          ? data.items
          : Array.isArray(data)
            ? data
            : [];
      return items.map((item: any) => this.toStrategyFile(item));
    } catch (error: any) {
      console.error('[StrategyManagementService] Load error:', error);
      throw new Error(error.message || '加载策略失败');
    }
  }

  /**
   * 获取单个策略详情
   */
  async getStrategy(strategyId: string): Promise<StrategyFile> {
    try {
      const response = await this.client.get(`/api/v1/strategies/${strategyId}`, {
        params: { resolve_code: true }
      });
      const data = this.unwrapResponse<any>(response.data);
      if (!data) throw new Error('策略详情不存在');
      return this.toStrategyFile(data);
    } catch (error: any) {
      console.error('[StrategyManagementService] Get detail error:', error);
      throw new Error(error.message || '获取策略详情失败');
    }
  }

  /**
   * 更新现有策略
   */
  async updateStrategy(strategyId: string, updates: Partial<StrategyFile>): Promise<StrategyFile> {
    try {
      // 构造符合后端 UpdateStrategyRequest 的 payload
      // 注意：code 放顶层，不再嵌套在 config.code 中
      const payload: any = {};
      if (updates.name !== undefined) payload.name = updates.name;
      if (updates.description !== undefined) payload.description = updates.description;
      if (updates.tags !== undefined) payload.tags = updates.tags;
      // code 直接放顶层（对应后端 UpdateStrategyRequest.code 字段）
      if (updates.code !== undefined) {
        payload.code = updates.code;
      }

      const response = await this.client.put(`/api/v1/strategies/${strategyId}`, payload);
      const updated = this.unwrapResponse<any>(response.data);
      return this.toStrategyFile(updated);
    } catch (error: any) {
      console.error('[StrategyManagementService] Update error:', error);
      throw new Error(error.response?.data?.detail || error.message || '更新策略失败');
    }
  }

  /**
   * 删除策略
   */
  async deleteStrategy(strategyId: string): Promise<void> {
    try {
      await this.client.delete(`/api/v1/strategies/${strategyId}`);
    } catch (error: any) {
      console.error('[StrategyManagementService] Delete error:', error);
      throw new Error(error.message || '删除策略失败');
    }
  }

  /**
   * 同步系统策略模板到个人中心
   */
  async syncTemplates(): Promise<{ success: boolean; synced_count: number; message: string }> {
    try {
      const response = await this.client.post('/api/v1/strategies/sync');
      return this.unwrapResponse<any>(response.data);
    } catch (error: any) {
      console.error('[StrategyManagementService] Sync templates error:', error);
      throw new Error(error.response?.data?.detail || error.message || '同步模板失败');
    }
  }

  /**
   * 激活策略 (同步配置至 Redis 匹配池)
   */
  async activateStrategy(strategyId: string): Promise<any> {
    try {
      const response = await this.client.post(`/api/v1/strategies/${strategyId}/activate`);
      return this.unwrapResponse<any>(response.data);
    } catch (error: any) {
      console.error('[StrategyManagementService] Activate error:', error);
      throw new Error(error.response?.data?.detail || error.message || '激活策略失败');
    }
  }

  /**
   * 停用策略 (从 Redis 匹配池移除)
   */
  async deactivateStrategy(strategyId: string): Promise<any> {
    try {
      const response = await this.client.delete(`/api/v1/strategies/${strategyId}/activate`);
      return this.unwrapResponse<any>(response.data);
    } catch (error: any) {
      console.error('[StrategyManagementService] Deactivate error:', error);
      throw new Error(error.response?.data?.detail || error.message || '停用策略失败');
    }
  }

  /**
   * 读取本地文件内容
   */
  async readLocalFile(file: File): Promise<string> {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = (e) => {
        const content = e.target?.result as string;
        resolve(content);
      };
      reader.onerror = () => reject(new Error('文件读取失败'));
      reader.readAsText(file, 'UTF-8');
    });
  }
}

export const strategyManagementService = new StrategyManagementService();
