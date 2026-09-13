import axios, { AxiosInstance } from 'axios';
import { ApiResponse } from '../../auth/types/auth.types';
import {
    DashboardMetrics,
    SystemLoadSummary,
    AdminUser,
    AIModel,
    ModelScanResult,
    ModelDirectoryInfo,
    InferencePrecheckResult,
    AdminModelFeatureCatalog,
    AdminPredictionListResult,
    AdminPredictionDetailResult,
    AdminDataStatusResult,
    StrategyTemplateAdmin,
    StrategyTemplateUpsertRequest,
} from '../types';
import { authService } from '../../auth/services/authService';
import { SERVICE_ENDPOINTS, resolveWebSafeServiceBase } from '../../../config/services';

class AdminService {
    private axiosInstance: AxiosInstance;
    private readonly baseURL = resolveWebSafeServiceBase(
        (import.meta as any).env?.VITE_USER_API_URL,
        SERVICE_ENDPOINTS.USER_SERVICE,
    );
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
                // 403 权限不足：提示重新登录以刷新 token
                if (error?.response?.status === 403) {
                    const detail = error?.response?.data?.detail || '';
                    if (detail.includes('admin') || detail.includes('权限')) {
                        // 标记需要重新登录
                        error._adminReauthHint = '管理员权限验证失败，请退出并重新登录以刷新权限令牌';
                    }
                }
                // 只有 401 才属于令牌刷新流程。将普通业务错误交给调用方，
                // 避免 404/校验错误被错误标记为 Auth Error。
                if (error?.response?.status === 401) {
                    return authService.handle401Error(error, this.axiosInstance);
                }
                return Promise.reject(error);
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

    /** 快速获取系统真实物理负载与服务健康概要（供侧边栏轻量轮询） */
    async getSystemLoad(): Promise<SystemLoadSummary> {
        const resp = await this.axiosInstance.get<ApiResponse<SystemLoadSummary>>(
            '/admin/dashboard/system-load',
            { _skipAuthRefresh: true } as any
        );
        return this.unwrap(resp.data);
    }

