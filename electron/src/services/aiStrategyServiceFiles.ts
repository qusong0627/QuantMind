// AI 策略服务文件与模板相关能力
import {
  Strategy,
  StrategyTemplate,
  TemplateMatch,
  ValidationResult,
  ParameterValidationResult,
  ProviderPerformance,
  SystemPerformance,
  PerformanceAlert,
  FileInfo
} from '../types/strategy';
import { SERVICE_ENDPOINTS } from '../config/services';
import { apiClient } from './aiStrategyClients';
import { AIStrategyServiceHelpersMixin } from './aiStrategyServiceHelpers';

export class AIStrategyServiceFilesMixin extends AIStrategyServiceHelpersMixin {
  async uploadFile(file: File, options: {
    userId?: string;
    category?: 'auto' | 'image' | 'document' | 'archive';
    description?: string;
    tags?: string[];
  } = {}): Promise<{
    id: string;
    name: string;
    originalName: string;
    size: number;
    type: string;
    url: string;
    category: string;
    uploadTime: string;
  }> {
    const formData = new FormData();
    formData.append('file', file);
    formData.append('user_id', options.userId || 'desktop-user');
    formData.append('category', options.category || 'auto');
    formData.append('description', options.description || '');
    if (options.tags && options.tags.length > 0) {
      formData.append('tags', options.tags.join(','));
    }

    try {
      const token = authService.getAccessToken();
      const response = await fetch(`${SERVICE_ENDPOINTS.API_GATEWAY}/files/upload`, {
        method: 'POST',
        headers: {
          ...(token ? { 'Authorization': `Bearer ${token}` } : {}),
        },
        body: formData,
      });

      if (!response.ok) {
        const errorData = await response.json();
        throw new Error(errorData.message || '上传失败');
      }

      const result = await response.json();

      if (result.code === 0) {
        return {
          id: result.data.file_id,
          name: result.data.file_name,
          originalName: result.data.original_name,
          size: result.data.file_size,
          type: result.data.content_type,
          url: result.data.file_url,
          category: result.data.file_category,
          uploadTime: result.data.upload_time
        };
      } else {
        throw new Error(result.message || '上传失败');
      }
    } catch (error) {
      console.error('文件上传失败:', error);
      throw error;
    }
  }

  // 上传策略文件
  async uploadStrategyFile(file: File, strategyId?: string): Promise<{
    id: string;
    name: string;
    originalName: string;
    size: number;
    type: string;
    url: string;
    category: string;
    uploadTime: string;
  }> {
    return this.uploadFile(file, {
      category: 'document',
      description: strategyId ? `策略文件 - 策略ID: ${strategyId}` : '策略文件',
      tags: strategyId ? ['strategy', `strategy_${strategyId}`] : ['strategy']
    });
  }

  // 获取用户文件列表
  async getUserFiles(params: {
    userId?: string;
    category?: string;
    limit?: number;
    offset?: number;
  } = {}): Promise<{
    files: Array<{
      id: string;
      name: string;
      originalName: string;
      size: number;
      type: string;
      url: string;
      category: string;
      uploadTime: string;
    }>;
    total: number;
    limit: number;
    offset: number;
    category?: string;
  }> {
    const searchParams = new URLSearchParams();
    searchParams.append('user_id', params.userId || 'desktop-user');
    if (params.category) {
      searchParams.append('category', params.category);
    }
    if (params.limit) {
      searchParams.append('limit', params.limit.toString());
    }
    if (params.offset) {
      searchParams.append('offset', params.offset.toString());
    }

    try {
      const token = authService.getAccessToken();
      const response = await fetch(`${SERVICE_ENDPOINTS.API_GATEWAY}/files/list?${searchParams.toString()}`, {
        headers: {
          ...(token ? { 'Authorization': `Bearer ${token}` } : {}),
        }
      });

      if (!response.ok) {
        throw new Error('获取文件列表失败');
      }

      const result = await response.json();

      if (result.code === 0) {
        return {
          files: result.data.files.map((file: unknown) => {
            const f = file as Record<string, unknown>;
            return {
              id: f.file_key as string,
              name: f.file_name as string,
              originalName: f.original_name as string,
              size: f.file_size as number,
              type: f.content_type as string,
              url: f.file_url as string,
              category: f.file_category as string,
              uploadTime: f.upload_time as string
            };
          }), total: result.data.total,
          limit: result.data.limit,
          offset: result.data.offset,
          category: result.data.category
        };
      } else {
        throw new Error(result.message || '获取文件列表失败');
      }
    } catch (error) {
      console.error('获取文件列表失败:', error);
      throw error;
    }
  }

