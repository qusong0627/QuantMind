import axios from 'axios';
import { StrategyTemplate } from '../../../data/qlibStrategyTemplates';
import { QLIB_STRATEGY_TEMPLATES } from '../../../data/qlibStrategyTemplates';
import { SERVICE_URLS, resolveWebSafeServiceBase } from '../../../config/services';
import { authService } from '../../auth/services/authService';

const CACHE_KEY = 'quantmind_strategy_templates_cache';
const CACHE_TTL_MS = 60 * 1000; // 60 秒，与后端 TTL 对齐

const normalizeServiceBaseUrl = (url: string) => url.replace(/\/+$/, '').replace(/\/api\/v1$/, '');

interface TemplateCache {
  templates: StrategyTemplate[];
  fetchedAt: number;
}

function readCache(): TemplateCache | null {
  try {
    const raw = sessionStorage.getItem(CACHE_KEY);
    if (!raw) return null;
    const parsed: TemplateCache = JSON.parse(raw);
    if (Date.now() - parsed.fetchedAt > CACHE_TTL_MS) return null;
    return parsed;
  } catch {
    return null;
  }
}

function writeCache(templates: StrategyTemplate[]): void {
  try {
    const cache: TemplateCache = { templates, fetchedAt: Date.now() };
    sessionStorage.setItem(CACHE_KEY, JSON.stringify(cache));
  } catch {
    // sessionStorage 不可用时静默忽略
  }
}

function clearCache(): void {
  try {
    sessionStorage.removeItem(CACHE_KEY);
  } catch {
    // ignore
  }
}

class StrategyTemplateService {
  private readonly baseURL = normalizeServiceBaseUrl(
    resolveWebSafeServiceBase(
      (import.meta as any).env?.VITE_API_GATEWAY_URL,
      SERVICE_URLS.API_GATEWAY || '/api/v1',
    )
  );

  /**
   * 从后端获取所有预置策略模板（优先 sessionStorage 缓存）。
   * 后端不可用时降级返回本地 fallback 列表。
   */
  async getTemplates(): Promise<StrategyTemplate[]> {
    // 1. 命中缓存直接返回
    const cached = readCache();
    if (cached) return cached.templates;

    // 2. 从后端拉取
    try {
      const templates = await this._fetchFromServer();
      if (templates.length > 0) {
        writeCache(templates);
        return templates;
      }
    } catch (error: any) {
      console.warn('后端策略模板加载失败，使用本地缓存', error);
    }

    // 3. 降级到 fallback
    return QLIB_STRATEGY_TEMPLATES;
  }

  /**
   * 强制从服务器刷新模板（清除缓存）。
   */
  async refresh(): Promise<StrategyTemplate[]> {
    clearCache();
    return this.getTemplates();
  }

  private async _fetchFromServer(): Promise<StrategyTemplate[]> {
    const token =
      authService.getAccessToken() ||
      localStorage.getItem('access_token') ||
      localStorage.getItem('auth_token') ||
      localStorage.getItem('token');
    const tenantId = authService.getTenantId?.() || localStorage.getItem('tenant_id') || 'default';

    const response = await axios.get(`${this.baseURL}/api/v1/strategies/templates`, {
      timeout: 15000,
      headers: {
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        'X-Tenant-Id': tenantId,
      },
    });

    if (response.data && Array.isArray(response.data.templates)) {
      return response.data.templates as StrategyTemplate[];
    }
    return [];
  }
}

export const strategyTemplateService = new StrategyTemplateService();