    /**
     * 节点性能历史序列（ts, cpu, mem, disk, %），供控制台「节点性能历史」面积图。
     */
    async getNodeHistory(limit = 180): Promise<Array<{ ts: number; cpu: number; mem: number; disk: number }>> {
        type NodePerfPoint = { ts: number; cpu: number; mem: number; disk: number };
        const resp = await this.axiosInstance.get<ApiResponse<NodePerfPoint[]>>(
            '/admin/dashboard/node-history',
            { params: { limit } }
        );
        const payload = this.unwrap<{ series: NodePerfPoint[] } | null>(resp.data);
        return payload?.series ?? [];
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

    async scanModels(refresh = false): Promise<ModelScanResult> {
        const resp = await this.axiosInstance.get<ModelScanResult>('/admin/models/scan', {
            params: { refresh },
            timeout: 30000,
            _skipAuthRefresh: true,
        } as any);
        return resp.data;
    }

    async getModelFeatureCatalog(market?: string): Promise<AdminModelFeatureCatalog> {
        const resp = await this.axiosInstance.get<AdminModelFeatureCatalog>('/admin/models/feature-catalog', {
            params: market ? { market } : {},
        });
        return resp.data;
    }

    async updateFeatureCatalog(catalog: AdminModelFeatureCatalog): Promise<{ status: string; feature_count: number }> {
        const resp = await this.axiosInstance.put('/admin/models/feature-catalog', catalog);
        return resp.data;
    }

    // Versioned direct QuantDB training-factor catalog (admin only).
    async getQuantDBFactorSources(market = 'CN'): Promise<any> {
        const resp = await this.axiosInstance.get('/admin/training-data/sources', {
            params: { market },
        });
        return resp.data;
    }

    async getMarketSourcesStatus(): Promise<any> {
        const resp = await this.axiosInstance.get('/admin/data-platform/market-sources-status');
        return resp.data;
    }

    async refreshQuantDBFactorSources(market = 'CN'): Promise<any> {
        const resp = await this.axiosInstance.post(
            '/admin/training-data/sources/refresh',
            undefined,
            { params: { market }, timeout: 120_000 },
        );
        return resp.data;
    }

    async getQuantDBFactorFields(sourceDataset: string, market = 'CN'): Promise<any> {
        const resp = await this.axiosInstance.get('/admin/training-data/fields', {
            params: { source_dataset: sourceDataset, market },
        });
        return resp.data;
    }

    async getQuantDBFactorCatalog(sourceDataset: string, versionId?: string, market = 'CN'): Promise<any | null> {
        const resp = await this.axiosInstance.get('/admin/training-data/catalog', {
            params: { source_dataset: sourceDataset, version_id: versionId, market },
        });
        // 未发布目录是正常的初始状态，后端以 catalog: null 表示。
        return resp.data?.catalog === null ? null : resp.data;
    }

    async createQuantDBFactorDraft(versionName: string, sourceDataset: string, market = 'CN'): Promise<any> {
        const resp = await this.axiosInstance.post('/admin/training-data/versions', {
            version_name: versionName,
            source_dataset: sourceDataset,
        }, { params: { market } });
        return resp.data;
    }

    async seedQuantDBFactorDraft(versionId: string): Promise<any> {
        const resp = await this.axiosInstance.post(`/admin/training-data/versions/${versionId}/seed`);
        return resp.data;
    }

    async saveQuantDBFactorMapping(versionId: string, mapping: any): Promise<any> {
        const resp = await this.axiosInstance.put(`/admin/training-data/versions/${versionId}/mappings`, { mapping });
        return resp.data;
    }

    async publishQuantDBFactorDraft(versionId: string): Promise<any> {
        const resp = await this.axiosInstance.post(`/admin/training-data/versions/${versionId}/publish`);
        return resp.data;
    }

    async cloneQuantDBFactorCatalog(versionId: string, versionName: string): Promise<any> {
        const resp = await this.axiosInstance.post(`/admin/training-data/versions/${versionId}/clone`, { version_name: versionName });
        return resp.data;
    }

    async getDataStatus(refresh = false, market = 'a_share'): Promise<AdminDataStatusResult> {
        const resp = await this.axiosInstance.get<AdminDataStatusResult>('/admin/models/data-status', {
            params: { refresh, market },
            timeout: 120000, // 增加超时到 2 分钟，确保扫描大目录不超时
        });
        return resp.data;
    }

    async triggerDailySync(params?: {
        market?: string;
        symbols?: string[];
        calibrate?: boolean;
    }): Promise<any> {
        const resp = await this.axiosInstance.post('/admin/data-platform/daily-sync', {
            market: params?.market || 'A',
            symbols: params?.symbols || [],
            calibrate: params?.calibrate ?? true,
        }, { timeout: 30000 });
        return resp.data;
    }

    async getDailySyncTaskStatus(taskId: string): Promise<any> {
        const resp = await this.axiosInstance.get(`/admin/data-platform/daily-sync/status/${taskId}`, {
            timeout: 15000,
        });
        return resp.data;
    }

    async getSyncStatus(): Promise<any> {
        const resp = await this.axiosInstance.get('/admin/data-platform/sync-status', {
            timeout: 30000,
        });
        return resp.data;
    }

    async getSyncProgress(): Promise<any> {
        const resp = await this.axiosInstance.get('/admin/data-platform/sync-progress', {
            timeout: 5000,
        });
        return resp.data;
    }

    async getAlphaAgentMarkets(): Promise<any> {
        const resp = await this.axiosInstance.get('/admin/data-platform/alpha-agent-markets', {
            timeout: 30000,
        });
        return resp.data;
    }

    async syncAlphaAgentMarket(market: string): Promise<any> {
        const resp = await this.axiosInstance.post(`/admin/data-platform/sync-alpha-agent-market`, null, {
            params: { market },
            timeout: 600000,
        });
        return resp.data;
    }

    async updateInvestmentData(version?: string): Promise<any> {
        const resp = await this.axiosInstance.post('/admin/data-platform/update-investment-data', null, {
            params: { version: version || '' },
            timeout: 600000,
        });
        return resp.data;
    }

    async updateFeatureParquet(rebuild = false): Promise<any> {
        const resp = await this.axiosInstance.post('/admin/models/update-feature-parquet', null, {
            params: { rebuild },
            timeout: 600000,
        });
        return resp.data;
    }

    async updateMarketFeatures(market: string, rebuild = false): Promise<any> {
        const resp = await this.axiosInstance.post('/admin/models/update-market-features', null, {
            params: { market, rebuild },
            timeout: 600000,
        });
        return resp.data;
    }

    async syncFundamentals(market = 'ALL', dryRun = false): Promise<any> {
        const resp = await this.axiosInstance.post('/admin/data-platform/sync-fundamentals', {
            market,
            dry_run: dryRun,
        }, {
            timeout: 600000,
        });
        return resp.data;
    }

    async getSyncSchedule(market: string): Promise<any> {
        const resp = await this.axiosInstance.get(`/admin/data-platform/sync-schedule/${market}`);
        return resp.data;
    }

    async saveSyncSchedule(market: string, cfg: {
        enabled: boolean;
        time: string;
        days: number;
        datasets: string[];
        with_qlib?: boolean;
    }): Promise<any> {
        const resp = await this.axiosInstance.post(`/admin/data-platform/sync-schedule/${market}`, cfg);
        return resp.data;
    }

    async runSyncScheduleNow(market: string): Promise<any> {
        const resp = await this.axiosInstance.post(`/admin/data-platform/sync-schedule/${market}/run`);
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

    public async listTrainingNodes(includeStatus: boolean = false): Promise<any> {
        const resp = await this.axiosInstance.get<any>('/admin/models/training-nodes', {
            params: { include_status: includeStatus },
        });
        return resp.data;
    }

    public async testTrainingNode(nodeId: string): Promise<any> {
        const resp = await this.axiosInstance.post<any>('/admin/models/training-nodes/test', { node_id: nodeId });
        return resp.data;
    }

    public async getTrainingNodeStatus(nodeId: string): Promise<any> {
        const resp = await this.axiosInstance.get<any>(`/admin/models/training-nodes/${nodeId}/status`);
        return resp.data;
    }

    public async saveTrainingNode(node: any): Promise<any> {
        const resp = await this.axiosInstance.post<any>('/admin/models/training-nodes/config', node);
        return resp.data;
    }

    public async deleteTrainingNode(nodeId: string): Promise<any> {
        const resp = await this.axiosInstance.delete<any>(`/admin/models/training-nodes/${nodeId}`);
        return resp.data;
    }

    public async getTrainingNodeDetail(nodeId: string): Promise<any> {
        const resp = await this.axiosInstance.get<any>(`/admin/models/training-nodes/${nodeId}/detail`);
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

    /**
     * 一键更新系统：触发宿主机 deploy/update.sh（git pull + 重建 + 重启服务）。
     * 需要后端开启 QUANTMIND_ENABLE_WEB_UPDATE 并挂载 docker socket，否则后端返回 403。
     */
    public async updateSystem(): Promise<{ started: boolean; task_id?: string }> {
        const resp = await this.axiosInstance.post('/admin/system/update', {}, { params: { confirm: 1 } });
        return this.unwrap(resp.data);
    }

    /**
     * 查询系统更新任务状态（读取 update.log）。
     */
    public async getUpdateStatus(): Promise<{
        state: 'idle' | 'running' | 'done' | 'failed';
        message?: string;
        log_tail?: string;
    }> {
        const resp = await this.axiosInstance.get('/admin/system/update/status', { timeout: 15000 });
        return this.unwrap(resp.data);
    }

    // FinBERT 开关（独立控制按键）
    async getFinbertStatus(): Promise<{
        enabled: boolean;
        device: number;
        model: string;
        installed?: boolean;
        framework_ok?: boolean;
        ready_for_use?: boolean;
        env_enabled?: boolean;
        model_ready: boolean;
        model_failed: boolean;
        override?: boolean | null;
        toggle_path?: string;
    }> {
        const resp = await this.axiosInstance.get('/admin/finbert/status');
        return resp.data?.data ?? resp.data;
    }
    async setFinbertEnabled(enabled: boolean): Promise<{ enabled: boolean; model_ready: boolean }> {
        const resp = await this.axiosInstance.post('/admin/finbert/toggle', { enabled });
        return resp.data?.data ?? resp.data;
    }
}

export const adminService = new AdminService();
