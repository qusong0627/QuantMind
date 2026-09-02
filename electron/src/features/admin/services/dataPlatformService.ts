import axios, { AxiosInstance } from 'axios';
import { authService } from '../../auth/services/authService';
import { SERVICE_ENDPOINTS } from '../../../config/services';

/** 健康矩阵单元格 */
export interface HealthCell {
    field: string;
    source: string;
    is_primary: boolean;
    registered: boolean;
    last_success_at?: string | null;
    last_error_at?: string | null;
    error_rate_1h: number;
    avg_latency_ms: number;
    fallback_triggered_count: number;
}

export interface HealthMatrix {
    market: string;
    fields: string[];
    /** 字段 → tier（如 T1/T2/T3），用于分组 */
    field_tiers?: Record<string, string>;
    sources: string[];
    cells: HealthCell[];
    timestamp: string;
}

export interface SourceSummary {
    name: string;
    class: string;
    markets: string[];
    field_count: number;
    covered_field_count: number;
    health_summary: {
        last_success_at?: string | null;
        last_error_at?: string | null;
        last_error_msg?: string | null;
        error_rate_1h?: number;
        avg_latency_ms?: number;
    };
}

export interface FieldCoverageRow {
    field: string;
    tier: string;
    primary: string;
    fallbacks: string[];
    /** 后端为 boolean（是否参与共识投票）；旧版可能给数组，做兼容。 */
    consensus: boolean | string[];
    cleanup?: string[];
}

export interface QualityAlert {
    id: number;
    alert_type: string;
    severity: string;
    market?: string | null;
    field?: string | null;
    source?: string | null;
    symbol?: string | null;
    trade_date?: string | null;
    message: string;
    details?: any;
    acknowledged: boolean;
    acknowledged_by?: string | null;
    acknowledged_at?: string | null;
    created_at: string;
}

export interface FreshnessItem {
    field: string;
    source: string;
    is_primary: boolean;
    last_success_at?: string | null;
    last_error_at?: string | null;
    days_stale: number | null;
    freshness: 'fresh' | 'stale' | 'outdated' | 'unknown';
    avg_latency_ms: number;
    error_rate_1h: number;
}

export interface OnlineStatusItem {
    name: string;
    class: string;
    markets: string[];
    fields: string[];
    status: 'online' | 'error' | 'unavailable' | 'unknown';
    latency_ms: number | null;
    error?: string | null;
    checked_at: string;
}

/** QuantDB 落盘形态：按交易日分区 / 每标的一文件 / 单文件 */
export type QuantDBLayout = 'partition' | 'symbol' | 'single';

export interface QuantDBConfig {
    api_key_configured: boolean;
    api_key_masked: string;
    data_dir: string;
    runtime_env_file: string;
    timestamp: string;
}

export interface QuantDBInfo {
    installed: boolean;
    api_key_configured: boolean;
    connected: boolean;
    version?: string;
    account?: { username: string; email: string };
    usage?: {
        used_gb: number;
        limit_gb: number;
        remaining_gb: number;
        credit_gb?: number;
        subscription?: { status: string };
    };
    used_bytes_human?: string;
    remaining_bytes_human?: string;
    used_traffic?: string | number;
    remaining_traffic?: string | number;
    traffic_reset_date?: string;
    tier_name?: string;
    error?: string;
}

export interface QuantDBGroup {
    id: string;
    name: string;
    category_id: string;
    dataset_count: number;
    synced_count: number;
    files: number;
    size_mb: number;
}

export interface QuantDBDataset {
    dataset: string;
    name: string;
    group: string;
    category_id: string;
    layout: QuantDBLayout;
    rel_dir: string;
    note: string;
    synced: boolean;
    files: number;
    size_mb: number;
    start_date?: string;
    end_date?: string;
    partitions?: number;
    updated_at?: string;
}

export interface QuantDBPreview {
    dataset: string;
    name?: string;
    source: 'local' | 'remote';
    file?: string | null;
    rows_total: number;
    column_count?: number;
    columns: Array<{ name: string; dtype: string }>;
    data: Array<Record<string, unknown>>;
    symbol_total?: number;
    symbol_choices?: string[];
    symbol_names?: Record<string, string>;
    timestamp: string;
}

