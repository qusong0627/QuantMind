import { ApiResponse } from '../auth/types/auth.types';

export interface AdminUser {
    user_id: string;
    username: string;
    email: string;
    is_active: boolean;
    is_admin: boolean;
    created_at: string;
}

export interface CommunityPost {
    id: number;
    tenant_id: string;
    author_id: string;
    title: string;
    category: string;
    views: number;
    likes: number;
    comments: number;
    pinned: boolean;
    featured: boolean;
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
}

export interface AdminModelFeatureCatalog {
    version_id: string;
    version_name: string;
    feature_count: number;
    categories: AdminModelFeatureCategory[];
    source: 'database' | 'file';
    fallback_path?: string;
    data_coverage?: AdminModelFeatureDataCoverage;
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

export interface AdminFeatureSnapshotsStatus {
    exists: boolean;
    snapshot_dir: string;
    file_count: number;
    scanned_files: number;
    failed_files: number;
    total_rows: number;
    min_date: string | null;
    max_date: string | null;
    latest_date_coverage: AdminFeatureSnapshotsLatestCoverage;
    topn_samples?: AdminFeatureSnapshotsTopNSamples;
    suggested_periods?: AdminFeatureSnapshotsSuggestedPeriods;
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

export interface AdminMarketDataSyncResult {
    success: boolean;
    async?: boolean;
    task_id?: string;
    exit_code: number;
    error?: string;
    stdout?: string;
    stderr?: string;
    result?: {
        success?: boolean;
        mode?: string;
        requested_date?: string;
        effective_trade_date?: string;
        symbols_total?: number;
        symbols_ok?: number;
        symbols_failed?: number;
        rows_upserted?: number;
        error_samples?: Array<{ symbol: string; error: string }>;
    };
}

export interface AdminOfficialDataUpdateSyncResult {
    success: boolean;
    exit_code: number;
    error?: string;
    stdout?: string;
    stderr?: string;
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
    content: {
        posts: number;
        comments: number;
    };
    system: {
        health_score: number;
        uptime_days: number;
    };
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
}
