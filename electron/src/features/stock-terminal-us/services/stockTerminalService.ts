/**
 * 美股个股终端服务。
 *
 * 取数实现复用跨市场基类（stock-terminal-shared/service.ts），这里只做两件美股特有的事：
 *  1. 端点前缀切到 `/stock-terminal-us`（K 线读本地 QuantUS parquet，不走 yahoo 外网）；
 *  2. 提供拆股事件，用于在 K 线上标注未复权价的跳变，以及详情面板的聚合数据。
 */

import { StockTerminalService, type KlineAdjust, type StockListParams } from '../../stock-terminal-shared/service';
import type { KlineBar, KlineMarker, KlineSplitsEvent, StockListItem, StockListResponse, StockProfile } from '../../stock-terminal-shared/types';
import type { UsStockDetail } from '../types';

export class UsStockTerminalService extends StockTerminalService {
  /**
   * 最近一次 K 线请求的 Promise。拆股事件只在「窗口内」返回（后端如此实现），
   * 所以标记不走第二次请求，而是复用同一次响应 —— 页面在同一个 effect 里
   * 先调 getDailyKline 再调 getKlineMarkers，这里存的是同一个 Promise。
   */
  private pendingKline: Promise<{ items: KlineBar[]; splits: KlineSplitsEvent[] }> | null = null;

  /**
   * 列表映射：后端返回的是美股口径条目（`display_name`/`cn_name`/`market_cap_yi`），
   * 这里翻译成搜索框与列表共用的 StockListItem。
   */
  override async getStockList(params: StockListParams): Promise<StockListResponse> {
    const raw = await super.getStockList(params);
    const items: StockListItem[] = (raw.items ?? []).map((it: any) => ({
      symbol: String(it.symbol ?? ''),
      name: String(it.display_name ?? it.cn_name ?? it.symbol ?? ''),
      board: '',
      industry: it.industry ?? it.sector_cn ?? null,
      close: it.close ?? null,
      pct_change: it.pct_change ?? null,
      total_mv: null,
      float_mv: null,
      pe: it.pe_ratio ?? null,
      pb: it.pb_ratio ?? null,
      is_st: false,
      fusion: null,
      side: null,
      signal_date: null,
      model: null,
      position_score: null,
      industry_top10_avg: null,
      board_top10_avg: null,
      cap_top10_avg: null,
      pct_industry: null,
      market_empty: null,
      cap_display: it.market_cap_yi ? `$${(it.market_cap_yi / 10000).toFixed(2)}万亿` : null,
    }));
    return { ...raw, items };
  }

  /** 个股详情聚合：估值/财务/分析师/财报/内部人/机构持仓/分红拆股，一次拉全 */
  async getDetail(symbol: string): Promise<UsStockDetail | null> {
    try {
      const resp = await this.client.get('/stock-terminal-us/detail', { params: { symbol } });
      return (resp.data?.data as UsStockDetail) ?? null;
    } catch {
      return null;
    }
  }

  /**
   * 头部信息映射：后端返回的是美股口径的扁平结构（display_name / sector_cn / has_quote …），
   * 这里翻译成页面骨架通用的 StockProfile，避免在共享组件里写市场分支。
   *
   * 美股没有 A 股那套语义，故 board / 宽基归属 / 概念 / 两融标记一律为空。
   */
  override async getProfile(symbol: string, date?: string): Promise<StockProfile | null> {
    try {
      const resp = await this.client.get('/stock-terminal-us/profile', { params: { symbol, ...(date ? { date } : {}) } });
      const d = resp.data?.data;
      if (!d) return null;
      const industry = [d.sector_cn, d.industry].filter((x: unknown) => typeof x === 'string' && x && x !== '未分类').join(' · ');
      return {
        symbol: String(d.symbol ?? symbol),
        name: String(d.display_name ?? d.cn_name ?? symbol),
        board: '',
        industry: industry || null,
        trade_date: String(d.trade_date ?? ''),
        close: d.close ?? null,
        pct_change: d.pct_change ?? null,
        total_mv: null,
        float_mv: null,
        total_share: null,
        free_float_share: null,
        pe_dynamic: d.pe_ratio ?? null,
        pb: d.pb_ratio ?? null,
        dividend_yield: d.dividend_yield ?? null,
        beta: null,
        staff_num: null,
        main_business: null,
        ipo_price: null,
        limit_up_price: null,
        limit_down_price: null,
        flags: { hs300: false, marginable: false, sh_hk_connect: false, is_st: false, is_hk_listed: false },
        valuation: {},
        index_membership: [],
        concepts: [],
        signal_date: null,
      };
    } catch {
      return null;
    }
  }

  protected override async fetchKline(
    symbol: string,
    days: number,
    adjust: KlineAdjust,
    start?: string,
    end?: string,
  ): Promise<{ items: KlineBar[]; splits: KlineSplitsEvent[] }> {
    const p = super.fetchKline(symbol, days, adjust, start, end);
    this.pendingKline = p.catch(() => ({ items: [], splits: [] }));
    return p;
  }

  /** K 线事件竖线：拆股日（A 股/港股基类返回空数组，这里才有内容） */
  override async getKlineMarkers(_symbol: string, _start?: string, _end?: string): Promise<KlineMarker[]> {
    const pending = this.pendingKline;
    if (!pending) return [];
    const res = await pending.catch(() => null);
    return (res?.splits ?? [])
      .filter((s) => s.date)
      .map((s) => ({
        date: s.date,
        label: s.ratio && s.ratio !== 1 ? `拆股 ${s.ratio}:1` : '拆股',
        color: '#f59e0b',
      }));
  }
}

export const stockTerminalService = new UsStockTerminalService({
  klineMarket: 'US',
  quoteMarket: 'US',
  paths: {
    stockList: '/stock-terminal-us/list',
    profile: '/stock-terminal-us/profile',
    kline: '/stock-terminal-us/kline',
    news: '/stock-terminal-us/news',
  },
});

export * from '../../stock-terminal-shared/service';
