import axios, { AxiosInstance } from 'axios';
import { SERVICE_URLS } from '../../config/services';

export interface QlibMarketData {
  symbol: string;
  data: Array<{
    date: string;
    open: number;
    high: number;
    low: number;
    close: number;
    volume: number;
  }>;
  start_date: string;
  end_date: string;
  data_source: string;
}

export interface FactorExpression {
  expression: string;
  name?: string;
}

export interface FactorData {
  name: string;
  expression: string;
  data: Array<{
    date: string;
    value: number | null;
  }>;
}

export interface FactorCalculationResult {
  symbol: string;
  factors: FactorData[];
}

/**
 * 市场数据缓存管理
 */
class DataCache {
  private cache = new Map<string, { data: any; timestamp: number }>();
  private maxAge = 5 * 60 * 1000; // 5分钟缓存

  set(key: string, data: any): void {
    this.cache.set(key, {
      data,
      timestamp: Date.now(),
    });
  }

  get(key: string): any | null {
    const cached = this.cache.get(key);
    if (!cached) return null;

    const age = Date.now() - cached.timestamp;
    if (age > this.maxAge) {
      this.cache.delete(key);
      return null;
    }

    return cached.data;
  }

  clear(): void {
    this.cache.clear();
  }

  remove(key: string): void {
    this.cache.delete(key);
  }
}

class QlibDataService {
  private client: AxiosInstance;
  private baseUrl: string;
  private cache: DataCache;

  constructor() {
    this.baseUrl = SERVICE_URLS.ENGINE_SERVICE;
    this.client = axios.create({
      baseURL: this.baseUrl,
      timeout: 30000,
      headers: { 'Content-Type': 'application/json' },
    });
    this.cache = new DataCache();

    // 请求拦截器
    this.client.interceptors.request.use(
      (config) => {
        console.log(`[QlibData] ${config.method?.toUpperCase()} ${config.url}`);
        return config;
      },
      (error) => Promise.reject(error)
    );

    // 响应拦截器
    this.client.interceptors.response.use(
      (response) => response,
      (error) => {
        console.error('[QlibData] Request failed:', error);
        if (error.response) {
          throw new Error(
            error.response.data?.detail ||
            error.response.data?.message ||
            `请求失败: ${error.response.status}`
          );
        } else if (error.request) {
          throw new Error('网络连接失败，请检查服务是否启动');
        } else {
          throw new Error(error.message || '请求配置错误');
        }
      }
    );
  }

  /**
   * 获取市场数据
   * @param symbol 股票代码
   * @param startDate 开始日期 (可选)
   * @param endDate 结束日期 (可选)
   * @param useCache 是否使用缓存 (默认true)
   * @returns 市场数据
   */
  async getMarketData(
    symbol: string,
    startDate?: string,
    endDate?: string,
    useCache = true
  ): Promise<QlibMarketData> {
    if (!symbol?.trim()) {
      throw new Error('股票代码不能为空');
    }

    // 生成缓存键
    const cacheKey = `market_${symbol}_${startDate}_${endDate}`;

    // 检查缓存
    if (useCache) {
      const cached = this.cache.get(cacheKey);
      if (cached) {
        console.log('[QlibData] Cache hit:', cacheKey);
        return cached;
      }
    }

    // 构建查询参数
    const params = new URLSearchParams();
    if (startDate) params.append('start_date', startDate);
    if (endDate) params.append('end_date', endDate);

    const url = `/market-data/${symbol}${params.toString() ? '?' + params.toString() : ''}`;
    const response = await this.client.get<QlibMarketData>(url);

    // 缓存结果
    if (useCache) {
      this.cache.set(cacheKey, response.data);
    }

    return response.data;
  }

  /**
   * 计算因子
   * @param symbol 股票代码
   * @param startDate 开始日期
   * @param endDate 结束日期
   * @param expressions 因子表达式列表
   * @returns 因子计算结果
   */
  async calculateFactors(
    symbol: string,
    startDate: string,
    endDate: string,
    expressions: FactorExpression[]
  ): Promise<FactorCalculationResult> {
    if (!symbol?.trim()) {
      throw new Error('股票代码不能为空');
    }
    if (!expressions || expressions.length === 0) {
      throw new Error('因子表达式不能为空');
    }

    const response = await this.client.post<FactorCalculationResult>(
      '/factors/calculate',
      {
        symbol,
        start_date: startDate,
        end_date: endDate,
        expressions,
      }
    );

    return response.data;
  }

  /**
   * 获取股票列表
   * @returns 股票代码列表
   */
  async getStockList(): Promise<string[]> {
    try {
      const response = await this.client.get<{ instruments: string[] }>('/instruments');
      return response.data.instruments || [];
    } catch (error) {
      console.warn('[QlibData] Failed to fetch stock list:', error);
      // 返回空数组而不是抛出错误，允许功能降级
      return [];
    }
  }

  /**
   * 清除所有缓存
   */
  clearCache(): void {
    this.cache.clear();
    console.log('[QlibData] Cache cleared');
  }

  /**
   * 清除特定股票的缓存
   * @param symbol 股票代码
   */
  clearSymbolCache(symbol: string): void {
    // 清除所有与该股票相关的缓存
    const keysToRemove: string[] = [];
    this.cache['cache'].forEach((_, key) => {
      if (key.startsWith(`market_${symbol}_`)) {
        keysToRemove.push(key);
      }
    });
    keysToRemove.forEach(key => this.cache.remove(key));
    console.log(`[QlibData] Cleared cache for ${symbol}`);
  }
}

export const qlibDataService = new QlibDataService();
