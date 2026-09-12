/**
 * 个股终端 API 服务 —— 跨市场唯一实现。
 *
 * 三个市场（A 股 / 港股 / 美股）此前各自复制了一份 300 行的服务文件，
 * 实际差异只有十几次赋值（market 常量、指数代码、baseURL 写法），
 * 且已经漂移出「港股副本缺少 resolveWebSafeServiceBase 修复」这类问题。
 * 现改为：本文件是唯一实现，各市场在 services/stockTerminalService.ts 里只提供配置。
 */

import axios, { AxiosInstance } from 'axios';
import { SERVICE_ENDPOINTS, resolveWebSafeServiceBase } from '../../config/services';
import { authService } from '../auth/services/authService';
import { KlineBar, KlineMarker, KlineSplitsEvent, StockListResponse, StockProfile } from './types';

/** 复权方式：qfq=前复权（默认）/ hfq=后复权 / none=不复权。仅 A 股日线后端真正支持三种；港股/美股固定 none */
export type KlineAdjust = 'qfq' | 'hfq' | 'none';

export interface TerminalMarketConfig {
  /** `/market/kline` 的 market 参数：A / HK / US */
  klineMarket: 'A' | 'HK' | 'US';
  /** `/market/quotes` 的 market 参数：CN / HK / US */
  quoteMarket: 'CN' | 'HK' | 'US';
  /**
   * `/stock-terminal/list` 的强制 market 参数。
   * A 股传 undefined（按后端默认全市场 + 前端 SH/SZ/BJ 过滤）；港股传 'HK'。
   */
  listMarket?: string;
  /** 大盘均线卡使用的指数代码（A 股 000001.SH / 港股 HSI.HK）；美股后端不支持该端点，留空 */
  indexMaSymbol?: string;
  /**
   * 端点路径覆盖。A 股/港股沿用既有的 /stock-terminal/* 与 /market/kline；
   * 美股的数据层是独立包（/stock-terminal-us/*，K 线读本地 parquet 而非 yahoo 外网），
   * 故只覆盖用到的那几条。
   */
  paths?: {
    stockList?: string;
    profile?: string;
    kline?: string;
    news?: string;
  };
}

export interface StockListParams {
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

export class StockTerminalService {
  constructor(private readonly cfg: TerminalMarketConfig) {}

  protected get client(): AxiosInstance {
    const baseURL = resolveWebSafeServiceBase(
      (import.meta as any).env?.VITE_USER_API_URL,
      SERVICE_ENDPOINTS.API_GATEWAY || SERVICE_ENDPOINTS.USER_SERVICE,
    );
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

  async getStockList(params: StockListParams): Promise<StockListResponse> {
    const withMarket = this.cfg.listMarket ? { ...params, market: this.cfg.listMarket } : params;
    const resp = await this.client.get(this.cfg.paths?.stockList ?? '/stock-terminal/list', { params: withMarket });
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
      const resp = await this.client.get(this.cfg.paths?.profile ?? '/stock-terminal/profile', { params: { symbol, ...(date ? { date } : {}) } });
      return resp.data?.data ?? null;
    } catch {
      return null;
    }
  }

  /**
   * K 线原始响应（含窗口内事件）。子类可覆盖以复用同一次响应里的事件数据，
   * 避免为了拿拆股标记再打一次同样的请求。
   */
  protected async fetchKline(
    symbol: string,
    days: number,
    adjust: KlineAdjust,
    start?: string,
    end?: string,
  ): Promise<{ items: KlineBar[]; splits: KlineSplitsEvent[] }> {
    const resp = await this.client.get(this.cfg.paths?.kline ?? '/market/kline', {
      params: { symbol, market: this.cfg.klineMarket, adjust, ...(start ? { start, end } : { days }) },
    });
    const data = resp.data?.data ?? {};
    const items = (data.items ?? [])
      .map((it: any) => ({
        date: String(it.date ?? '').slice(0, 10),
        open: Number(it.open),
        high: Number(it.high),
        low: Number(it.low),
        close: Number(it.close),
        volume: it.volume != null ? Number(it.volume) : null,
        amount: it.amount != null ? Number(it.amount) : null,
      }))
      .filter((b: KlineBar) => b.date && Number.isFinite(b.close));
    const splits: KlineSplitsEvent[] = (data.splits ?? [])
      .map((s: any) => ({ date: String(s.date ?? '').slice(0, 10), ratio: s.ratio ?? null }))
      .filter((s: KlineSplitsEvent) => s.date);
    return { items, splits };
  }

  /** 日K：传 start/end 时按精确日期区间拉取（后端不再做 days×2 自然日放大）；否则按 days 回溯 */
  async getDailyKline(symbol: string, days = 500, adjust: KlineAdjust = 'qfq', start?: string, end?: string): Promise<KlineBar[]> {
    try {
      return (await this.fetchKline(symbol, days, adjust, start, end)).items;
    } catch {
      return [];
    }
  }

  /**
   * K 线事件标记（拆股等）。默认无 —— A 股/港股不变；美股子类用拆股事件解释未复权价的跳变。
   */
  async getKlineMarkers(_symbol: string, _start?: string, _end?: string): Promise<KlineMarker[]> {
    return [];
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

  /** 指数快照（A 股：上证/深成/沪深300…；港股：恒指等；美股：标普/纳指/道指…）；asof 取历史日及之前最近行情 */
  async getIndexQuotes(asof?: string): Promise<IndexQuote[]> {
    try {
      const resp = await this.client.get('/market/quotes', { params: { market: this.cfg.quoteMarket, ...(asof ? { asof } : {}) } });
      return resp.data?.data?.quotes ?? [];
    } catch {
      return [];
    }
  }

  /** 大盘均线过滤（指数 MA5/10/20/30/60 + 可持仓判断）。未配置 indexMaSymbol 的市场返回 null */
  async getIndexMa(asof?: string): Promise<IndexMa | null> {
    if (!this.cfg.indexMaSymbol) return null;
    try {
      const resp = await this.client.get('/market/index-ma', {
        params: { symbol: this.cfg.indexMaSymbol, ...(asof ? { asof } : {}) },
      });
      return resp.data?.data ?? null;
    } catch {
      return null;
    }
  }

  /** 大盘 MA20 日历（指数收盘/MA20/偏离度 + 当日推理概况），供日期筛选弹层着色 */
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
      const resp = await this.client.get(this.cfg.paths?.news ?? '/stock-terminal/news', { params: { symbol } });
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
