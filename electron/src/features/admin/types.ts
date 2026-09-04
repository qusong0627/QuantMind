import { ApiResponse } from '../auth/types/auth.types';

export interface AdminUser {
    user_id: string;
    username: string;
    email: string;
    is_active: boolean;
    is_admin: boolean;
    created_at: string;
}

export interface AIModel {
    id: number;
    name: string;
    description?: string;
    source_type: string;
    start_date?: string;
    end_date?: string;
    user_id: string;
    is_active: boolean;
    created_at: string;
    updated_at: string;
}

/** 模型目录文件条目 */
export interface ModelFileInfo {
    name: string;
    size: number;
    modified_at: string;
}

/** 自动扫描得到的单个模型目录信息 */
export interface ModelDirectoryInfo {
    model_id: string;
    dir_path: string;
    is_production: boolean;
    feature_count: number | null;
    model_format: string | null;
    resolved_class: string | null;
    sha256: string | null;
    train_start: string | null;
    train_end: string | null;
    test_start: string | null;
    test_end: string | null;
    updated_at: string;
    metadata: Record<string, any> | null;
    performance_metrics: Record<string, any> | null;
    workflow_config: Record<string, any> | null;
    qlib_config: Record<string, any> | null;
    best_params: Record<string, any> | null;
    feature_schema: any | null;
    feature_description: string | null;
    files: ModelFileInfo[];
    error?: string;
}

/** /admin/models/scan 接口响应 */
export interface ModelScanResult {
    total: number;
    models: ModelDirectoryInfo[];
    from_cache?: boolean;
    cached_at?: number;
}

export interface InferencePrecheckItem {
    key: string;
    label: string;
    passed: boolean;
    detail: string;
}

export interface InferencePrecheckResult {
    passed: boolean;
    checked_at: string;
    requested_inference_date?: string;
    calendar_adjusted?: boolean;
    data_trade_date?: string;
    prediction_trade_date?: string;
    items: InferencePrecheckItem[];
}

export interface AdminModelFeatureItem {
    feature_id: string;
    key: string;
    feature_name: string;
    formula: string;
    source_table_fields: string;
    enabled: boolean;
    order_no: number;
    default_selected?: boolean;
    required?: boolean;
    source_dataset?: string;
    source_column?: string;
    markets?: string[];
}

export interface AdminModelFeatureCategory {
    id: string;
    name: string;
    order: number;
    feature_count: number;
    features: AdminModelFeatureItem[];
}

export interface AdminModelFeatureSuggestedPeriods {
    train: [string, string];
    val: [string, string];
    test: [string, string];
}

export interface AdminModelFeatureDataCoverage {
    source: string;
    snapshot_dir: string;
    file_count: number;
    scanned_files: number;
    failed_files: number;
    total_rows: number;
    min_date: string;
    max_date: string;
    suggested_periods?: AdminModelFeatureSuggestedPeriods | null;
    /** Direct QuantDB factor source compatibility fields. */
    dataset_id?: string;
    schema_hash?: string;
    ready?: boolean;
    missing_required?: string[];
    reason?: string | null;
    refreshed_at?: string | null;
}

export interface AdminModelFeatureCatalog {
    version_id: string;
    version_name: string;
    feature_count: number;
    categories: AdminModelFeatureCategory[];
    source: 'database' | 'file' | 'quantdb_factor_catalog';
    source_dataset?: string;
    status?: 'draft' | 'published' | 'archived' | 'unpublished';
    /** ready = published catalog plus a ready raw QuantDB source. */
    catalog_status?: 'ready' | 'unpublished' | 'source_not_ready';
    message?: string | null;
    fallback_path?: string;
    data_coverage?: AdminModelFeatureDataCoverage;
}

export interface QuantDBTrainingSource {
    id: string;
    name: string;
    default: boolean;
    ready: boolean;
    published: boolean;
    trainable: boolean;
    feature_count: number;
    catalog_version: string | null;
    schema_hash: string;
    reason: string | null;
}

export interface QuantDBTrainingSourcesResult {
    default_source: string;
    sources: QuantDBTrainingSource[];
}

export interface AdminPredictionRunSummary {
    run_id: string;
    trade_date: string;
    tenant_id: string;
    user_id: string;
    model_version: string;
    rows_count: number;
    symbols_count: number;
    min_fusion_score: number | null;
    max_fusion_score: number | null;
    first_created_at: string | null;
    last_created_at: string | null;
}

export interface AdminPredictionListResult {
    page: number;
    page_size: number;
    total: number;
    items: AdminPredictionRunSummary[];
}

export interface AdminPredictionSignalItem {
    symbol: string;
    fusion_score: number | null;
    light_score: number | null;
    tft_score: number | null;
    score_rank: number | null;
    signal_side: string | null;
    expected_price: number | null;
    quality: string | null;
    created_at: string | null;
}

export interface AdminPredictionDetailResult {
    summary: AdminPredictionRunSummary;
    page: number;
    page_size: number;
    total: number;
    items: AdminPredictionSignalItem[];
}

export interface AdminDataStatusQlibInstruments {
    total: number;
    sh: number;
    sz: number;
    bj: number;
    other: number;
}

export interface AdminDataStatusLatestCoverage {
    target_date: string | null;
    at_target_count: number;
    older_count: number;
    invalid_count: number;
}

export interface AdminDataStatusOlderSample {
    symbol: string;
    last_date: string;
    lag_days: number;
}

