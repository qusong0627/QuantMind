import axios, { AxiosInstance } from 'axios';
import { ApiResponse } from '../../auth/types/auth.types';
import {
    DashboardMetrics,
    AdminUser,
    CommunityPost,
    AIModel,
    ModelScanResult,
    ModelDirectoryInfo,
    InferencePrecheckResult,
    AdminModelFeatureCatalog,
    AdminPredictionListResult,
    AdminPredictionDetailResult,
    AdminDataStatusResult,
    AdminMarketDataSyncResult,
    AdminOfficialDataUpdateSyncResult,
    StrategyTemplateAdmin,
    StrategyTemplateUpsertRequest,
} from '../types';
import { authService } from '../../auth/services/authService';
import { SERVICE_ENDPOINTS } from '../../../config/services';

class AdminService {
    private axiosInstance: AxiosInstance;
    private readonly baseURL = (import.meta as any).env?.VITE_USER_API_URL || SERVICE_ENDPOINTS.USER_SERVICE;
    private metrics401Locked = false;

    constructor() {
        this.axiosInstance = axios.create({
            baseURL: this.baseURL,
            timeout: 30000,
            headers: {
                'Content-Type': 'application/json',
            },
        });

        this.axiosInstance.interceptors.request.use((config) => {
            const token = authService.getAccessToken();
            if (token) {
                if (config.headers && typeof config.headers.set === 'function') {
                    config.headers.set('Authorization', `Bearer ${token}`);
                } else if (config.headers) {
                    config.headers.Authorization = `Bearer ${token}`;
                }
            }

            // 多租户：默认携带 tenant_id
            let tenantId = 'default';
            try {
                const raw = localStorage.getItem('user');
                if (raw) {
                    const u = JSON.parse(raw);
                    if (u?.tenant_id) {
                        tenantId = String(u.tenant_id).trim();
                    }
                }
            } catch (e) { }

            if (config.headers && typeof config.headers.set === 'function') {
                if (!config.headers.has('X-Tenant-Id') && !config.headers.has('x-tenant-id')) {
                    config.headers.set('X-Tenant-Id', tenantId);
                }
            } else if (config.headers) {
                if (!config.headers['X-Tenant-Id'] && !config.headers['x-tenant-id']) {
                    config.headers['X-Tenant-Id'] = tenantId;
                }
            }

            return config;
        });

        this.axiosInstance.interceptors.response.use(
            (response) => response,
            async (error) => {
                return authService.handle401Error(error, this.axiosInstance);
            }
        );
    }

    // Dashboard
    async getMetrics(): Promise<DashboardMetrics> {
        if (this.metrics401Locked) {
            throw new Error('ADMIN_METRICS_UNAUTHORIZED_LOCKED');
        }
        const resp = await this.axiosInstance.get<ApiResponse<DashboardMetrics>>(
            '/admin/dashboard/metrics',
            { _skipAuthRefresh: true } as any
        );
        return this.unwrap(resp.data);
    }

    markMetricsUnauthorized(): void {
        this.metrics401Locked = true;
    }

    clearMetricsUnauthorized(): void {
        this.metrics401Locked = false;
    }

    // Users
    async listUsers(query?: string, page = 1, pageSize = 20): Promise<AdminUser[]> {
        const resp = await this.axiosInstance.get<any>('/admin/users/', {
            params: { query, page, page_size: pageSize }
        });
        // 后端返回结构比较特殊，见 users.py
        if (resp.data.success && Array.isArray(resp.data.data)) {
            return resp.data.data;
        }
        throw new Error('获取用户列表失败');
    }

    async toggleUserStatus(userId: string): Promise<boolean> {
        const resp = await this.axiosInstance.post<any>(`/admin/users/${userId}/toggle-status`);
        return resp.data.code === 200;
    }

    // Community
    async listPosts(page = 1, pageSize = 20): Promise<{ items: CommunityPost[], total: number }> {
        const resp = await this.axiosInstance.get<ApiResponse<any>>('/admin/community/posts', {
            params: { page, page_size: pageSize }
        });
        const data = this.unwrap(resp.data) as any;
        return {
            items: data.items,
            total: data.pagination.total
        };
    }