  // 删除文件
  async deleteFile(fileKey: string, userId?: string): Promise<void> {
    try {
      const token = authService.getAccessToken();
      const response = await fetch(`${SERVICE_ENDPOINTS.API_GATEWAY}/files/delete`, {
        method: 'DELETE',
        headers: {
          'Content-Type': 'application/json',
          ...(token ? { 'Authorization': `Bearer ${token}` } : {}),
        },
        body: JSON.stringify({
          file_key: fileKey,
          user_id: userId || 'desktop-user'
        }),
      });

      if (!response.ok) {
        const errorData = await response.json();
        throw new Error(errorData.message || '删除失败');
      }

      const result = await response.json();
      if (result.code !== 0) {
        throw new Error(result.message || '删除失败');
      }
    } catch (error) {
      console.error('删除文件失败:', error);
      throw error;
    }
  }

  // 获取策略代码（从COS）
  async getStrategyCode(strategyId: string): Promise<{
    strategy_id: string;
    name: string;
    provider: string;
    code: string;
    cos_file_url?: string;
    cos_updated?: boolean;
    cos_error?: string;
  }> {
    try {
      const response = await apiClient.get(`/strategies/${strategyId}/code`);

      if (response.data.code === 0) {
        return response.data.data;
      } else {
        throw new Error(response.data.message || '获取策略代码失败');
      }
    } catch (error) {
      console.error('获取策略代码失败:', error);
      throw error;
    }
  }

  // 下载策略文件
  async downloadStrategyFile(strategyId: string): Promise<string> {
    try {
      const response = await apiClient.get(`/strategies/${strategyId}/download`, {
        maxRedirects: 5,
        validateStatus: (status) => status >= 200 && status < 400
      });

      // 如果是重定向响应，返回重定向URL
      if (response.status >= 300 && response.status < 400) {
        return response.headers.location || response.request.responseURL;
      }

      throw new Error('获取下载链接失败');
    } catch (error) {
      console.error('下载策略文件失败:', error);
      throw error;
    }
  }

  // ==================== 模板相关API ====================

  // 获取策略模板列表
  async getTemplates(params: {
    category?: string;
    risk_level?: string;
    market?: string;
    complexity?: string;
    page?: number;
    page_size?: number;
    query?: string;
  } = {}): Promise<{
    templates: StrategyTemplate[];
    total: number;
    page: number;
    page_size: number;
    total_pages: number;
  }> {
    try {
      const response = await apiClient.get('/templates', { params });
      return response.data.data;
    } catch (error) {
      console.error('获取模板列表失败:', error);
      throw error;
    }
  }

  // 获取策略模板详情
  async getTemplate(templateId: string): Promise<StrategyTemplate> {
    try {
      const response = await apiClient.get(`/templates/${templateId}`);
      return response.data.data;
    } catch (error) {
      console.error('获取模板详情失败:', error);
      throw error;
    }
  }

