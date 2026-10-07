// 市场数据服务API接口 - 增强版股票搜索
import axios from 'axios';
import { toSuffixCode } from '../utils/portfolioUtils';

// 基础配置 - 使用统一端口配置
import { SERVICE_URLS } from '../config/services';
const resolveApiBaseURL = () => String(SERVICE_URLS.DATA_SERVICE || '').replace(/\/+$/, '');

// 创建axios实例
const apiClient = axios.create({
  timeout: 30000,
  headers: {
    'Content-Type': 'application/json',
  },
});

// 数据类型定义
export interface StockInfo {
  code: string;
  name: string;
  exchange?: string;
  market?: string;
  industry?: string;
  category?: string;
  price?: number | null;
  currentPrice?: number | null; // 增强版股票池使用的字段
  change?: number | null;
  change_percent?: number | null;
  change_pct?: number | null;
  volume?: number | null;
  market_cap?: number | null;
  turnover_rate?: number | null;
  pe_ttm?: number | null;
  pb?: number | null;
  data_source?: string;
  symbol?: string; // 兼容性字段
  update_time?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface SearchResult {
  success: boolean;
  data: StockInfo[];
  total: number;
  message?: string;
  data_source?: string;
  limit?: number;
  offset?: number;
}

export interface StockConditions {
  industries?: string[];
  technical?: {
    marketCap?: { min?: number; max?: number };
    price?: { min?: number; max?: number };
  };
  fundamental?: {
    market_cap?: { min?: number; max?: number };
    pe_ratio?: { min?: number; max?: number };
    roe?: { min?: number };
    revenue_growth?: { min?: number };
    debt_ratio?: { max?: number };
  };
}

export interface StockListParams {
  page?: number;
  limit?: number;
  category?: string;
  offset?: number;
  search_type?: 'all' | 'code' | 'name';
  market?: string;
}

export interface HealthStatus {
  success: boolean;
  message: string;
  data: {
    service: string;
    status: string;
    timestamp: string;
    database: string;
    local_cache_exists?: boolean;
    main_table_exists?: boolean;
  };
}

import { authService } from '../features/auth/services/authService';

// 添加请求和响应拦截器
apiClient.interceptors.request.use(
  (config) => {
    config.baseURL = resolveApiBaseURL();
    apiClient.defaults.baseURL = config.baseURL;
    const token = authService.getAccessToken();
    if (token) {
      config.headers.Authorization = `Bearer ${token}`;
    }
    console.log('增强版市场数据API请求:', config.method?.toUpperCase(), config.url);
    return config;
  },
  (error) => {
    console.error('增强版市场数据API请求错误:', error);
    return Promise.reject(error);
  }
);

apiClient.interceptors.response.use(
  (response) => {
    console.log('增强版市场数据API响应:', response.status, response.config.url);
    return response;
  },
  async (error) => {
    // 交由 authService 统一处理 401 Token 刷新与重试
    if (error.response?.status === 401) {
      return authService.handle401Error(error, apiClient);
    }

    console.error('增强版市场数据API响应错误:', error);
    return Promise.reject(error);
  }
);

// API请求封装
class MarketDataService {
  private static instance: MarketDataService;

  static getInstance(): MarketDataService {
    if (!MarketDataService.instance) {
      MarketDataService.instance = new MarketDataService();
    }
    return MarketDataService.instance;
  }

  // 增强版股票搜索 - 优先匹配本地缓存
  async searchStocks(keyword: string, limit: number = 10): Promise<SearchResult> {
    console.log('增强版股票搜索:', { keyword, limit });

    if (!keyword?.trim()) {
      return {
        success: false,
        data: [],
        total: 0,
        message: '搜索关键词不能为空'
      };
    }

    try {
      const response = await apiClient.get('/api/v1/stocks/search', {
        params: {
          q: keyword.trim(),
          keyword: keyword.trim(),
          limit: Math.min(Math.max(limit, 1), 100), // 限制在1-100之间
          offset: 0,
          search_type: 'all'
        }
      });

      // 格式化数据以兼容前端组件
      const raw = response.data || {};
      const rawList = Array.isArray(raw?.results) ? raw.results : (Array.isArray(raw?.data) ? raw.data : []);
      const normalizedList = rawList.map((stock: unknown) => ({
          ...(stock as Record<string, unknown>),
          symbol: String((stock as Record<string, unknown>).symbol ?? (stock as Record<string, unknown>).code ?? ''), // 添加symbol字段以兼容前端组件
          change: (stock as Record<string, unknown>).change_pct as number | undefined, // 添加change字段
          category: (stock as Record<string, unknown>).industry as string | undefined // 添加category字段
      }));

      return {
        success: true,
        data: normalizedList as StockInfo[],
        total: Number(raw?.total ?? normalizedList.length ?? 0),
        message: raw?.message,
        data_source: raw?.source || raw?.data_source,
        limit: Number(raw?.limit ?? limit),
        offset: Number(raw?.offset ?? 0),
      };
    } catch (error) {
      console.error('增强版股票搜索失败:', error);
      if (axios.isAxiosError(error)) {
        const status = error.response?.status;
        const message = error.response?.data?.message || error.message;

        if (status === 404) {
          return {
            success: false,
            data: [],
            total: 0,
            message: '股票搜索服务不可用'
          };
        } else if (status && status >= 500) {
          return {
            success: false,
            data: [],
            total: 0,
            message: '服务器内部错误，请稍后重试'
          };
        } else {
          return {
            success: false,
            data: [],
            total: 0,
            message: `搜索失败: ${message}`
          };
        }
      }
      return {
        success: false,
        data: [],
        total: 0,
        message: '网络连接失败，请检查增强版股票搜索服务是否运行'
      };
    }
  }