    async moderatePost(postId: number, update: { pinned?: boolean, featured?: boolean }): Promise<void> {
        const resp = await this.axiosInstance.patch<ApiResponse>(`/admin/community/posts/${postId}`, update);
        this.unwrap(resp.data);
    }

    async deletePost(postId: number): Promise<void> {
        const resp = await this.axiosInstance.delete<ApiResponse>(`/admin/community/posts/${postId}`);
        this.unwrap(resp.data);
    }

    // Model Management
    async listModels(): Promise<AIModel[]> {
        const resp = await this.axiosInstance.get<AIModel[]>('/admin/models');
        return resp.data;
    }

    async updateModel(data: { name: string, description?: string, source_type: string, start_date?: string, end_date?: string }): Promise<AIModel> {
        const resp = await this.axiosInstance.post<AIModel>('/admin/models', data);
        return resp.data;
    }

    async deleteModel(modelId: number): Promise<void> {
        await this.axiosInstance.delete(`/admin/models/${modelId}`);
    }

    async runInference(modelFile = 'model.bin'): Promise<{
        success: boolean;
        message?: string;
        trade_date?: string;
        requested_inference_date?: string;
        calendar_adjusted?: boolean;
        data_trade_date?: string;
        prediction_trade_date?: string;
        run_id?: string;
        exit_code?: number;
        signals_count?: number;
        stdout?: string;
        stderr?: string;
        error?: string;
    }> {
        const resp = await this.axiosInstance.post<any>('/admin/models/run-inference', null, {
            params: { model_file: modelFile },
            timeout: 660000, // 11 分钟（略大于服务端 600s 超时）
        });
        return resp.data;
    }

    async precheckInference(): Promise<InferencePrecheckResult> {
        const resp = await this.axiosInstance.get<InferencePrecheckResult>('/admin/models/precheck-inference');
        return resp.data;
    }

    async scanModels(): Promise<ModelScanResult> {
        const resp = await this.axiosInstance.get<ModelScanResult>('/admin/models/scan');
        return resp.data;
    }

    async getModelFeatureCatalog(): Promise<AdminModelFeatureCatalog> {
        const resp = await this.axiosInstance.get<AdminModelFeatureCatalog>('/admin/models/feature-catalog');
        return resp.data;
    }

    async getDataStatus(refresh = false): Promise<AdminDataStatusResult> {
        const resp = await this.axiosInstance.get<AdminDataStatusResult>('/admin/models/data-status', {
            params: { refresh },
            timeout: 120000, // 增加超时到 2 分钟，确保扫描大目录不超时
        });
        return resp.data;
    }

    async syncMarketDataDaily(params?: {
        targetDate?: string;
        maxSymbols?: number;
        apply?: boolean;
    }): Promise<AdminMarketDataSyncResult> {
        const resp = await this.axiosInstance.post<AdminMarketDataSyncResult>(
            '/admin/models/sync-market-data-daily',
            null,
            {
                params: {
                    target_date: params?.targetDate,
                    max_symbols: params?.maxSymbols ?? 0,
                    apply: params?.apply ?? true,
                    background: true,
                },
                timeout: 3600000,
            },
        );
        return resp.data;
    }

    async syncOfficialDataUpdate(params: {
        apiBaseUrl: string;
        accessKey: string;
        secretKey: string;
        version?: string;
        dryRun?: boolean;
    }): Promise<AdminOfficialDataUpdateSyncResult> {
        const resp = await this.axiosInstance.post<AdminOfficialDataUpdateSyncResult>(
            '/admin/models/sync-official-data-update',
            {
                api_base_url: params.apiBaseUrl,
                access_key: params.accessKey,
                secret_key: params.secretKey,
                version: params.version?.trim() || null,
                dry_run: params.dryRun ?? false,
            },
            { timeout: 1800000 },
        );
        return resp.data;
    }

    async getModelDirectoryDetail(modelPath: string): Promise<ModelDirectoryInfo> {
        const resp = await this.axiosInstance.get<ModelDirectoryInfo>(`/admin/models/directory/${modelPath}`);
        return resp.data;
    }