  // 智能匹配策略模板
  async matchTemplates(params: {
    description: string;
    market?: string;
    risk_level?: string;
    style?: string;
    symbols?: string[];
    timeframe?: string;
    initial_capital?: number;
    max_matches?: number;
  }): Promise<{
    matches: TemplateMatch[];
    total_matches: number;
    processing_time: number;
    suggestions: string[];
  }> {
    try {
      const response = await apiClient.post('/templates/match', params);
      return response.data.data;
    } catch (error) {
      console.error('模板匹配失败:', error);
      throw error;
    }
  }

  // 获取模板类别
  async getTemplateCategories(): Promise<{
    categories: Array<{
      value: string;
      label: string;
      description: string;
    }>;
  }> {
    try {
      const response = await apiClient.get('/templates/categories');
      return response.data.data;
    } catch (error) {
      console.error('获取模板类别失败:', error);
      throw error;
    }
  }

  // 获取模板统计
  async getTemplateStats(): Promise<{
    total_templates: number;
    category_distribution: Record<string, number>;
    complexity_distribution: Record<string, number>;
    avg_min_capital: number;
    avg_max_symbols: number;
  }> {
    try {
      const response = await apiClient.get('/templates/stats');
      return response.data.data;
    } catch (error) {
      console.error('获取模板统计失败:', error);
      throw error;
    }
  }

  // ==================== 验证相关API ====================

  // 验证策略参数
  async validateParameters(params: {
    parameters: Record<string, unknown>;
    strict_mode?: boolean;
  }): Promise<ParameterValidationResult> {
    try {
      const response = await apiClient.post('/validate/parameters', params);
      return response.data.data;
    } catch (error) {
      console.error('参数验证失败:', error);
      throw error;
    }
  }

  // 验证策略代码
  async validateCode(params: {
    code: string;
    language?: string;
    strict_mode?: boolean;
    market?: string;
    check_style?: boolean;
  }): Promise<ValidationResult> {
    try {
      const response = await apiClient.post('/validate/code', params);
      return response.data.data;
    } catch (error) {
      console.error('代码验证失败:', error);
      throw error;
    }
  }

  // 验证模板兼容性
  async validateTemplate(params: {
    template_id: string;
    parameters: Record<string, unknown>;
    strict_mode?: boolean;
  }): Promise<ValidationResult> {
    try {
      const response = await apiClient.post('/validate/template', params);
      return response.data.data;
    } catch (error) {
      console.error('模板验证失败:', error);
      throw error;
    }
  }

  // 批量验证
  async validateBatch(params: {
    parameters?: Record<string, unknown>;
    code?: string;
    template_id?: string;
    validation_types: string[];
    strict_mode?: boolean;
  }): Promise<ValidationResult> {
    try {
      const response = await apiClient.post('/validate/batch', params);
      return response.data.data;
    } catch (error) {
      console.error('批量验证失败:', error);
      throw error;
    }
  }

  // 实时验证
  async validateRealtime(params: {
    parameters?: Record<string, unknown>;
    code?: string;
    template_id?: string;
    strict_mode?: boolean;
  }): Promise<{
    success: boolean;
    overall_score: number;
    is_ready_for_generation: boolean;
    next_steps: string[];
    processing_time: number;
    summary: Record<string, unknown>;
  }> {
    try {
      const response = await apiClient.post('/validate/realtime', params);
      return response.data.data;
    } catch (error) {
      console.error('实时验证失败:', error);
      throw error;
    }
  }

  // 获取验证规则
  async getValidationRules(): Promise<{
    rules: Array<{
      field: string;
      rule: string;
      message: string;
      severity: string;
    }>;
    total_rules: number;
    categories: Record<string, number>;
  }> {
    try {
      const response = await apiClient.get('/validate/rules');
      return response.data.data;
    } catch (error) {
      console.error('获取验证规则失败:', error);
      throw error;
    }
  }

  // 获取验证质量指标
  async getValidationMetrics(): Promise<{
    code_quality_metrics: Record<string, unknown>;
    thresholds: Record<string, number>;
    recommendations: Record<string, string>;
  }> {
    try {
      const response = await apiClient.get('/validate/metrics');
      return response.data.data;
    } catch (error) {
      console.error('获取验证指标失败:', error);
      throw error;
    }
  }