  // 获取股票详细信息
  async getStockDetail(code: string): Promise<{
    success: boolean;
    data?: StockInfo;
    message: string;
    data_source?: string;
  }> {
    console.log('获取股票详情:', code);

    if (!code?.trim()) {
      return {
        success: false,
        message: '股票代码不能为空'
      };
    }

    try {
      const normalized = this.normalizeStockSymbol(code);
      const normalizedCode = encodeURIComponent(normalized);
      const response = await apiClient.get(`/api/v1/stocks/${normalizedCode}`);
      const raw = response.data || {};
      const payload = (raw?.data && typeof raw.data === 'object') ? raw.data : raw;
      const name = String(
        payload?.name
        ?? payload?.stock_name
        ?? payload?.sec_name
        ?? payload?.symbol_name
        ?? '',
      ).trim();

      if (name) {
        return {
          success: true,
          message: String(raw?.message || '获取股票信息成功'),
          data_source: raw?.source || raw?.data_source,
          data: {
            ...payload,
            code: String(payload?.code ?? payload?.symbol ?? code).toUpperCase(),
            symbol: String(payload?.symbol ?? payload?.code ?? code).toUpperCase(),
            name,
            change: payload?.change ?? payload?.change_pct,
            category: payload?.category ?? payload?.industry,
          } as StockInfo,
        };
      }

      const searchResp = await this.searchStocks(code.trim(), 10);
      const exact = (searchResp.data || []).find((item) => {
        const symbol = String(item.symbol || '').toUpperCase();
        const itemCode = String(item.code || '').toUpperCase();
        const target = code.trim().toUpperCase();
        return symbol === target || itemCode === target || itemCode.startsWith(target.split('.')[0]);
      }) || (searchResp.data || [])[0];

      if (exact && String(exact.name || '').trim()) {
        return {
          success: true,
          message: '通过搜索结果匹配到股票信息',
          data_source: searchResp.data_source,
          data: {
            ...exact,
            code: String(exact.code || exact.symbol || code).toUpperCase(),
            symbol: String(exact.symbol || exact.code || code).toUpperCase(),
            name: String(exact.name || '').trim(),
          },
        };
      }

      return {
        success: false,
        message: `股票 ${code} 名称未找到`,
      };
    } catch (error) {
      console.error('获取股票详情失败:', error);
      try {
        const searchResp = await this.searchStocks(code.trim(), 10);
        const exact = (searchResp.data || []).find((item) => {
          const symbol = String(item.symbol || '').toUpperCase();
          const itemCode = String(item.code || '').toUpperCase();
          const target = code.trim().toUpperCase();
          return symbol === target || itemCode === target || itemCode.startsWith(target.split('.')[0]);
        }) || (searchResp.data || [])[0];

        if (exact && String(exact.name || '').trim()) {
          return {
            success: true,
            message: '详情接口异常，已通过搜索结果匹配股票信息',
            data_source: searchResp.data_source,
            data: {
              ...exact,
              code: String(exact.code || exact.symbol || code).toUpperCase(),
              symbol: String(exact.symbol || exact.code || code).toUpperCase(),
              name: String(exact.name || '').trim(),
            },
          };
        }
      } catch (_fallbackError) {
        // ignore fallback error, continue with original error mapping
      }

      if (axios.isAxiosError(error)) {
        const status = error.response?.status;
        const message = error.response?.data?.message || error.message;

        if (status === 404) {
          return {
            success: false,
            message: `股票 ${code} 未找到`
          };
        } else if (status && status >= 500) {
          return {
            success: false,
            message: '服务器内部错误，请稍后重试'
          };
        } else {
          return {
            success: false,
            message: `获取失败: ${message}`
          };
        }
      }
      return {
        success: false,
        message: '网络连接失败，请检查增强版股票搜索服务是否运行'
      };
    }
  }