    async listPredictionRuns(params?: {
        predictionDate?: string;
        tenantId?: string;
        userId?: string;
        runId?: string;
        modelVersion?: string;
        page?: number;
        pageSize?: number;
    }): Promise<AdminPredictionListResult> {
        const resp = await this.axiosInstance.get<AdminPredictionListResult>('/admin/models/predictions', {
            params: {
                prediction_date: params?.predictionDate,
                tenant_id: params?.tenantId,
                user_id: params?.userId,
                run_id: params?.runId,
                model_version: params?.modelVersion ?? 'inference_script',
                page: params?.page ?? 1,
                page_size: params?.pageSize ?? 20,
            },
        });
        return resp.data;
    }

    async getPredictionRunDetail(
        runId: string,
        params?: {
            predictionDate?: string;
            tenantId?: string;
            userId?: string;
            page?: number;
            pageSize?: number;
        },
    ): Promise<AdminPredictionDetailResult> {
        const resp = await this.axiosInstance.get<AdminPredictionDetailResult>(`/admin/models/predictions/${runId}`, {
            params: {
                prediction_date: params?.predictionDate,
                tenant_id: params?.tenantId,
                user_id: params?.userId,
                page: params?.page ?? 1,
                page_size: params?.pageSize ?? 200,
            },
        });
        return resp.data;
    }

    async downloadPredictionExport(
        runId: string,
        params?: {
            predictionDate?: string;
            tenantId?: string;
            userId?: string;
        },
    ): Promise<void> {
        const resp = await this.axiosInstance.get(`/admin/models/predictions/${runId}/export`, {
            params: {
                prediction_date: params?.predictionDate,
                tenant_id: params?.tenantId,
                user_id: params?.userId,
            },
            responseType: 'blob',
        });

        // Trigger browser download
        const url = window.URL.createObjectURL(new Blob([resp.data]));
        const link = document.createElement('a');
        link.href = url;
        const filename = `prediction_${runId}_${params?.predictionDate || 'export'}.csv`;
        link.setAttribute('download', filename);
        document.body.appendChild(link);
        link.click();
        link.remove();
        window.URL.revokeObjectURL(url);
    }

    // -----------------------------------------------------------------------
    // 策略模板管理
    // -----------------------------------------------------------------------

    async listStrategyTemplates(): Promise<{ total: number; templates: StrategyTemplateAdmin[] }> {
        const resp = await this.axiosInstance.get('/admin/strategy-templates');
        return resp.data;
    }

    async createStrategyTemplate(data: StrategyTemplateUpsertRequest): Promise<{ success: boolean; id: string; message: string }> {
        const resp = await this.axiosInstance.post('/admin/strategy-templates', data);
        return resp.data;
    }

    async updateStrategyTemplate(id: string, data: StrategyTemplateUpsertRequest): Promise<{ success: boolean; id: string; message: string }> {
        const resp = await this.axiosInstance.put(`/admin/strategy-templates/${id}`, data);
        return resp.data;
    }

    async deleteStrategyTemplate(id: string): Promise<{ success: boolean; id: string; message: string }> {
        const resp = await this.axiosInstance.delete(`/admin/strategy-templates/${id}`);
        return resp.data;
    }

    public async runCloudTraining(payload: any): Promise<{runId: string, status: string}> {
        const resp = await this.axiosInstance.post<{runId: string, status: string}>('/admin/models/run-training', payload);
        return resp.data;
    }

    public async getTrainingRun(runId: string): Promise<any> {
        const resp = await this.axiosInstance.get<any>(`/admin/models/training-runs/${runId}`);
        return resp.data;
    }

    public async listTrainingJobs(params?: {
        status?: string;
        tenant_id?: string;
        user_id?: string;
        page?: number;
        page_size?: number;
    }): Promise<{
        total: number;
        page: number;
        page_size: number;
        items: Array<{
            run_id: string;
            tenant_id: string;
            user_id: string;
            status: string;
            progress: number;
            instance_id: string | null;
            model_type: string;
            job_name: string;
            features_count: number;
            train_start: string;
            train_end: string;
            registered_model_id: string;
            has_logs: boolean;
            created_at: string;
            updated_at: string;
        }>;
    }> {
        const resp = await this.axiosInstance.get('/admin/models/training-jobs', { params });
        return resp.data;
    }

    private unwrap<T>(res: ApiResponse<T> | any): T {
        if (res.success || res.code === 200 || res.status === 'success') {
            return res.data;
        }
        throw new Error(res.message || 'API 请求失败');
    }
}

export const adminService = new AdminService();