export interface QuantDBSyncJob {
    job_id: string;
    status: 'running' | 'completed' | 'failed' | 'cancelled' | 'cancelling';
    stage: string;
    datasets: string[];
    total: number;
    done: number;
    current?: string | null;
    results: Array<{
        dataset: string;
        status: 'synced' | 'up_to_date' | 'failed';
        downloaded: number;
        layout?: string;
        error?: string;
    }>;
    with_pg: boolean;
    with_qlib: boolean;
    cancel_requested?: boolean;
    summary?: any;
    pg_fill?: { status: string; rows?: number; reason?: string };
    qlib_cache?: { status: string; provider_uri?: string; reason?: string };
    error?: string;
    started_at: string;
    finished_at?: string;
    started_by?: string;
}

export interface QuantDBLocalScanPreflight {
    root: string;
    exists: boolean;
    same_root: boolean;
    datasets: Array<{
        dataset: string;
        name: string;
        group: string;
        layout: string;
        rel_dir: string;
        files: number;
        bytes: number;
    }>;
    total_files: number;
    total_bytes: number;
    state: {
        quantmind_path: string;
        quantmind_objects: number;
        sdk_path: string;
        sdk_objects: number;
    };
    warnings: string[];
    timestamp: string;
}

export interface QuantDBLocalScanJob {
    job_id: string;
    kind: 'local_scan';
    status: 'running' | 'completed' | 'failed' | 'cancelled' | 'cancelling';
    stage: string;
    root?: string | null;
    datasets?: string[] | null;
    force?: boolean;
    total: number;
    done: number;
    current?: string | null;
    current_detail?: { dataset?: string; phase?: string; done?: number; total?: number; files?: number } | null;
    summary?: {
        root: string;
        registered: number;
        reused: number;
        invalid_files: number;
        total_files: number;
        total_bytes: number;
        elapsed_sec: number;
        state_dbs: Record<string, string>;
        per_dataset: Record<string, { files: number; registered: number; reused: number; invalid: number; bytes: number }>;
        warnings?: string[];
    } | null;
    error?: string | null;
    cancel_requested?: boolean;
    started_at: string;
    finished_at?: string;
    started_by?: string;
}

export interface QuantDBDatasetDiff {
    dataset: string;
    name: string;
    category_id: string;
    layout: QuantDBLayout;
    local: {
        synced: boolean;
        files: number;
        size_mb: number;
        end_date?: string | null;
        partitions?: number;
    };
    remote: {
        end_date?: string | null;
        rows?: number | null;
        files?: number | null;
    } | null;
    status: 'up_to_date' | 'updates_available' | 'not_synced' | 'unknown';
    new_files: number;
}

export interface QuantDBDiffResult {
    datasets: QuantDBDatasetDiff[];
    summary: {
        total_datasets: number;
        up_to_date: number;
        updates_available: number;
        not_synced: number;
        unknown: number;
    };
    timestamp: string;
}

// ---- Qlib 数据管理 ----
export interface QlibStatus {
    enabled?: boolean;
    market: string;
    qlib_dir: string;
    ready: boolean;
    qlib_data: {
        exists: boolean;
        calendar_total_days: number;
        calendar_start_date: string | null;
        calendar_last_date: string | null;
        calendar_files: string[];
        instruments: { total: number; sh: number; sz: number; bj: number; other: number };
        feature_dirs_total: number;
    };
    parquet_latest_date: string | null;
    lag_days: number | null;
    lag_hint: string | null;
    checked_at: string;
}

export interface QlibJobResult {
    build?: { calendar: number; instruments: number; features: number; skipped: number };
    status?: Record<string, any>;
    parquet?: Record<string, any>;
    qlib_cache?: { status?: string; provider_uri?: string; reason?: string } | null;
    finished?: string | null;
}

export interface QlibJob {
    job_id: string;
    kind: string;
    status: 'running' | 'completed' | 'failed' | 'cancelled' | 'cancelling';
    stage: string;
    progress: number;
    current?: string | null;
    datasets?: string[] | null;
    total?: number | null;
    done?: number;
    cancel_requested?: boolean;
    error?: string | null;
    result?: QlibJobResult | Record<string, any> | null;
    started_at: string;
    finished_at?: string | null;
    started_by?: string;
}