  /**
   * 批量获取股票详细信息 - 带分批控制以防超时
   * @param codes 股票代码列表
   * @param batchSize 每批次请求数量，默认 5
   * @param delayMs 批次间延迟（毫秒），默认 100
   */
  async getStockDetailsBatch(
    codes: string[],
    batchSize: number = 5,
    delayMs: number = 100
  ): Promise<Array<{ code: string; result: any }>> {
    console.log(`[MarketDataService] 开始分批获取股票详情: 总数=${codes.length}, 每批=${batchSize}`);
    const results: Array<{ code: string; result: any }> = [];

    for (let i = 0; i < codes.length; i += batchSize) {
      const batch = codes.slice(i, i + batchSize);
      const batchIdx = Math.floor(i / batchSize) + 1;
      const totalBatches = Math.ceil(codes.length / batchSize);
      
      console.log(`[MarketDataService] 正在请求第 ${batchIdx}/${totalBatches} 批: ${batch.join(', ')}`);

      const batchPromises = batch.map(async (code) => {
        try {
          const result = await this.getStockDetail(code);
          return { code, result };
        } catch (err) {
          console.error(`[MarketDataService] 请求股票详情失败: ${code}`, err);
          return { code, result: { success: false, message: String(err) } };
        }
      });

      const batchResults = await Promise.all(batchPromises);
      results.push(...batchResults);

      // 如果不是最后一批，且设置了延迟，则等待
      if (i + batchSize < codes.length && delayMs > 0) {
        await new Promise(resolve => setTimeout(resolve, delayMs));
      }
    }

    return results;
  }

  // 获取热门股票 - 基于本地缓存
  async getPopularStocks(): Promise<StockInfo[]> {
    console.log('获取热门股票（基于本地缓存）');

    try {
      const response = await apiClient.get('/api/v1/stocks/popular', {
        params: { limit: 8 }
      });

      const result = response.data;
      if (result.success && result.data) {
        return result.data.map((stock: unknown) => ({
          ...(stock as Record<string, unknown>),
          symbol: String((stock as Record<string, unknown>).code ?? ''), // 确保symbol字段存在
          category: (stock as Record<string, unknown>).industry as string | undefined || (stock as Record<string, unknown>).category as string | undefined // 使用industry作为category
        }));
      }

      // 如果API失败，返回预设的热门股票
      return this.getFallbackPopularStocks();
    } catch (error) {
      console.error('获取热门股票失败:', error);
      return this.getFallbackPopularStocks();
    }
  }

  // 备用热门股票列表
  private getFallbackPopularStocks(): StockInfo[] {
    return [
      { code: '000001.SZ', name: '平安银行', exchange: 'SZSE', market: 'SZSE', symbol: '000001.SZ', industry: '银行' },
      { code: '000002.SZ', name: '万科A', exchange: 'SZSE', market: 'SZSE', symbol: '000002.SZ', industry: '房地产' },
      { code: '600519.SH', name: '贵州茅台', exchange: 'SSE', market: 'SSE', symbol: '600519.SH', industry: '白酒' },
      { code: '000858.SZ', name: '五粮液', exchange: 'SZSE', market: 'SZSE', symbol: '000858.SZ', industry: '白酒' },
      { code: '600036.SH', name: '招商银行', exchange: 'SSE', market: 'SSE', symbol: '600036.SH', industry: '银行' },
      { code: '002415.SZ', name: '海康威视', exchange: 'SZSE', market: 'SZSE', symbol: '002415.SZ', industry: '安防' },
      { code: '300750.SZ', name: '宁德时代', exchange: 'SZSE', market: 'SZSE', symbol: '300750.SZ', industry: '电池' },
      { code: '688981.SH', name: '中芯国际', exchange: 'SSE', market: 'SSE', symbol: '688981.SH', industry: '半导体' }
    ];
  }

  // 根据分类获取股票
  async getStocksByCategory(category: string, limit: number = 20): Promise<StockInfo[]> {
    try {
      const result = await this.searchStocks(category, limit);
      return result.data.map(stock => ({
        ...stock,
        symbol: stock.code,
        change: stock.change_pct,
        category: stock.industry
      }));
    } catch (error) {
      console.error('根据分类获取股票失败:', error);
      return [];
    }
  }

