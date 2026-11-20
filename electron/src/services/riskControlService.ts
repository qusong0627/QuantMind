/**
 * 模拟盘风险控制服务（用户级）
 *
 * 对应后端 `backend/services/api/routers/risk_rules.py`：
 * 普通登录用户管理本人模拟盘风控规则，无需管理员权限。
 * 后端强制 applies_to_all=false、user_ids=[本人]、trading_mode=SIMULATION。
 */

import { APIClient, createAPIClient, DEFAULT_API_CONFIG, APIClientConfig } from './api-client';
import { API_ENDPOINTS } from './config';
import { SERVICE_URLS } from '../config/services';
import type {
    RiskRuleAdmin,
    RiskRuleUpsertRequest,
    RiskEventAdmin,
    RiskDryRunItem,
} from '../features/admin/types';

interface Envelope<T> {
    success: boolean;
    code?: number;
    message?: string;
    data: T;
}

function unwrap<T>(res: Envelope<T> | T): T {
    if (res && typeof res === 'object' && 'success' in (res as Record<string, unknown>)) {
        const env = res as Envelope<T>;
        if (env.success) return env.data;
        throw new Error(env.message || '风控接口请求失败');
    }
    return res as T;
}

class RiskControlService {
    private client: APIClient;

    constructor() {
        this.client = createAPIClient({
            ...DEFAULT_API_CONFIG,
            baseURL: SERVICE_URLS.API_GATEWAY,
            onUnauthorized: () => {
                console.warn('RiskControl Service: 未授权，请重新登录');
            },
        } as APIClientConfig);
    }

    async listRules(activeOnly = false): Promise<RiskRuleAdmin[]> {
        const res = await this.client.get<Envelope<RiskRuleAdmin[]> | RiskRuleAdmin[]>(
            API_ENDPOINTS.RISK_RULES,
            { active_only: activeOnly },
        );
        return unwrap(res) || [];
    }

    async createRule(payload: RiskRuleUpsertRequest): Promise<RiskRuleAdmin> {
        const res = await this.client.post<Envelope<RiskRuleAdmin>>(
            API_ENDPOINTS.RISK_RULES,
            payload as unknown as Record<string, unknown>,
        );
        return unwrap(res);
    }

    async updateRule(ruleId: number, payload: Partial<RiskRuleUpsertRequest>): Promise<RiskRuleAdmin> {
        const res = await this.client.patch<Envelope<RiskRuleAdmin>>(
            API_ENDPOINTS.RISK_RULE_DETAIL(ruleId),
            payload as unknown as Record<string, unknown>
        );
        return unwrap(res);
    }

    async deleteRule(ruleId: number): Promise<void> {
        await this.client.delete(API_ENDPOINTS.RISK_RULE_DETAIL(ruleId));
    }

    async listEvents(params?: { rule_type?: string; status?: string; limit?: number }): Promise<RiskEventAdmin[]> {
        const res = await this.client.get<Envelope<RiskEventAdmin[]> | RiskEventAdmin[]>(
            API_ENDPOINTS.RISK_EVENTS,
            { limit: 50, ...(params || {}) },
        );
        return unwrap(res) || [];
    }

    async dryRunRule(ruleId: number, payload?: { tenant_id?: string; market?: string }): Promise<RiskDryRunItem[]> {
        const res = await this.client.post<Envelope<RiskDryRunItem[]>>(
            API_ENDPOINTS.RISK_RULE_DRY_RUN(ruleId),
            { tenant_id: 'default', market: 'CN', ...(payload || {}) },
        );
        return unwrap(res) || [];
    }
}

export const riskControlService = new RiskControlService();