  // ==================== 性能监控相关API ====================

  // 获取系统性能统计
  async getSystemPerformance(): Promise<SystemPerformance> {
    try {
      const response = await apiClient.get('/performance/system');
      return response.data.data;
    } catch (error) {
      console.error('获取系统性能失败:', error);
      throw error;
    }
  }

  // 获取Provider性能统计
  async getProviderPerformance(providerName: string): Promise<ProviderPerformance> {
    try {
      const response = await apiClient.get(`/performance/providers/${providerName}`);
      return response.data.data;
    } catch (error) {
      console.error('获取Provider性能失败:', error);
      throw error;
    }
  }

  // 获取所有Provider性能统计
  async getAllProvidersPerformance(): Promise<{
    providers: ProviderPerformance[];
    total_providers: number;
  }> {
    try {
      const response = await apiClient.get('/performance/providers');
      return response.data.data;
    } catch (error) {
      console.error('获取Provider性能列表失败:', error);
      throw error;
    }
  }

  // 获取性能历史数据
  async getPerformanceHistory(params: {
    provider_name?: string;
    time_range?: string;
    metric_types?: string[];
    start_time?: string;
    end_time?: string;
    limit?: number;
  }): Promise<{
    provider_name: string;
    time_range: string;
    data_points: unknown[];
    summary: Record<string, unknown>;
  }> {
    try {
      const response = await apiClient.post('/performance/history', params);
      return response.data.data;
    } catch (error) {
      console.error('获取性能历史失败:', error);
      throw error;
    }
  }

  // 获取活跃告警
  async getActiveAlerts(): Promise<{
    alerts: PerformanceAlert[];
    total_alerts: number;
  }> {
    try {
      const response = await apiClient.get('/performance/alerts');
      return response.data.data;
    } catch (error) {
      console.error('获取告警列表失败:', error);
      throw error;
    }
  }

  // 解决告警
  async resolveAlert(alertId: string): Promise<{ message: string }> {
    try {
      const response = await apiClient.post(`/performance/alerts/${alertId}/resolve`);
      return response.data.data;
    } catch (error) {
      console.error('解决告警失败:', error);
      throw error;
    }
  }

  // 重置性能统计
  async resetPerformanceStats(providerName?: string): Promise<{ message: string }> {
    try {
      const response = await apiClient.post('/performance/reset', {
        provider_name: providerName
      });
      return response.data.data;
    } catch (error) {
      console.error('重置性能统计失败:', error);
      throw error;
    }
  }

  // ==================== 文件管理相关API ====================

  // 上传文件到AI策略服务
  async uploadFileToAI(file: File, options: {
    userId?: string;
    category?: string;
    description?: string;
    tags?: string[];
  } = {}): Promise<FileInfo> {
    const formData = new FormData();
    formData.append('file_content', file);
    formData.append('filename', file.name);
    formData.append('user_id', options.userId || 'desktop-user');
    formData.append('category', options.category || 'auto');
    formData.append('description', options.description || '');
    if (options.tags && options.tags.length > 0) {
      formData.append('tags', JSON.stringify(options.tags));
    }

    try {
      const response = await apiClient.post('/files/upload', formData, {
        headers: {
          'Content-Type': 'multipart/form-data',
        },
      });

      return response.data.data;
    } catch (error) {
      console.error('文件上传失败:', error);
      throw error;
    }
  }

  // 获取文件信息
  async getFileInfo(fileId: string): Promise<FileInfo> {
    try {
      const response = await apiClient.get(`/files/${fileId}`);
      return response.data.data;
    } catch (error) {
      console.error('获取文件信息失败:', error);
      throw error;
    }
  }

