import axios, { AxiosInstance } from 'axios';
import { authService } from '../../auth/services/authService';
import { SERVICE_ENDPOINTS, resolveWebSafeServiceBase } from '../../../config/services';

/* ---- Types ---- */

export interface FieldInfo {
    field: string;
    tier: string;
    primary: string;
    fallbacks: string[];
    consensus: boolean;
    cleanup: boolean;
}

export interface KlineItem {
    date: string;
    open: number;
    high: number;
    low: number;
    close: number;
    volume: number;
    amount?: number;
}

export interface KlineResponse {
    market: string;
    symbol: string;
    period: string;
    source_used: string;
    items: KlineItem[];
    fallbacks_tried: string[];
}

export interface FieldDataResponse {
    market: string;
    field: string;
    symbol: string;
    source_used: string;
    count: number;
    columns: string[];
    data: Record<string, any>[];
}

export interface SearchResult {
    symbol: string;
    code: string;
    name: string;
    market: string;
}

export interface RealtimeQuote {
    symbol: string;
    last?: number;
    open?: number;
    high?: number;
    low?: number;
    pre_close?: number;
    volume?: number;
    amount?: number;
    source: string;
    [key: string]: any;
}

class DataDashboardService {
    private client: AxiosInstance;
    private baseURL = resolveWebSafeServiceBase(
        (import.meta as any).env?.VITE_USER_API_URL,
        SERVICE_ENDPOINTS.USER_SERVICE,
    );

    constructor() {
        this.client = axios.create({
            baseURL: this.baseURL,
            timeout: 60000,
            headers: { 'Content-Type': 'application/json' },
        });

        this.client.interceptors.request.use((config) => {
            const token = authService.getAccessToken();
            if (token && config.headers) {
                (config.headers as any).Authorization = `Bearer ${token}`;
            }
            let tenantId = 'default';
            try {
                const raw = localStorage.getItem('user');
                if (raw) {
                    const u = JSON.parse(raw);
                    if (u?.tenant_id) tenantId = String(u.tenant_id).trim();
                }
            } catch {}
            if (config.headers) {
                (config.headers as any)['X-Tenant-Id'] = tenantId;
            }
            return config;
        });

        this.client.interceptors.response.use(
            (r) => r,
            async (e) => authService.handle401Error(e, this.client),
        );
    }

    private unwrap<T>(resp: any): T {
        const d = resp?.data;
        if (d && d.success !== undefined && d.data !== undefined) return d.data as T;
        if (d && d.success !== undefined) return d as T;
        return d as T;
    }

    /** 按市场列出所有可用字段 */
    async getFields(market: string): Promise<FieldInfo[]> {
        const resp = await this.client.get('/data-dashboard/fields', {
            params: { market },
        });
        const d = this.unwrap<{ fields: FieldInfo[] }>(resp);
        return d?.fields || [];
    }

    /** 获取日K线数据（复用已有的 /market/kline 端点） */
    async getKline(
        market: string,
        symbol: string,
        days = 120,
        start?: string,
        end?: string,
    ): Promise<KlineResponse> {
        const params: Record<string, any> = { market, symbol, period: 'daily' };
        if (start) params.start = start;
        if (end) params.end = end;
        if (!start && !end) params.days = days;
        const resp = await this.client.get('/market/kline', { params });
        return this.unwrap<KlineResponse>(resp);
    }

    /** 获取任意字段数据 */
    async getFieldData(
        market: string,
        field: string,
        symbol: string,
        days = 365,
    ): Promise<FieldDataResponse> {
        const resp = await this.client.get('/data-dashboard/field-data', {
            params: { market, field, symbol, days },
            timeout: 120000,
        });
        return this.unwrap<FieldDataResponse>(resp);
    }

    /** 股票搜索 */
    async search(
        keyword: string,
        market?: string,
        limit = 20,
    ): Promise<SearchResult[]> {
        const params: Record<string, any> = { keyword, limit };
        if (market) params.market = market;
        const resp = await this.client.get('/data-dashboard/search', { params });
        const d = this.unwrap<{ results: SearchResult[] }>(resp);
        return d?.results || [];
    }

    /** 实时行情 */
    async getRealtime(
        market: string,
        symbol: string,
    ): Promise<RealtimeQuote | null> {
        try {
            const resp = await this.client.get('/data-dashboard/realtime', {
                params: { market, symbol },
            });
            const d = this.unwrap<{ quote: RealtimeQuote }>(resp);
            return d?.quote || null;
        } catch {
            return null;
        }
    }

    /** 行业板块 */
    async getSectors(market: string, symbol: string): Promise<Record<string, any>[]> {
        try {
            const resp = await this.client.get('/data-dashboard/sectors', {
                params: { market, symbol },
            });
            const d = this.unwrap<{ data: Record<string, any>[] }>(resp);
            return d?.data || [];
        } catch {
            return [];
        }
    }

    /** 股票基本信息 (F10) */
    async getMeta(
        market: string,
        symbol: string,
    ): Promise<Record<string, any>[]> {
        try {
            const resp = await this.client.get('/data-dashboard/meta', {
                params: { market, symbol },
            });
            const d = this.unwrap<{ data: Record<string, any>[] }>(resp);
            return d?.data || [];
        } catch {
            return [];
        }
    }
}

export const dataDashboardService = new DataDashboardService();