export interface AdminDataStatusInvalidSample {
    symbol: string;
    reason: string;
    file?: string;
    size?: number;
    start_index?: number;
    rows?: number;
    end_index?: number;
}

export interface AdminDataStatusTopNSamples {
    sample_size: number;
    older_samples: AdminDataStatusOlderSample[];
    invalid_samples: AdminDataStatusInvalidSample[];
}

export interface AdminDataStatusQlib {
    qlib_dir: string;
    exists: boolean;
    calendar_total_days: number;
    calendar_start_date: string | null;
    calendar_last_date: string | null;
    instruments: AdminDataStatusQlibInstruments;
    feature_dirs_total: number;
    feature_dirs_sh_sz: number;
    feature_fields_expected: string[];
    latest_date_coverage: AdminDataStatusLatestCoverage;
    topn_samples?: AdminDataStatusTopNSamples;
    calendar_error?: string;
    instruments_error?: string;
}

export interface AdminFeatureSnapshotsOlderSample {
    symbol: string;
    last_date: string;
    lag_days: number;
}

export interface AdminFeatureSnapshotsInvalidSample {
    symbol: string;
    reason: string;
    file?: string;
}

export interface AdminFeatureSnapshotsTopNSamples {
    sample_size: number;
    older_samples: AdminFeatureSnapshotsOlderSample[];
    invalid_samples: AdminFeatureSnapshotsInvalidSample[];
}

export interface AdminFeatureSnapshotsLatestCoverage {
    target_date: string | null;
    at_target_count: number;
    older_count: number;
    invalid_count: number;
}

export interface AdminFeatureSnapshotsSuggestedPeriods {
    train: [string, string];
    val: [string, string];
    test: [string, string];
}

export interface AdminFeatureSnapshotMetadata {
    year: number;
    start_date: string;
    end_date: string;
    row_count: number;
    feature_dim: number;
    symbol_count: number;
    filename: string;
}

export interface AdminFeatureSnapshotsStatus {
    exists: boolean;
    snapshot_dir: string;
    file_count: number;
    scanned_files: number;
    failed_files: number;
    total_rows: number;
    min_date: string | null;
    max_date: string | null;
    metadata_files?: AdminFeatureSnapshotMetadata[];
    latest_date_coverage: AdminFeatureSnapshotsLatestCoverage;
    topn_samples?: AdminFeatureSnapshotsTopNSamples;
    suggested_periods?: AdminFeatureSnapshotsSuggestedPeriods;
    integrity_status?: 'ok' | 'warning' | 'error';
    integrity_issues?: string[];
    fallback_used?: boolean;
    error?: string;
}

export interface AdminDataStatusResult {
    checked_at: string;
    trade_date: string;
    qlib_data: AdminDataStatusQlib;
    feature_snapshots: AdminFeatureSnapshotsStatus;
    from_cache?: boolean;
    message?: string;
}

export interface DashboardMetrics {
    users: {
        total: number;
        active: number;
        new_today: number;
    };
    strategies: {
        total: number;
        live: number;
        backtesting: number;
    };
    models: {
        total: number;
    };
    system: {
        health_score: number;
        uptime_days: number;
        status?: string;
        services?: DashboardServiceInfo[];
        workload?: HostWorkload;
        services_summary?: {
            healthy: number;
            total: number;
        };
    };
    recent_events?: Array<{
        title: string;
        time: string;
        type: 'success' | 'warning' | 'info';
    }>;
}

export interface HostWorkload {
    cpu_percent: number;
    cpu_count: number;
    memory_percent: number;
    memory_used_gb: number;
    memory_total_gb: number;
    disk_percent: number;
    disk_used_gb: number;
    disk_total_gb: number;
}

export interface SystemLoadSummary {
    workload: HostWorkload;
    uptime_days: number | null;
    health_score: number;
    services_summary: {
        healthy: number;
        total: number;
    };
}

/** 后端 /admin/dashboard/metrics 返回的真实服务健康信息 */
export interface DashboardServiceInfo {
    service: string;
    url?: string;
    port?: number;
    desc?: string;
    status: string;
    score: number;
    healthy: boolean;
    details?: Record<string, unknown>;
    error?: string;
}

export type AdminTab = 'dashboard' | 'users' | 'models' | 'data' | 'strategy-templates';

/** 策略模板参数定义 */
export interface StrategyTemplateParam {
    name: string;
    description: string;
    default: number | string | boolean;
    min?: number;
    max?: number;
}

/** 管理员视图下的策略模板（含完整代码） */
export interface StrategyTemplateAdmin {
    id: string;
    name: string;
    description: string;
    category: 'basic' | 'advanced' | 'risk_control';
    difficulty: 'beginner' | 'intermediate' | 'advanced';
    code: string;
    params: StrategyTemplateParam[];
    execution_defaults: Record<string, any>;
    live_defaults: Record<string, any>;
    live_config_tips: string[];
    markets?: string[];
}

/** 新建/更新模板的请求体 */
export interface StrategyTemplateUpsertRequest {
    id?: string;
    name: string;
    description: string;
    category: 'basic' | 'advanced' | 'risk_control';
    difficulty: 'beginner' | 'intermediate' | 'advanced';
    code: string;
    params: StrategyTemplateParam[];
    execution_defaults?: Record<string, any>;
    live_defaults?: Record<string, any>;
    live_config_tips?: string[];
    markets?: string[];
}
