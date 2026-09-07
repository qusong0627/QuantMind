// 简化版本：仅使用腾讯财经API
import { SERVICE_URLS } from '../config/services';
import { isMarketEnabled } from '../config/marketFlags';

export interface ApiResponse<T> {
  success: boolean;
  data?: T;
  error?: string;
  timestamp: string;
}

// 市场数据接口定义
export interface MarketIndex {
  symbol: string;
  name: string;
  price: number;
  change: number;
  changePercent: number;
  volume?: number;
  amount?: number;
  marketCap?: number;
  high?: number;
  low?: number;
  open?: number;
  preClose?: number;
  tradeDate?: string;
  timestamp?: string;
}

export interface MarketOverviewResponse {
  indices: MarketIndex[];
  lastUpdate: string;
  count: number;
  stats?: { up: number; down: number; flat: number; total: number };
  sourceUsed?: string;
}

export interface RealtimeQuote {
  symbol: string;
  price: number;
  change: number;
  changePercent: number;
  volume: number;
  amount: number;
  high: number;
  low: number;
  open: number;
  preClose: number;
  timestamp: string;
}

// 支持的8个主要指数
const SUPPORTED_INDICES = {
  'sh000001': '上证指数',
  'sz399001': '深成指数',
  'sz399006': '创业板指',
  'sh000300': '沪深300',
  'sh000905': '中证500',
  'sh000016': '上证50',
  'sz399005': '中小板指',
  'sz399102': '创业板综'
};

// 多市场指数配置
export type MarketId = 'CN' | 'HK' | 'US' | 'CRYPTO' | 'FUTURES';

export const MARKET_INDICES: Record<MarketId, { symbol: string; name: string; basePrice: number }[]> = {
  CN: [
    { symbol: 'sh000001', name: '上证指数', basePrice: 3200 },
    { symbol: 'sz399001', name: '深成指数', basePrice: 12000 },
    { symbol: 'sz399006', name: '创业板指', basePrice: 2500 },
    { symbol: 'sh000300', name: '沪深300', basePrice: 4200 },
    { symbol: 'sh000905', name: '中证500', basePrice: 6800 },
    { symbol: 'sh000016', name: '上证50', basePrice: 2800 },
  ],
  HK: [
    { symbol: 'hsi', name: '恒生指数', basePrice: 18000 },
    { symbol: 'hscei', name: '恒生国企', basePrice: 6500 },
    { symbol: 'hstech', name: '恒生科技', basePrice: 4000 },
    { symbol: 'hscas', name: '恒生综合', basePrice: 2800 },
    { symbol: 'hangseng_bank', name: '恒生金融', basePrice: 15000 },
    { symbol: 'hangseng_prop', name: '恒生地产', basePrice: 12000 },
  ],
  US: [
    { symbol: 'dji', name: '道琼斯', basePrice: 39000 },
    { symbol: 'ixic', name: '纳斯达克', basePrice: 16500 },
    { symbol: 'inx', name: '标普500', basePrice: 5200 },
    { symbol: 'rut', name: '罗素2000', basePrice: 2100 },
    { symbol: 'vix', name: 'VIX恐慌指数', basePrice: 15 },
    { symbol: 'ndx', name: '纳斯达克100', basePrice: 18000 },
  ],
  CRYPTO: [
    { symbol: 'btc', name: '比特币', basePrice: 73000 },
    { symbol: 'eth', name: '以太坊', basePrice: 2500 },
    { symbol: 'bnb', name: '币安币', basePrice: 650 },
    { symbol: 'sol', name: 'Solana', basePrice: 150 },
    { symbol: 'xrp', name: '瑞波币', basePrice: 2.2 },
    { symbol: 'ada', name: '艾达币', basePrice: 0.65 },
  ],
  FUTURES: [
    { symbol: 'cl', name: 'WTI原油', basePrice: 78 },
    { symbol: 'rb', name: '螺纹钢', basePrice: 3200 },
    { symbol: 'au', name: '沪金', basePrice: 780 },
    { symbol: 'cu', name: '沪铜', basePrice: 72000 },
    { symbol: 'ag', name: '沪银', basePrice: 7500 },
    { symbol: 'sc', name: '原油主力', basePrice: 520 },
  ],
};

