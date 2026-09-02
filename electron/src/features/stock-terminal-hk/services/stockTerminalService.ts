/** 个股终端 API 服务 */

import axios, { AxiosInstance } from 'axios';
import { SERVICE_ENDPOINTS } from '../../../config/services';
import { authService } from '../../auth/services/authService';
import { KlineBar, StockListResponse, StockProfile } from '../types';

/** 复权方式：qfq=前复权（默认）/ hfq=后复权 / none=不复权，仅 A 股日线生效 */
export type KlineAdjust = 'qfq' | 'hfq' | 'none';

class StockTerminalService {
  private get client(): AxiosInstance {
    const baseURL = (import.meta as any).env?.VITE_USER_API_URL || SERVICE_ENDPOINTS.API_GATEWAY || SERVICE_ENDPOINTS.USER_SERVICE;
    const client = axios.create({ baseURL, timeout: 30000 });
    client.interceptors.request.use((config) => {
      const token = authService.getAccessToken();
      if (token) {
        if (config.headers && typeof config.headers.set === 'function') {
          config.headers.set('Authorization', `Bearer ${token}`);
        } else if (config.headers) {
          config.headers['Authorization'] = `Bearer ${token}`;
        }
      }
      return config;
    });
    return client;
  }

  async getStockList(params: {
    market?: string;
    industry?: string;
    concept?: string;
    q?: string;
    date?: string;
    score_min?: number;
    score_max?: number;
    model?: string;
    board?: string;
    cap_tier?: string;
    trend?: string;
    tag?: string;
    index_code?: string;
    side?: string;
    /** 排除 ST 股 */
    exclude_st?: boolean;
    page?: number;
    page_size?: number;
    /** 附带各筛选下拉选项的命中数（option_counts） */
    with_counts?: boolean;
    /** 定位股票（600519.SH），返回当前排序中的名次（find_rank）供列表跳转 */
    find_symbol?: string;
    /** 自选股列表（逗号分隔，prefix/suffix/纯代码均可），按当前排序保留分数降序 */
    symbols?: string;
  }): Promise<StockListResponse> {
    // 港股版：列表接口固定 HK 市场（信号底表 engine_signal_scores 的 HK run）
    const resp = await this.client.get('/stock-terminal/list', { params: { ...params, market: 'HK' } });
    return resp.data?.data ?? { total: 0, page: 1, page_size: 100, trade_date: '', items: [] };
  }

  async getConcepts(): Promise<string[]> {
    try {
      const resp = await this.client.get('/stock-terminal/concepts');
      return resp.data?.data?.concepts ?? [];
    } catch {
      return [];
    }
  }

  async getIndustries(): Promise<string[]> {
    const resp = await this.client.get('/stock-terminal/industries');
    return resp.data?.data?.industries ?? [];
  }

  async getProfile(symbol: string, date?: string): Promise<StockProfile | null> {
    try {
      const resp = await this.client.get('/stock-terminal/profile', { params: { symbol, ...(date ? { date } : {}) } });
      return resp.data?.data ?? null;
    } catch {
      return null;
    }
  }

  /** 日K：传 start/end 时按精确日期区间拉取（后端不再做 days×2 自然日放大）；否则按 days 回溯 */
  async getDailyKline(symbol: string, days = 500, adjust: KlineAdjust = 'qfq', start?: string, end?: string): Promise<KlineBar[]> {
    try {
      const resp = await this.client.get('/market/kline', {
        params: { symbol, market: 'HK', adjust, ...(start ? { start, end } : { days }) },
      });
      const items = resp.data?.data?.items ?? [];
      return items.map((it: any) => ({
        date: String(it.date ?? '').slice(0, 10),
        open: Number(it.open),
        high: Number(it.high),
        low: Number(it.low),
        close: Number(it.close),
        volume: it.volume != null ? Number(it.volume) : null,
        amount: it.amount != null ? Number(it.amount) : null,
      })).filter((b: KlineBar) => b.date && Number.isFinite(b.close));
    } catch {
      return [];
    }
  }