  // 快速搜索
  async quickSearch(keyword: string): Promise<SearchResult> {
    try {
      const response = await apiClient.get(`/api/v1/stocks/quick-search/${encodeURIComponent(keyword.trim())}`);

      // 格式化数据以兼容前端组件
      const result = response.data;
      if (result.success && result.data) {
        result.data = result.data.map((stock: unknown) => ({
          ...(stock as Record<string, unknown>),
          symbol: String((stock as Record<string, unknown>).code ?? ''),
          change: (stock as Record<string, unknown>).change_pct as number | undefined,
          category: (stock as Record<string, unknown>).industry as string | undefined
        }));
      }

      return result;
    } catch (error) {
      console.error('快速搜索失败:', error);
      // 回退到标准搜索
      return this.searchStocks(keyword, 10);
    }
  }

  // 健康检查
  async healthCheck(): Promise<HealthStatus | null> {
    try {
      const response = await apiClient.get('/api/health', {
        timeout: 5000
      });
      return response.data;
    } catch (error) {
      console.error('增强版股票搜索服务健康检查失败:', error);
      return null;
    }
  }

  // 检查服务是否可用
  async isServiceAvailable(): Promise<boolean> {
    try {
      const health = await this.healthCheck();
      return !!(health?.success && health?.data?.status === 'healthy');
    } catch (error) {
      return false;
    }
  }

  // 格式化股票显示名称
  formatStockDisplay(stock: StockInfo): string {
    return `${stock.name} (${stock.code})`;
  }

  // 验证股票代码格式
  validateStockSymbol(symbol: string): boolean {
    // 支持多种格式：000001.SZ, 000001, 平安银行等
    if (!symbol || symbol.trim().length === 0) {
      return false;
    }

    // 如果是纯数字，检查是否为6位
    if (/^\d+$/.test(symbol.trim())) {
      return symbol.trim().length === 6;
    }

    // 如果包含点，检查格式
    if (symbol.includes('.')) {
      const parts = symbol.split('.');
      if (parts.length === 2) {
        const code = parts[0];
        const suffix = parts[1].toUpperCase();
        return /^\d{6}$/.test(code) && ['SZ', 'SH', 'BJ'].includes(suffix);
      }
    }

    // 中文名称或其他格式都认为有效
    return true;
  }

  // 自动补全/规范化股票代码格式 (转换为 600000.SH 格式)。
  // 口径以 utils/portfolioUtils.toSuffixCode 为准（前缀/后缀/裸码统一收敛），
  // 本方法仅做委托，禁止在此手写号段/正则——旧正则 ^[S[HZB]J]\d{6}$ 的字符类
  // 只匹配单字符，前缀式代码从未被转换过；旧裸码推断把 9xxxxx（沪B）与
  // 92xxxx（新北交所号段）一律判成 SZ。
  normalizeStockSymbol(input: string): string {
    return toSuffixCode(input);
  }

  // 搜索建议（自动补全）
  async getSearchSuggestions(keyword: string, limit: number = 5): Promise<string[]> {
    if (!keyword || keyword.trim().length < 1) {
      return [];
    }

    try {
      const result = await this.searchStocks(keyword, limit);
      return result.data.map(stock => stock.name || stock.code);
    } catch (error) {
      console.error('获取搜索建议失败:', error);
      return [];
    }
  }