// 腾讯财经API字段映射（完整标准）
const TENCENT_FIELD_MAP = {
  0: 'market',           // 市场代码 (1: SH, 51: SZ 等)
  1: 'name',             // 股票/指数名称
  2: 'code',             // 代码
  3: 'price',            // 当前价格
  4: 'preClose',         // 昨收价
  5: 'open',             // 今开盘价
  6: 'volume',           // 成交量（手）
  30: 'timestamp',       // 时间 (YYYYMMDDHHmmss)
  31: 'change',          // 涨跌额
  32: 'changePercent',   // 涨跌幅(%)
  33: 'high',            // 最高价
  34: 'low',             // 最低价
  35: 'rawPriceVolAmt',  // 价格/成交量/成交额(元)
  36: 'volumeShares',    // 成交量（手）
  37: 'amountWan',       // 成交额（万元）
};

// 错误处理配置
const ERROR_CONFIG = {
  MAX_RETRIES: 3,
  RETRY_DELAY: 1000,
  REQUEST_TIMEOUT: 8000,
  RATE_LIMIT_DELAY: 2000
};

class MarketService {
  // 重试请求方法
  private async retryRequest(fn: () => Promise<Response>, maxRetries: number = ERROR_CONFIG.MAX_RETRIES): Promise<Response> {
    let lastError: Error;

    for (let i = 0; i <= maxRetries; i++) {
      try {
        const result = await fn();
        return result;
      } catch (error) {
        lastError = error as Error;
        console.warn(`请求失败，第${i + 1}次重试:`, error);

        if (i < maxRetries) {
          await new Promise(resolve => setTimeout(resolve, ERROR_CONFIG.RETRY_DELAY * (i + 1)));
        }
      }
    }

    throw lastError!;
  }

  // 腾讯财经API获取实时行情数据
  async getTencentMarketData(): Promise<ApiResponse<MarketOverviewResponse>> {
    const symbols = Object.keys(SUPPORTED_INDICES);

    try {
      console.log('开始获取腾讯财经数据，支持指数:', symbols);

      const symbolsStr = symbols.join(',');
      const url = `https://qt.gtimg.cn/q=${symbolsStr}`;

      const response = await this.retryRequest(async () => {
        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), ERROR_CONFIG.REQUEST_TIMEOUT);

        const fetchResponse = await fetch(url, {
          method: 'GET',
          signal: controller.signal,
          headers: {
            'Accept': 'text/plain',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Referer': 'https://finance.qq.com/'
          }
        });

        clearTimeout(timeoutId);
        return fetchResponse;
      });

      if (!response.ok) {
        throw new Error(`HTTP ${response.status}: ${response.statusText}`);
      }

      const text = await response.text();
      console.log('腾讯财经API响应长度:', text.length);

      if (!text || text.trim().length === 0) {
        throw new Error('腾讯财经API返回空数据');
      }

      const indices = this.parseTencentData(text);

      if (indices.length === 0) {
        throw new Error('解析腾讯财经数据失败，未获取到有效指数数据');
      }

      console.log(`成功获取${indices.length}个指数数据`);

      const up = indices.filter(x => x.changePercent > 0).length;
      const down = indices.filter(x => x.changePercent < 0).length;
      const flat = indices.length - up - down;

