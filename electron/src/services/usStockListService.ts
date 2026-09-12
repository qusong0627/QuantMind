/** 美股证券列表服务：从后端美股终端标的池加载（标普500 + 纳指补充，约 500 只），内存搜索 */

import { SERVICE_ENDPOINTS } from '../config/services';

export interface UsStock {
  symbol: string;   // AAPL
  name: string;     // 苹果
}

/** 标的池上限：/stock-terminal-us/list 的 page_size 上界是 600，一次取全做内存搜索 */
const POOL_PAGE_SIZE = 600;

class UsStockListService {
  private stocks: UsStock[] = [];
  private loadedFlag = false;
  private loadingPromise: Promise<void> | null = null;

  async load(): Promise<void> {
    if (this.loadedFlag) return;
    if (this.loadingPromise) return this.loadingPromise;
    this.loadingPromise = (async () => {
      try {
        const token = localStorage.getItem('access_token') || '';
        const url = `${SERVICE_ENDPOINTS.USER_SERVICE}/stock-terminal-us/list?page=1&page_size=${POOL_PAGE_SIZE}`;
        const resp = await fetch(url, { headers: token ? { Authorization: `Bearer ${token}` } : {} });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const body = await resp.json() as { data?: { items?: Array<{ symbol?: string; name?: string; cn_name?: string; en_name?: string }> } };
        this.stocks = (body.data?.items ?? [])
          .map((it) => ({
            symbol: String(it.symbol ?? '').toUpperCase(),
            name: String(it.name ?? it.cn_name ?? it.en_name ?? ''),
          }))
          .filter((s) => s.symbol);
        this.stocks.sort((a, b) => a.symbol.localeCompare(b.symbol));
        this.loadedFlag = true;
      } catch (e) {
        console.warn('[UsStockList] 美股标的池加载失败:', e);
        throw e;
      } finally {
        this.loadingPromise = null;
      }
    })();
    return this.loadingPromise;
  }

  isLoaded(): boolean { return this.loadedFlag; }

  /** 代码或名称模糊搜索（最多 limit 条）；中文名走 includes，ticker 前缀优先 */
  search(keyword: string, limit = 10): UsStock[] {
    const kw = keyword.trim().toLowerCase();
    if (!kw) return [];
    const out: UsStock[] = [];
    // 先按 ticker 前缀（用户多半在敲代码），再按中文/英文名包含
    for (const s of this.stocks) {
      if (s.symbol.toLowerCase().startsWith(kw)) {
        out.push(s);
        if (out.length >= limit) return out;
      }
    }
    for (const s of this.stocks) {
      const hit = s.name.toLowerCase().includes(kw) || s.symbol.toLowerCase().includes(kw);
      if (hit && !out.includes(s)) {
        out.push(s);
        if (out.length >= limit) break;
      }
    }
    return out;
  }

  /** symbol（AAPL / aapl）→ 名称 */
  nameOf(symbol: string): string {
    const s = symbol.trim().toUpperCase();
    return this.stocks.find((x) => x.symbol === s)?.name || '';
  }
}

export const usStockListService = new UsStockListService();