class DataPlatformService {
    private axiosInstance: AxiosInstance;
    private readonly baseURL =
        (import.meta as any).env?.VITE_USER_API_URL || SERVICE_ENDPOINTS.USER_SERVICE;

    constructor() {
        this.axiosInstance = axios.create({
            baseURL: this.baseURL,
            timeout: 30000,
            headers: { 'Content-Type': 'application/json' },
        });

        this.axiosInstance.interceptors.request.use((config) => {
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
            } catch (e) {}
            if (config.headers) {
                (config.headers as any)['X-Tenant-Id'] = tenantId;
            }
            return config;
        });

        this.axiosInstance.interceptors.response.use(
            (response) => response,
            async (error) => authService.handle401Error(error, this.axiosInstance),
        );
    }

    private unwrap<T>(resp: any): T {
        const d = resp?.data;
        if (d && d.success && d.data) return d.data as T;
        return d as T;
    }

    async listMarkets(): Promise<{ markets: string[]; timestamp: string }> {
        const resp = await this.axiosInstance.get('/admin/data-platform/markets');
        return this.unwrap(resp);
    }

    async listSources(): Promise<{ sources: SourceSummary[]; timestamp: string }> {
        const resp = await this.axiosInstance.get('/admin/data-platform/sources');
        return this.unwrap(resp);
    }

    async getSourceHealth(name: string): Promise<{ source: string; fields: Record<string, any>; timestamp: string }> {
        const resp = await this.axiosInstance.get(`/admin/data-platform/sources/${name}/health`);
        return this.unwrap(resp);
    }

    async getHealthMatrix(market: string): Promise<HealthMatrix> {
        const resp = await this.axiosInstance.get('/admin/data-platform/health-matrix', {
            params: { market },
        });
        return this.unwrap(resp);
    }

    async getFieldCoverage(): Promise<{ coverage: Record<string, FieldCoverageRow[]>; timestamp: string }> {
        const resp = await this.axiosInstance.get('/admin/data-platform/field-coverage');
        return this.unwrap(resp);
    }

    async listAlerts(params: {
        severity?: string;
        market?: string;
        field?: string;
        acknowledged?: boolean;
        limit?: number;
        offset?: number;
    }): Promise<{ total: number; items: QualityAlert[]; timestamp: string }> {
        const resp = await this.axiosInstance.get('/admin/data-platform/quality-alerts', {
            params,
        });
        return this.unwrap(resp);
    }

    async ackAlert(alertId: number, note?: string): Promise<{ alert_id: number; acknowledged_by: string }> {
        const resp = await this.axiosInstance.post(
            `/admin/data-platform/quality-alerts/${alertId}/ack`,
            { note },
        );
        return this.unwrap(resp);
    }

    async triggerSync(
        name: string,
        payload: { market: string; field: string; symbols: string[] },
    ): Promise<{ source: string; results: Array<any> }> {
        const resp = await this.axiosInstance.post(`/admin/data-platform/sources/${name}/sync`, payload);
        return this.unwrap(resp);
    }

    async sweepMarket(payload: {
        market: string;
        field: string;
        symbols: string[];
        include_fallbacks?: boolean;
    }): Promise<{
        market: string;
        field: string;
        sources: string[];
        symbols: string[];
        summary: { ok: number; failed: number };
        per_source: Array<{ source: string; results: Array<any> }>;
        aggregated: Array<any>;
        timestamp: string;
    }> {
        const resp = await this.axiosInstance.post('/admin/data-platform/sweep', payload);
        return this.unwrap(resp);
    }

    async getFreshness(market: string): Promise<{ market: string; items: FreshnessItem[]; timestamp: string }> {
        const resp = await this.axiosInstance.get('/admin/data-platform/freshness', {
            params: { market },
            timeout: 30000,
        });
        return this.unwrap(resp);
    }

    async getOnlineStatus(): Promise<{
        items: OnlineStatusItem[];
        total: number;
        online: number;
        offline: number;
        timestamp: string;
    }> {
        const resp = await this.axiosInstance.get('/admin/data-platform/online-status', {
            timeout: 120000, // 在线检测可能较慢
        });
        return this.unwrap(resp);
    }

    // ---- QuantDB SDK 管理 ----
    async getQuantDBInfo(): Promise<{
        quantdb: {
            installed: boolean;
            api_key_configured: boolean;
            connected: boolean;
            version?: string;
            account?: { username: string; email: string };
            usage?: {
                used_gb: number;
                limit_gb: number;
                remaining_gb: number;
                credit_gb?: number;
                subscription?: { status: string };
            };
            error?: string;
        };
        timestamp: string;
    }> {
        const resp = await this.axiosInstance.get('/admin/data-platform/quantdb/info');
        return this.unwrap(resp);
    }

    async queryQuantDBKline(payload: {
        symbol: string;
        adj_type?: string;
        start_date?: string;
        end_date?: string;
    }): Promise<{
        symbol: string;
        rows: number;
        columns: string[];
        data: any[];
        timestamp: string;
    }> {
        const resp = await this.axiosInstance.post('/admin/data-platform/quantdb/query-kline', payload);
        return this.unwrap(resp);
    }

    async queryQuantDBStockList(params: {
        keyword?: string;
        limit?: number;
    }): Promise<{ rows: number; columns: string[]; data: any[]; timestamp: string }> {
        const resp = await this.axiosInstance.get('/admin/data-platform/quantdb/stock-list', { params });
        return this.unwrap(resp);
    }

    async queryQuantDBCalendar(start_date: string, end_date: string): Promise<{
        rows: number;
        columns: string[];
        data: any[];
        timestamp: string;
    }> {
        const resp = await this.axiosInstance.get('/admin/data-platform/quantdb/calendar', {
            params: { start_date, end_date },
        });
        return this.unwrap(resp);
    }

    async queryQuantDBTick(payload: {
        symbol: string;
        trade_date: string;
        start_ts?: string;
        end_ts?: string;
        fields?: string;
        limit?: number;
    }): Promise<{
        symbol: string;
        trade_date: string;
        rows: number;
        columns: string[];
        data: any[];
        timestamp: string;
    }> {
        const resp = await this.axiosInstance.post('/admin/data-platform/quantdb/query-tick', payload, {
            timeout: 120000,
        });
        return this.unwrap(resp);
    }

    async queryQuantDBManifest(payload: {
        category_id: string;
        sub_category: string;
        trade_date?: string;
        limit?: number;
    }): Promise<{
        files: any[];
        count: number;
        total: number;
        truncated: boolean;
        timestamp: string;
    }> {
        const resp = await this.axiosInstance.post('/admin/data-platform/quantdb/query-manifest', payload);
        return this.unwrap(resp);
    }

    async getQuantDBConfig(): Promise<QuantDBConfig> {
        const resp = await this.axiosInstance.get('/admin/data-platform/quantdb/config');
        return this.unwrap(resp);
    }

    async saveQuantDBConfig(apiKey: string): Promise<{
        api_key_masked: string;
        verified: boolean;
        error?: string | null;
        timestamp: string;
    }> {
        const resp = await this.axiosInstance.post('/admin/data-platform/quantdb/config', {
            api_key: apiKey,
        });
        return this.unwrap(resp);
    }

    async getQuantDBCatalog(): Promise<{
        data_dir: string;
        groups: QuantDBGroup[];
        datasets: QuantDBDataset[];
        timestamp: string;
    }> {
        const resp = await this.axiosInstance.get('/admin/data-platform/quantdb/catalog', {
            timeout: 120000, // 目录统计需遍历数万个 parquet 文件
        });
        return this.unwrap(resp);
    }

    async previewQuantDBDataset(params: {
        dataset: string;
        symbol?: string;
        limit?: number;
        remote?: boolean;
    }): Promise<QuantDBPreview> {
        const resp = await this.axiosInstance.get('/admin/data-platform/quantdb/preview', {
            params,
            timeout: 120000,
        });
        return this.unwrap(resp);
    }

    async syncQuantDBDatasets(payload: {
        datasets: string[];
        with_pg?: boolean;
        with_qlib?: boolean;
        pg_full?: boolean;
    }): Promise<{ job: QuantDBSyncJob }> {
        const resp = await this.axiosInstance.post(
            '/admin/data-platform/quantdb/sync-datasets',
            payload,
        );
        return this.unwrap(resp);
    }

    async listQuantDBSyncJobs(): Promise<{ jobs: QuantDBSyncJob[]; timestamp: string }> {
        const resp = await this.axiosInstance.get('/admin/data-platform/quantdb/sync-jobs');
        return this.unwrap(resp);
    }

    async getQuantDBSyncJob(jobId: string): Promise<{ job: QuantDBSyncJob }> {
        const resp = await this.axiosInstance.get(`/admin/data-platform/quantdb/sync-jobs/${jobId}`);
        return this.unwrap(resp);
    }

    async cancelQuantDBSyncJob(jobId: string): Promise<{
        job_id: string;
        status: string;
        message: string;
    }> {
        const resp = await this.axiosInstance.post(
            `/admin/data-platform/quantdb/sync-jobs/${jobId}/cancel`,
        );
        return this.unwrap(resp);
    }

    async checkQuantDBDiff(datasets?: string[]): Promise<QuantDBDiffResult> {
        const resp = await this.axiosInstance.get('/admin/data-platform/quantdb/diff', {
            params: datasets ? { datasets: datasets.join(',') } : undefined,
            timeout: 120000,
        });
        return this.unwrap(resp);
    }

    // ---- QuantDB 本地扫描（离线数据 → SQLite 同步状态库） ----
    async localScanPreflight(root?: string): Promise<QuantDBLocalScanPreflight> {
        const resp = await this.axiosInstance.get('/admin/data-platform/quantdb/local-scan/preflight', {
            params: root ? { root } : undefined,
            timeout: 120000, // 预检需遍历全部数据集目录统计文件
        });
        return this.unwrap(resp);
    }

    async startQuantDBLocalScan(payload: {
        root?: string;
        datasets?: string[];
        force?: boolean;
    }): Promise<{ job: QuantDBLocalScanJob }> {
        const resp = await this.axiosInstance.post('/admin/data-platform/quantdb/local-scan', payload, {
            timeout: 30000,
        });
        return this.unwrap(resp);
    }

    async listQuantDBLocalScanJobs(): Promise<{ jobs: QuantDBLocalScanJob[]; timestamp: string }> {
        const resp = await this.axiosInstance.get('/admin/data-platform/quantdb/local-scan/jobs');
        return this.unwrap(resp);
    }

    async getQuantDBLocalScanJob(jobId: string): Promise<{ job: QuantDBLocalScanJob }> {
        const resp = await this.axiosInstance.get(`/admin/data-platform/quantdb/local-scan/jobs/${jobId}`);
        return this.unwrap(resp);
    }

    async cancelQuantDBLocalScanJob(jobId: string): Promise<{
        job_id: string;
        status: string;
        message: string;
    }> {
        const resp = await this.axiosInstance.post(
            `/admin/data-platform/quantdb/local-scan/jobs/${jobId}/cancel`,
        );
        return this.unwrap(resp);
    }

    // ---- QuantUS / QuantHK / QuantBC 本地数据管理（复用 QuantDB 的类型与响应格式） ----
    private marketBase(market: 'quantdb' | 'quantus' | 'quanthk' | 'quantbc' | 'quantfutures'): string {
        return `/admin/data-platform/${market}`;
    }

    async getMarketCatalog(market: 'quantus' | 'quanthk' | 'quantbc' | 'quantfutures'): Promise<{
        data_dir: string;
        market: string;
        groups: QuantDBGroup[];
        datasets: QuantDBDataset[];
        timestamp: string;
    }> {
        const resp = await this.axiosInstance.get(`${this.marketBase(market)}/catalog`, {
            timeout: 120000,
        });
        return this.unwrap(resp);
    }

    async getMarketConfig(market: 'quantus' | 'quanthk' | 'quantbc' | 'quantfutures'): Promise<{
        market: string;
        data_dir: string;
        env_var: string;
        sync_entry: string;
        timestamp: string;
    }> {
        const resp = await this.axiosInstance.get(`${this.marketBase(market)}/config`);
        return this.unwrap(resp);
    }

    async previewMarketDataset(market: 'quantus' | 'quanthk' | 'quantbc' | 'quantfutures', params: {
        dataset: string;
        symbol?: string;
        limit?: number;
    }): Promise<QuantDBPreview> {
        const resp = await this.axiosInstance.get(`${this.marketBase(market)}/preview`, {
            params,
            timeout: 120000,
        });
        return this.unwrap(resp);
    }

    async syncMarketDatasets(market: 'quantus' | 'quanthk' | 'quantbc' | 'quantfutures', payload: {
        datasets: string[];
        days?: number;
        with_qlib?: boolean;
    }): Promise<{ job: QuantDBSyncJob }> {
        const resp = await this.axiosInstance.post(`${this.marketBase(market)}/sync-datasets`, payload);
        return this.unwrap(resp);
    }

    async listMarketSyncJobs(market: 'quantus' | 'quanthk' | 'quantbc' | 'quantfutures'): Promise<{ jobs: QuantDBSyncJob[]; timestamp: string }> {
        const resp = await this.axiosInstance.get(`${this.marketBase(market)}/sync-jobs`);
        return this.unwrap(resp);
    }

    async getMarketSyncJob(market: 'quantus' | 'quanthk' | 'quantbc' | 'quantfutures', jobId: string): Promise<{ job: QuantDBSyncJob }> {
        const resp = await this.axiosInstance.get(`${this.marketBase(market)}/sync-jobs/${jobId}`);
        return this.unwrap(resp);
    }

    async cancelMarketSyncJob(market: 'quantus' | 'quanthk' | 'quantbc' | 'quantfutures', jobId: string): Promise<{
        job_id: string;
        status: string;
        message: string;
    }> {
        const resp = await this.axiosInstance.post(`${this.marketBase(market)}/sync-jobs/${jobId}/cancel`);
        return this.unwrap(resp);
    }

    // ---- 数据源勾选配置 ----
    async getMarketDataSources(market: 'quantdb' | 'quantus' | 'quanthk' | 'quantbc' | 'quantfutures'): Promise<{
        market: string;
        sources: Array<{ source: string; label: string; enabled: boolean }>;
        timestamp: string;
    }> {
        const resp = await this.axiosInstance.get(`${this.marketBase(market)}/data-sources`);
        return this.unwrap(resp);
    }

    async saveMarketDataSources(market: 'quantdb' | 'quantus' | 'quanthk' | 'quantbc' | 'quantfutures', sources: Record<string, boolean>): Promise<{
        market: string;
        sources: Record<string, boolean>;
        timestamp: string;
    }> {
        const resp = await this.axiosInstance.post(`${this.marketBase(market)}/data-sources`, { sources });
        return this.unwrap(resp);
    }

    // ---- Qlib 数据管理（仅 A 股 CN） ----
    async getQlibStatus(market = 'CN'): Promise<QlibStatus> {
        const resp = await this.axiosInstance.get(`/admin/data-platform/qlib/status?market=${market}`, {
            timeout: 60000,
        });
        return this.unwrap(resp);
    }

    async updateQlibFromSdk(market = 'CN'): Promise<{ job: QlibJob }> {
        const resp = await this.axiosInstance.post(`/admin/data-platform/qlib/update-from-sdk?market=${market}`, null, {
            timeout: 30000,
        });
        return this.unwrap(resp);
    }

    async listQlibJobs(): Promise<{ jobs: QlibJob[]; timestamp: string }> {
        const resp = await this.axiosInstance.get('/admin/data-platform/qlib/jobs');
        return this.unwrap(resp);
    }

    async getQlibJob(jobId: string): Promise<{ job: QlibJob }> {
        const resp = await this.axiosInstance.get(`/admin/data-platform/qlib/jobs/${jobId}`);
        return this.unwrap(resp);
    }

    async cancelQlibJob(jobId: string): Promise<{
        job_id: string;
        status: string;
        message: string;
    }> {
        const resp = await this.axiosInstance.post(
            `/admin/data-platform/qlib/jobs/${jobId}/cancel`,
        );
        return this.unwrap(resp);
    }
}

export const dataPlatformService = new DataPlatformService();
