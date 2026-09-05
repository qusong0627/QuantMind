import axios from 'axios';
import { StrategyTemplate } from '../../../data/qlibStrategyTemplates';
import { QLIB_STRATEGY_TEMPLATES } from '../../../data/qlibStrategyTemplates';
import { filterTemplatesByMarket } from '../../../data/qlibStrategyTemplates';
import { SERVICE_URLS } from '../../../config/services';
import { authService } from '../../auth/services/authService';

const CACHE_KEY = 'quantmind_strategy_templates_cache';
const CACHE_TTL_MS = 60 * 1000; // 60 秒，与后端 TTL 对齐

const normalizeServiceBaseUrl = (url: string) => url.replace(/\/+$/, '').replace(/\/api\/v1$/, '');

interface TemplateCache {
  templates: StrategyTemplate[];
  fetchedAt: number;
}

/** 按市场隔离缓存：不同市场的模板列表不可串用 */
function cacheKeyFor(market?: string): string {
  const mkt = String(market || '').trim().toUpperCase();
  return mkt ? `${CACHE_KEY}:${mkt}` : CACHE_KEY;
}

function readCache(key: string): TemplateCache | null {
  try {
    const raw = sessionStorage.getItem(key);
    if (!raw) return null;
    const parsed: TemplateCache = JSON.parse(raw);
    if (Date.now() - parsed.fetchedAt > CACHE_TTL_MS) return null;
    return parsed;
  } catch {
    return null;
  }
}

function writeCache(key: string, templates: StrategyTemplate[]): void {
  try {
    const cache: TemplateCache = { templates, fetchedAt: Date.now() };
    sessionStorage.setItem(key, JSON.stringify(cache));
  } catch {
    // sessionStorage 不可用时静默忽略
  }
}

function clearCache(key: string): void {
  try {
    sessionStorage.removeItem(key);
  } catch {
    // ignore
  }
}

class StrategyTemplateService {
  private readonly baseURL = normalizeServiceBaseUrl(
    (import.meta as any).env?.VITE_API_GATEWAY_URL || SERVICE_URLS.API_GATEWAY
  );

  /**
   * 从后端获取预置策略模板（优先 sessionStorage 缓存，缓存按市场隔离）。
   * 可传 market（CN/HK/US/CRYPTO）只取该市场模板；
   * 后端不可用时降级返回本地 fallback 列表（同样按市场过滤）。
   */
  async getTemplates(market?: string): Promise<StrategyTemplate[]> {
    const key = cacheKeyFor(market);
    // 1. 命中缓存直接返回
    const cached = readCache(key);
    if (cached) return cached.templates;

    // 2. 从后端拉取
    try {
      const templates = await this._fetchFromServer(market);
      if (templates.length > 0) {
        writeCache(key, templates);
        return templates;
      }
    } catch (error: any) {
      console.warn('后端策略模板加载失败，使用本地缓存', error);
    }

    // 3. 降级到 fallback（按市场过滤：历史静态列表仅适用于 A 股视图）
    return filterTemplatesByMarket(QLIB_STRATEGY_TEMPLATES, market);
  }

  /**
   * 强制从服务器刷新模板（清除对应市场的缓存）。
   */
  async refresh(market?: string): Promise<StrategyTemplate[]> {
    clearCache(cacheKeyFor(market));
    return this.getTemplates(market);
  }

  private async _fetchFromServer(market?: string): Promise<StrategyTemplate[]> {
    const token =
      authService.getAccessToken() ||
      localStorage.getItem('access_token') ||
      localStorage.getItem('auth_token') ||
      localStorage.getItem('token');
    const tenantId = authService.getTenantId?.() || localStorage.getItem('tenant_id') || 'default';
    const marketQuery = String(market || '').trim()
      ? `?market=${encodeURIComponent(String(market).trim())}`
      : '';

    const response = await axios.get(
      `${this.baseURL}/api/v1/strategies/templates${marketQuery}`,
      {
        timeout: 15000,
        headers: {
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
          'X-Tenant-Id': tenantId,
        },
      }
    );

    if (response.data && Array.isArray(response.data.templates)) {
      // 后端返回后再做一次本市场防御过滤（无标记的历史模板仅留在 A 股视图）
      return filterTemplatesByMarket(response.data.templates as StrategyTemplate[], market);
    }
    return [];
  }
}

export const strategyTemplateService = new StrategyTemplateService();