  async getIndexKline(symbol: string, days = 500): Promise<{ date: string; close: number }[]> {
    try {
      const resp = await this.client.get('/market/index-kline', {
        params: { symbol, days },
      });
      const data = resp.data?.data ?? {};
      const dates: string[] = data.dates ?? [];
      const closes: number[] = data.close ?? [];
      return dates.map((d, i) => ({ date: String(d).slice(0, 10), close: Number(closes[i]) }))
        .filter(x => x.date && Number.isFinite(x.close));
    } catch {
      return [];
    }
  }

  /** 指数快照（上证/深成/沪深300/中证500/创业板/科创50/上证50/北证50）；asof 取历史日及之前最近行情 */
  async getIndexQuotes(asof?: string): Promise<IndexQuote[]> {
    try {
      const resp = await this.client.get('/market/quotes', { params: { market: 'HK', ...(asof ? { asof } : {}) } });
      return resp.data?.data?.quotes ?? [];
    } catch {
      return [];
    }
  }

  /** 大盘均线过滤（上证指数 MA5/10/20/30/60 + 可持仓判断） */
  async getIndexMa(asof?: string): Promise<IndexMa | null> {
    try {
      const resp = await this.client.get('/market/index-ma', {
        params: { symbol: 'HSI.HK', ...(asof ? { asof } : {}) },
      });
      return resp.data?.data ?? null;
    } catch {
      return null;
    }
  }

  /** 大盘 MA20 日历（上证收盘/MA20/偏离度 + 当日推理概况），供日期筛选弹层着色 */
  async getMarketCalendar(months = 12, model?: string, refresh = false): Promise<MarketCalendarData> {
    try {
      const resp = await this.client.get('/stock-terminal/market-calendar', {
        params: { months, ...(model ? { model } : {}), ...(refresh ? { refresh: true } : {}) },
      });
      return resp.data?.data ?? { index_symbol: '000001.SH', index_name: '上证指数', days: [] };
    } catch {
      return { index_symbol: '000001.SH', index_name: '上证指数', days: [] };
    }
  }

  async getMinuteKline(symbol: string, freq: 'min5' | 'min1', days = 10): Promise<{ items: KlineBar[]; available: boolean }> {
    try {
      const resp = await this.client.get('/stock-terminal/minute', { params: { symbol, freq, days } });
      const data = resp.data?.data ?? {};
      const items = (data.items ?? []).map((it: any) => ({
        date: String(it.date ?? ''),
        open: Number(it.open),
        high: Number(it.high),
        low: Number(it.low),
        close: Number(it.close),
        volume: it.volume != null ? Number(it.volume) : null,
        amount: it.amount != null ? Number(it.amount) : null,
      }));
      return { items, available: !!data.available };
    } catch {
      return { items: [], available: false };
    }
  }

  async getFinancials(symbol: string, limit = 8, date?: string): Promise<FinancialsResponse> {
    try {
      const resp = await this.client.get('/stock-terminal/financials', { params: { symbol, limit, ...(date ? { date } : {}) } });
      return resp.data?.data ?? { symbol, periods: [], income: [], balance: [], cashflow: [], per_share: [] };
    } catch {
      return { symbol, periods: [], income: [], balance: [], cashflow: [], per_share: [] };
    }
  }

  async getSeries(symbol: string, group: string, years = 3, endDate?: string): Promise<SeriesResponse> {
    try {
      const resp = await this.client.get('/stock-terminal/series', { params: { symbol, group, years, ...(endDate ? { end_date: endDate } : {}) } });
      return resp.data?.data ?? { dates: [], columns: {} };
    } catch {
      return { dates: [], columns: {} };
    }
  }