      return {
        success: true,
        data: {
          indices,
          lastUpdate: indices[0]?.tradeDate || new Date().toISOString(),
          count: indices.length,
          stats: { up, down, flat, total: indices.length },
          sourceUsed: 'tencent_realtime',
        },
        timestamp: new Date().toISOString()
      };
    } catch (error) {
      console.error('腾讯财经API获取失败:', error);
      return {
        success: false,
        error: error instanceof Error ? error.message : '获取腾讯财经数据失败',
        timestamp: new Date().toISOString()
      };
    }
  }

  // 后端本地 parquet 多市场概览（真实行情，覆盖全部市场）
  private async getBackendOverview(market: MarketId): Promise<ApiResponse<MarketOverviewResponse>> {
    try {
      const base = SERVICE_URLS.API_GATEWAY;
      const url = `${base}/api/v1/market/overview?market=${market}`;
      const token = localStorage.getItem('access_token') || localStorage.getItem('auth_token');
      const resp = await fetch(url, {
        method: 'GET',
        headers: token ? { Authorization: `Bearer ${token}` } : undefined,
      });
      if (!resp.ok) {
        throw new Error(`HTTP ${resp.status}`);
      }
      const payload = await resp.json();
      if (!payload?.success || !payload.data?.indices) {
        throw new Error(payload?.error || '后端概览无数据');
      }
      const data = payload.data;
      // 后端字段映射到前端 MarketIndex
      const indices: MarketIndex[] = (data.indices || []).map((x: any) => ({
        symbol: x.symbol,
        name: x.name,
        price: x.price,
        change: x.change,
        changePercent: x.change_percent ?? x.changePercent ?? 0,
        volume: x.volume,
        amount: x.amount,
        high: x.high,
        low: x.low,
        open: x.open,
        preClose: x.pre_close,
        tradeDate: x.trade_date,
        timestamp: x.trade_date,
      }));
      return {
        success: true,
        data: {
          indices,
          lastUpdate: data.last_update || new Date().toISOString(),
          count: indices.length,
          stats: data.stats,
          sourceUsed: data.source_used,
        },
        timestamp: new Date().toISOString(),
      };
    } catch (error) {
      console.warn(`后端概览 ${market} 获取失败:`, error);
      return {
        success: false,
        error: error instanceof Error ? error.message : '后端概览获取失败',
        timestamp: new Date().toISOString()
      };
    }
  }

  // 解析腾讯财经API返回的数据（支持完整腾讯行情规范字段解析）
  private parseTencentData(text: string): MarketIndex[] {
    const indices: MarketIndex[] = [];
    const lines = text.split('\n').filter(line => line.trim());

    console.log('腾讯财经API原始数据长度:', text.length);

    lines.forEach(line => {
      try {
        // 腾讯财经数据格式: v_sh000001="1~上证指数~000001~3927.18~3926.96~3930.02~499525613~0~0~...~20260814161401~0.22~0.01~3932.64~3903.70~3927.18/499525613/990371924238~499525613~99037192~1.03~..."
        const match = line.match(/v_([^=]+)="([^"]+)"/);
        if (!match) return;

        const symbol = match[1];
        const data = match[2].split('~');

        // 验证数据字段数量（至少需要基础字段）
        if (data.length < 6) {
          console.warn(`${symbol}数据字段不足: ${data.length}，跳过`);
          return;
        }

        // 获取指数名称
        const name = SUPPORTED_INDICES[symbol as keyof typeof SUPPORTED_INDICES] || data[1] || symbol;

        // 腾讯财经行情字段映射:
        // data[3]: 当前价格
        // data[4]: 昨收价
        // data[5]: 今开盘价
        // data[30]: 时间 (YYYYMMDDHHmmss)
        // data[31]: 涨跌额
        // data[32]: 涨跌幅(%)
        // data[33]: 最高价
        // data[34]: 最低价
        // data[35]: 价格/成交量/成交额(元)
        // data[36]: 成交量(手)
        // data[37]: 成交额(万元)
        const price = this.safeParseFloat(data[3]);
        const preClose = this.safeParseFloat(data[4]);
        const open = this.safeParseFloat(data[5]);

        // 数据验证：价格必须大于0
        if (price <= 0) {
          console.warn(`${symbol}价格无效: ${price}，跳过`);
          return;
        }

        let change = 0;
        let changePercent = 0;

        if (data.length > 32 && data[32] !== '' && data[32] !== undefined) {
          change = this.safeParseFloat(data[31]);
          changePercent = this.safeParseFloat(data[32]);
        } else if (preClose > 0) {
          change = price - preClose;
          changePercent = (change / preClose) * 100;
        }

        const high = data.length > 33 ? this.safeParseFloat(data[33]) : undefined;
        const low = data.length > 34 ? this.safeParseFloat(data[34]) : undefined;
        const volume = data.length > 36 ? this.safeParseFloat(data[36]) : (data.length > 6 ? this.safeParseFloat(data[6]) : undefined);

        // 成交额：优先从 data[35]（单位：元）获取，或从 data[37]（单位：万元）换算为元
        let amount: number | undefined;
        if (data.length > 35 && data[35].includes('/')) {
          const parts = data[35].split('/');
          if (parts.length >= 3) {
            const rawAmt = this.safeParseFloat(parts[2]);
            if (rawAmt > 0) amount = rawAmt;
          }
        }
        if (!amount && data.length > 37) {
          const amtWan = this.safeParseFloat(data[37]);
          if (amtWan > 0) amount = amtWan * 10000;
        }

        let tradeDate = '';
        let timestamp = new Date().toISOString();
        if (data.length > 30 && data[30] && data[30].length >= 8) {
          const rawTime = data[30];
          tradeDate = `${rawTime.slice(0, 4)}-${rawTime.slice(4, 6)}-${rawTime.slice(6, 8)}`;
          if (rawTime.length >= 14) {
            timestamp = `${tradeDate}T${rawTime.slice(8, 10)}:${rawTime.slice(10, 12)}:${rawTime.slice(12, 14)}`;
          }
        }

        // 构建指数数据对象
        const indexData: MarketIndex = {
          symbol: symbol.toUpperCase(),
          name,
          price: Math.round(price * 100) / 100, // 保留2位小数
          change: Math.round(change * 100) / 100,
          changePercent: Math.round(changePercent * 100) / 100,
          volume: volume && volume > 0 ? Math.round(volume) : undefined,
          amount: amount && amount > 0 ? Math.round(amount * 100) / 100 : undefined,
          high: high && high > 0 ? Math.round(high * 100) / 100 : undefined,
          low: low && low > 0 ? Math.round(low * 100) / 100 : undefined,
          open: open && open > 0 ? Math.round(open * 100) / 100 : undefined,
          preClose: preClose && preClose > 0 ? Math.round(preClose * 100) / 100 : undefined,
          tradeDate: tradeDate || undefined,
          timestamp,
        };

        console.log(`${symbol}解析成功:`, {
          name: indexData.name,
          price: indexData.price,
          change: indexData.change,
          changePercent: indexData.changePercent,
          amount: indexData.amount
        });

        indices.push(indexData);

      } catch (err) {
        console.warn('解析行数据失败:', line.substring(0, 100), err);
      }
    });

    console.log(`解析完成，成功获取${indices.length}个指数数据`);
    return indices;
  }

  // 安全的数值解析方法
  private safeParseFloat(value: string | undefined): number {
    if (!value || value === '' || value === '--') {
      return 0;
    }
    const parsed = parseFloat(value);
    return isNaN(parsed) ? 0 : parsed;
  }

  // CoinGecko symbol → coin id 映射
  private static readonly COINGECKO_MAP: Record<string, string> = {
    btc: 'bitcoin',
    eth: 'ethereum',
    bnb: 'binancecoin',
    sol: 'solana',
    xrp: 'ripple',
    ada: 'cardano',
  };

  // 从 CoinGecko 获取加密货币实时行情
  private async getCryptoMarketData(): Promise<MarketIndex[] | null> {
    try {
      const ids = Object.values(MarketService.COINGECKO_MAP).join(',');
      const url = `https://api.coingecko.com/api/v3/simple/price?ids=${ids}&vs_currencies=usd&include_24hr_change=true&include_24hr_vol=true`;
      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), 8000);
      const resp = await fetch(url, { signal: controller.signal });
      clearTimeout(timeoutId);
      if (!resp.ok) return null;
      const data = await resp.json();

      const indices: MarketIndex[] = [];
      for (const [symbol, coinId] of Object.entries(MarketService.COINGECKO_MAP)) {
        const coin = data[coinId];
        if (!coin?.usd) continue;
        const meta = (MARKET_INDICES.CRYPTO || []).find(m => m.symbol === symbol);
        const price = coin.usd;
        const changePct = coin.usd_24h_change ?? 0;
        const change = price * changePct / 100;
        indices.push({
          symbol: symbol.toUpperCase(),
          name: meta?.name || symbol.toUpperCase(),
          price: parseFloat(price.toFixed(price >= 1 ? 2 : 4)),
          change: parseFloat(change.toFixed(price >= 1 ? 2 : 4)),
          changePercent: parseFloat(changePct.toFixed(2)),
          volume: coin.usd_24h_vol ? Math.round(coin.usd_24h_vol) : undefined,
          timestamp: new Date().toISOString(),
        });
      }
      return indices.length > 0 ? indices : null;
    } catch {
      return null;
    }
  }

  // 获取市场概览数据（简化版：仅使用腾讯财经API）
  async getMarketOverview(market: MarketId = 'CN'): Promise<ApiResponse<MarketOverviewResponse>> {
    try {
      console.log(`开始获取${market}市场概览数据...`);

      // 生产环境屏蔽区块链时，CRYPTO 直接返回空，避免 CoinGecko/Binance 连接
      if (market === 'CRYPTO' && !isMarketEnabled(market)) {
        return {
          success: false,
          error: 'CRYPTO market disabled',
          data: { indices: [], lastUpdate: '', count: 0 },
          timestamp: new Date().toISOString(),
        };
      }

      // A股：腾讯财经实时优先，不可达时后端日线兜底
      if (market === 'CN') {
        const tencentResponse = await this.getTencentMarketData();
        if (tencentResponse.success && tencentResponse.data && tencentResponse.data.indices.length > 0) {
          console.log(`成功获取腾讯财经实时数据，共${tencentResponse.data.indices.length}个指数`);
          return tencentResponse;
        }
        if (tencentResponse.error) {
          console.warn('腾讯财经实时不可达，回落后端日线:', tencentResponse.error);
        }
      }

      // 后端本地 parquet 真实行情兜底（覆盖全部 5 个市场；CN 为腾讯失败后的兜底）
      const backendResp = await this.getBackendOverview(market);
      if (backendResp.success && backendResp.data && backendResp.data.indices.length > 0) {
        console.log(`成功获取后端 ${market} 市场概览，共${backendResp.data.indices.length}个品种`);
        return backendResp;
      }

      // 加密货币使用 CoinGecko 实时行情
      if (market === 'CRYPTO') {
        const cryptoIndices = await this.getCryptoMarketData();
        if (cryptoIndices && cryptoIndices.length > 0) {
          console.log(`成功获取 CoinGecko 数据，共${cryptoIndices.length}个币种`);
          return {
            success: true,
            data: { indices: cryptoIndices, lastUpdate: new Date().toISOString(), count: cryptoIndices.length },
            timestamp: new Date().toISOString(),
          };
        }
        console.warn('CoinGecko API 失败，使用模拟数据');
      }

      // 非A股或API失败时，使用模拟数据
      console.warn(`${market}市场使用模拟数据`);
      return {
        success: true,
        data: this.generateMarketMockData(market),
        timestamp: new Date().toISOString()
      };

    } catch (error) {
      console.error('获取市场概览数据异常:', error);
      return {
        success: true,
        data: this.generateMarketMockData(market),
        timestamp: new Date().toISOString()
      };
    }
  }



  // 辅助方法：根据股票代码获取指数名称
  private getIndexName(symbol: string): string {
    const indexNames: Record<string, string> = {
      '000001.SH': '上证指数',
      '399001.SZ': '深成指数',
      '399006.SZ': '创业板指',
      '000300.SH': '沪深300',
      '000905.SH': '中证500',
      '000016.SH': '上证50',
      '399005.SZ': '中小板指'
    };

    return indexNames[symbol] || symbol;
  }

  // 生成模拟数据（支持8个主要指数）
  generateMockData(): MarketOverviewResponse {
    const mockIndices: MarketIndex[] = Object.entries(SUPPORTED_INDICES).map(([symbol, name]) => {
      const basePrice = this.getBasePriceForIndex(symbol);
      const change = (Math.random() - 0.5) * (basePrice * 0.03); // 最大3%波动
      const price = basePrice + change;
      const prevClose = price - change;
      const changePercent = prevClose > 0 ? (change / prevClose) * 100 : 0;

      return {
        symbol: symbol.toUpperCase(),
        name,
        price: parseFloat(price.toFixed(2)),
        change: parseFloat(change.toFixed(2)),
        changePercent: parseFloat(changePercent.toFixed(2)),
        volume: Math.floor(Math.random() * 1000000000),
        amount: Math.floor(Math.random() * 500000000000), // 成交额
        marketCap: Math.floor(Math.random() * 50000000000000), // 市值
        timestamp: new Date().toISOString()
      };
    });

    return {
      indices: mockIndices,
      lastUpdate: new Date().toISOString(),
      count: mockIndices.length
    };
  }

  // 生成指定市场的模拟数据
  generateMarketMockData(market: MarketId): MarketOverviewResponse {
    const indices = MARKET_INDICES[market] || MARKET_INDICES.CN;
    const volatility = market === 'CRYPTO' ? 0.06 : 0.03; // 加密货币波动更大

    const mockIndices: MarketIndex[] = indices.map(({ symbol, name, basePrice }) => {
      const change = (Math.random() - 0.5) * (basePrice * volatility);
      const price = basePrice + change;
      const prevClose = price - change;
      const changePercent = prevClose > 0 ? (change / prevClose) * 100 : 0;

      return {
        symbol: symbol.toUpperCase(),
        name,
        price: parseFloat(price.toFixed(2)),
        change: parseFloat(change.toFixed(2)),
        changePercent: parseFloat(changePercent.toFixed(2)),
        volume: Math.floor(Math.random() * 1000000000),
        amount: Math.floor(Math.random() * 500000000000),
        timestamp: new Date().toISOString()
      };
    });

    return {
      indices: mockIndices,
      lastUpdate: new Date().toISOString(),
      count: mockIndices.length
    };
  }

  // 获取指数基准价格
  private getBasePriceForIndex(symbol: string): number {
    const basePrices: Record<string, number> = {
      'sh000001': 3200, // 上证指数
      'sz399001': 12000, // 深成指数
      'sz399006': 2500, // 创业板指
      'sh000300': 4200, // 沪深300
      'sh000905': 6800, // 中证500
      'sh000016': 2800, // 上证50
      'sz399102': 1800, // 创业板综
      'sz399005': 8500  // 中小板指
    };

    return basePrices[symbol] || 3000;
  }
}

export const marketService = new MarketService();