  // 根据动态条件获取股票池
  async getStocksByConditions(conditions: StockConditions, limit: number = 20): Promise<SearchResult> {
    console.log('根据动态条件获取股票池:', { conditions, limit });

    try {
      // 构建查询参数
      const params: Record<string, unknown> = {
        limit: Math.min(limit, 100),
        offset: 0
      };

      // 添加行业筛选
      if (conditions.industries && conditions.industries.length > 0) {
        params.industries = conditions.industries.join(',');
      }

      // 添加市值筛选
      if (conditions.technical?.marketCap) {
        if (conditions.technical.marketCap.min) {
          params.min_market_cap = conditions.technical.marketCap.min;
        }
        if (conditions.technical.marketCap.max) {
          params.max_market_cap = conditions.technical.marketCap.max;
        }
      }

      // 添加价格筛选
      if (conditions.technical?.price) {
        if (conditions.technical.price.min) {
          params.min_price = conditions.technical.price.min;
        }
        if (conditions.technical.price.max) {
          params.max_price = conditions.technical.price.max;
        }
      }

      // 添加基本面筛选
      if (conditions.fundamental) {
        if (conditions.fundamental.market_cap?.min) {
          params.min_market_cap = conditions.fundamental.market_cap.min;
        }
        if (conditions.fundamental.market_cap?.max) {
          params.max_market_cap = conditions.fundamental.market_cap.max;
        }
        if (conditions.fundamental.pe_ratio?.min) {
          params.min_pe = conditions.fundamental.pe_ratio.min;
        }
        if (conditions.fundamental.pe_ratio?.max) {
          params.max_pe = conditions.fundamental.pe_ratio.max;
        }
        if (conditions.fundamental.roe?.min) {
          params.min_roe = conditions.fundamental.roe.min;
        }
        if (conditions.fundamental.revenue_growth?.min) {
          params.min_revenue_growth = conditions.fundamental.revenue_growth.min;
        }
        if (conditions.fundamental.debt_ratio?.max) {
          params.max_debt_ratio = conditions.fundamental.debt_ratio.max;
        }
      }

      // 调用API（如果后端支持动态筛选）
      const response = await apiClient.get('/api/v1/stocks/filter', { params });

      // 格式化数据
      const result = response.data;
      if (result.success && result.data) {
        result.data = result.data.map((stock: unknown) => ({
          ...(stock as Record<string, unknown>),
          symbol: String((stock as Record<string, unknown>).code ?? ''),
          change: (stock as Record<string, unknown>).change_pct as number | undefined,
          category: (stock as Record<string, unknown>).industry as string | undefined
        }));
      }

      return result;
    } catch (error) {
      console.error('根据动态条件获取股票池失败:', error);

      // 回退到模拟数据
      return this.getFilteredStocksFallback(conditions, limit);
    }
  }

  // 动态筛选回退数据
  private async getFilteredStocksFallback(conditions: StockConditions, limit: number): Promise<SearchResult> {
    console.log('使用回退数据进行动态筛选');

    try {
      // 获取基础股票列表
      const popularStocks = await this.getPopularStocks();

      let filteredStocks = [...popularStocks];

      // 简单的行业筛选
      if (conditions.industries && conditions.industries.length > 0) {
        const industryMap: Record<string, string[]> = {
          'technology': ['半导体', '软件', '通信', '安防'],
          'finance': ['银行', '保险', '证券'],
          'medical': ['医药', '生物', '医疗'],
          'consumption': ['白酒', '食品', '零售'],
          'energy': ['新能源', '电力', '石油'],
          'materials': ['化工', '钢铁', '建材'],
          'industrial': ['机械', '制造', '工程'],
          'utilities': ['公用事业', '环保'],
          'real_estate': ['房地产'],
          'telecom': ['通信', '电信']
        };

        const selectedIndustries = conditions.industries.flatMap((ind: string) =>
          industryMap[ind] || []
        );

        if (selectedIndustries.length > 0) {
          filteredStocks = filteredStocks.filter(stock =>
            selectedIndustries.some(industry =>
              stock.industry?.includes(industry)
            )
          );
        }
      }

      // 市值筛选
      if (conditions.technical?.marketCap?.min) {
        filteredStocks = filteredStocks.filter(stock =>
          (stock.market_cap || 0) >= conditions.technical.marketCap.min * 100000000 // 转换为元
        );
      }
      if (conditions.technical?.marketCap?.max) {
        filteredStocks = filteredStocks.filter(stock =>
          (stock.market_cap || 0) <= conditions.technical.marketCap.max * 100000000
        );
      }

      // 限制数量
      if (limit && filteredStocks.length > limit) {
        filteredStocks = filteredStocks.slice(0, limit);
      }

      // 添加currentPrice字段
      filteredStocks = filteredStocks.map(stock => ({
        ...stock,
        currentPrice: stock.price || null
      }));

      return {
        success: true,
        data: filteredStocks,
        total: filteredStocks.length,
        message: `成功筛选出${filteredStocks.length}只股票`,
        data_source: 'fallback'
      };
    } catch (error) {
      console.error('回退数据生成失败:', error);
      return {
        success: false,
        data: [],
        total: 0,
        message: '筛选失败，请稍后重试'
      };
    }
  }

  // 获取行业列表
  async getIndustries(): Promise<string[]> {
    try {
      const response = await apiClient.get('/api/v1/stocks/industries');
      return response.data.data || [];
    } catch (error) {
      console.error('获取行业列表失败:', error);
      // 返回预设行业列表
      return [
        '银行', '保险', '证券', '房地产', '白酒', '医药', '生物',
        '半导体', '软件', '通信', '安防', '新能源', '电力', '机械',
        '制造', '化工', '钢铁', '建材', '公用事业', '环保', '食品', '零售'
      ];
    }
  }
}

// 导出单例实例
export const marketDataService = MarketDataService.getInstance();
