/** 港股证券列表服务：从后端 quanthk security_master 加载全市场名称（2807 只），内存搜索 */

import { SERVICE_ENDPOINTS } from '../config/services';

export interface HkStock {
  symbol: string;   // 0700.HK
  name: string;     // 腾讯控股
}

class HkStockListService {
  private stocks: HkStock[] = [];
  private loadedFlag = false;
  private loadingPromise: Promise<void> | null = null;

  async load(): Promise<void> {
    if (this.loadedFlag) return;
    if (this.loadingPromise) return this.loadingPromise;
    this.loadingPromise = (async () => {
      try {
        const token = localStorage.getItem('access_token') || '';
        const resp = await fetch(`${SERVICE_ENDPOINTS.USER_SERVICE}/research/stock-names?market=HK`, {
          headers: token ? { Authorization: `Bearer ${token}` } : {},
        });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json() as { names: Record<string, string> };
        this.stocks = Object.entries(data.names || {}).map(([symbol, name]) => ({ symbol, name }));
        this.stocks.sort((a, b) => a.symbol.localeCompare(b.symbol));
        this.loadedFlag = true;
      } catch (e) {
        console.warn('[HkStockList] 港股名称加载失败:', e);
        throw e;
      } finally {
        this.loadingPromise = null;
      }
    })();
    return this.loadingPromise;
  }

  isLoaded(): boolean { return this.loadedFlag; }

  /** 代码或中文名模糊搜索（最多 limit 条） */
  search(keyword: string, limit = 10): HkStock[] {
    const kw = keyword.trim().toLowerCase();
    if (!kw) return [];
    const digits = kw.replace(/\D/g, '');
    const out: HkStock[] = [];
    for (const s of this.stocks) {
      const codePart = s.symbol.replace('.HK', '');
      const hit = s.name.toLowerCase().includes(kw)
        || s.symbol.toLowerCase().includes(kw)
        || (digits.length >= 2 && codePart.includes(digits));
      if (hit) {
        out.push(s);
        if (out.length >= limit) break;
      }
    }
    return out;
  }

  /** symbol（0700.HK / 0700）→ 中文名 */
  nameOf(symbol: string): string {
    const s = symbol.toUpperCase().replace(/\.HK$/, '');
    const hit = this.stocks.find((x) => x.symbol.toUpperCase().replace('.HK', '') === s);
    return hit?.name || '';
  }
}

export const hkStockListService = new HkStockListService();