  // 获取文件内容
  async getFileContent(fileId: string): Promise<Blob> {
    try {
      const response = await apiClient.get(`/files/${fileId}/content`, {
        responseType: 'blob'
      });
      return response.data;
    } catch (error) {
      console.error('获取文件内容失败:', error);
      throw error;
    }
  }

  // 下载文件
  async downloadFileFromAI(fileId: string): Promise<{ file_path: string }> {
    try {
      const response = await apiClient.get(`/files/${fileId}/download`);
      return response.data.data;
    } catch (error) {
      console.error('下载文件失败:', error);
      throw error;
    }
  }

  // 删除文件
  async deleteFileFromAI(fileId: string, userId?: string): Promise<{ message: string }> {
    try {
      const response = await apiClient.delete(`/files/${fileId}`, {
        params: { user_id: userId || 'desktop-user' }
      });
      return response.data.data;
    } catch (error) {
      console.error('删除文件失败:', error);
      throw error;
    }
  }

  // 列出用户文件
  async listUserFilesFromAI(params: {
    userId?: string;
    category?: string;
    limit?: number;
    offset?: number;
  } = {}): Promise<{
    files: FileInfo[];
    total: number;
    limit: number;
    offset: number;
  }> {
    try {
      const response = await apiClient.get('/files', {
        params: {
          user_id: params.userId || 'desktop-user',
          category: params.category,
          limit: params.limit,
          offset: params.offset
        }
      });
      return response.data.data;
    } catch (error) {
      console.error('获取文件列表失败:', error);
      throw error;
    }
  }

  // 按类别列出文件
  async listFilesByCategory(category: string, params: {
    limit?: number;
    offset?: number;
  } = {}): Promise<{
    files: FileInfo[];
    total: number;
    limit: number;
    offset: number;
    category: string;
  }> {
    try {
      const response = await apiClient.get(`/files/category/${category}`, {
        params
      });
      return response.data.data;
    } catch (error) {
      console.error('获取类别文件失败:', error);
      throw error;
    }
  }

  // 搜索文件
  async searchFilesFromAI(params: {
    query: string;
    userId?: string;
    category?: string;
    limit?: number;
  }): Promise<{
    files: FileInfo[];
    total: number;
    query: string;
  }> {
    try {
      const response = await apiClient.get('/files/search', {
        params: {
          query: params.query,
          user_id: params.userId || 'desktop-user',
          category: params.category,
          limit: params.limit
        }
      });
      return response.data.data;
    } catch (error) {
      console.error('搜索文件失败:', error);
      throw error;
    }
  }

  // 更新文件元数据
  async updateFileMetadata(fileId: string, params: {
    userId?: string;
    description?: string;
    tags?: string[];
  }): Promise<{ message: string }> {
    try {
      const response = await apiClient.put(`/files/${fileId}/metadata`, {
        user_id: params.userId || 'desktop-user',
        description: params.description,
        tags: params.tags
      });
      return response.data.data;
    } catch (error) {
      console.error('更新文件元数据失败:', error);
      throw error;
    }
  }

  // 清理临时文件
  async cleanupTempFiles(maxAgeDays: number = 7): Promise<{
    message: string;
    deleted_count: number;
  }> {
    try {
      const response = await apiClient.post('/files/cleanup', {
        max_age_days: maxAgeDays
      });
      return response.data.data;
    } catch (error) {
      console.error('清理临时文件失败:', error);
      throw error;
    }
  }

  // 获取文件存储统计
  async getFileStats(): Promise<{
    total_files: number;
    total_size: number;
    base_directory: string;
    categories: Record<string, unknown>;
    users: Record<string, unknown>;
    storage_limit?: unknown;
    last_cleanup?: unknown;
  }> {
    try {
      const response = await apiClient.get('/files/stats');
      return response.data.data;
    } catch (error) {
      console.error('获取文件统计失败:', error);
      throw error;
    }
  }
}