  async getNews(symbol: string): Promise<{ items: any[]; available: boolean }> {
    try {
      const resp = await this.client.get('/stock-terminal/news', { params: { symbol } });
      return resp.data?.data ?? { items: [], available: false };
    } catch {
      return { items: [], available: false };
    }
  }

  async getAiBacktest(symbol: string, hint = ''): Promise<any> {
    const resp = await this.client.get('/stock-terminal/ai-backtest', { params: { symbol, hint }, timeout: 60000 });
    return resp.data?.data;
  }

  async getChartBacktest(symbol: string, buyExpr: string, sellExpr: string, days = 500): Promise<any> {
    const resp = await this.client.get('/stock-terminal/chart-backtest', {
      params: { symbol, buy_expr: buyExpr, sell_expr: sellExpr, days },
      timeout: 60000,
    });
    return resp.data?.data;
  }

  async getSignalOverlay(symbol: string, days = 250): Promise<Record<string, { date: string; fusion: number | null; side: string }[]>> {
    try {
      const resp = await this.client.get('/stock-terminal/signal-overlay', { params: { symbol, days } });
      return resp.data?.data?.series ?? {};
    } catch {
      return {};
    }
  }

  async getTags(symbol: string): Promise<{ tags: any[]; presets: any[] }> {
    try {
      const resp = await this.client.get('/stock-terminal/tags', { params: { symbol }, timeout: 30000 });
      return resp.data?.data ?? { tags: [], presets: [] };
    } catch {
      return { tags: [], presets: [] };
    }
  }

  /** 标签同类股票：返回 {items, score_min, score_max}（当前模型全市场分数极值，供动态归一化显示） */
  async getTagStocks(tagId: string, limit = 30): Promise<{ items: any[]; score_min: number | null; score_max: number | null }> {
    try {
      const resp = await this.client.get(`/stock-terminal/tags/${tagId}/stocks`, { params: { limit }, timeout: 30000 });
      const data = resp.data?.data ?? {};
      return { items: data.items ?? [], score_min: data.score_min ?? null, score_max: data.score_max ?? null };
    } catch {
      return { items: [], score_min: null, score_max: null };
    }
  }

  async getDividends(symbol: string, date?: string): Promise<DividendItem[]> {
    try {
      const resp = await this.client.get('/stock-terminal/dividends', { params: { symbol, ...(date ? { date } : {}) } });
      return resp.data?.data?.items ?? [];
    } catch {
      return [];
    }
  }
}

export interface FinRecord { period: string; items: Record<string, number | null>; }
export interface IndexQuote {
  symbol: string;
  name: string;
  price: number;
  change: number;
  change_percent: number;
  trade_date?: string;
}
export interface IndexMa {
  symbol: string;
  name: string;
  trade_date: string;
  close: number | null;
  ma5: number | null;
  ma10: number | null;
  ma20: number | null;
  ma30: number | null;
  ma60: number | null;
  above_ma20: boolean;
  status: string;
}
export interface MarketCalendarDay {
  date: string;          // YYYY-MM-DD
  close: number;
  ma20: number;
  dev_pct: number;       // (close-ma20)/ma20*100，正=高于均线
  signal_count?: number; // 当日有分数的信号行数
  top10_avg?: number | null;  // 当日 Top10 推理信号平均分
  has_inference?: boolean;
}
export interface MarketCalendarData {
  index_symbol: string;
  index_name: string;
  days: MarketCalendarDay[];
}
export interface FinancialsResponse {
  symbol: string;
  periods: string[];
  income: FinRecord[];
  balance: FinRecord[];
  cashflow: FinRecord[];
  per_share: FinRecord[];
}
export interface SeriesResponse { dates: string[]; columns: Record<string, (number | null)[]>; }
export interface DividendItem {
  date: string; interest: number | null; stock_bonus: number | null;
  stock_gift: number | null; gugai: number | null; dr: number | null;
}

export const stockTerminalService = new StockTerminalService();
