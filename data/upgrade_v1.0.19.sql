-- upgrade_v1.0.19: 补齐因子挖掘企业版 8 张表（存储配额账本 4 + 因子代次 3 + 报告归档 1）。
-- 与 backend/shared/db_init.sql 第 T 节同 DDL；幂等可重放，新库走 db_init，
-- 存量库靠本脚本拉齐。写法约束：本文件不得出现百分号字符（psycopg2 fallback
-- 会误解析），故 CHECK 用 IN 枚举和正则匹配，不用 LIKE。
--
-- 来源（企业版仓库 data/ 原文合并为最终完整定义）：
--   storage_quota_*: upgrade_v1.0.27.sql + upgrade_v1.0.28.sql（replaced_bytes 列
--     + reserved 状态资源唯一索引，账本代码读写该列）；
--   alpha_user_factor_*: upgrade_v1.0.36.sql + upgrade_v1.0.37.sql（dataset_key 放宽
--     到 l1_factors/rd_mined，OSS 代码写 rd_mined）+ upgrade_v1.0.39.sql
--    （factor_revision_no 列，factor_jobs.py 写入该列）；
--   qm_user_report_archive: upgrade_v1.0.25.sql 合并段原 v1.0.20。
-- 与企业版原文的差异（OSS 侧无匹配唯一键，建外键会失败，故省略）：
--   alpha_user_factor_generation_features -> rd_agent_factors(tenant_id,user_id,factor_id)
--   的外键省略：OSS rd_agent_factors 主键只是 factor_id，无双维唯一键
--   （见 upgrade_v1.0.16.sql 文件头同类处理）；factor_id 数据列保留。

BEGIN;

SET LOCAL lock_timeout = '10s';

-- ---------------------------------------------------------------------------
-- T1. 存储配额账本（企业版 upgrade_v1.0.27 + v1.0.28 合并）。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.storage_quota_state (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    ready BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO public.storage_quota_state (singleton, ready)
VALUES (TRUE, FALSE)
ON CONFLICT (singleton) DO NOTHING;

CREATE TABLE IF NOT EXISTS public.storage_quota_accounts (
    tenant_id VARCHAR(64) NOT NULL,
    user_id VARCHAR(64) NOT NULL,
    used_bytes BIGINT NOT NULL DEFAULT 0 CHECK (used_bytes >= 0),
    reserved_bytes BIGINT NOT NULL DEFAULT 0 CHECK (reserved_bytes >= 0),
    quota_bytes BIGINT NOT NULL CHECK (quota_bytes >= 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, user_id)
);

CREATE TABLE IF NOT EXISTS public.storage_quota_reservations (
    reservation_id VARCHAR(64) PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    user_id VARCHAR(64) NOT NULL,
    resource_key TEXT NOT NULL,
    reserved_bytes BIGINT NOT NULL CHECK (reserved_bytes >= 0),
    replaced_bytes BIGINT NOT NULL DEFAULT 0 CHECK (replaced_bytes >= 0),
    actual_bytes BIGINT,
    state VARCHAR(16) NOT NULL CHECK (state IN ('reserved', 'settled', 'released')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE public.storage_quota_reservations
    ADD COLUMN IF NOT EXISTS replaced_bytes BIGINT NOT NULL DEFAULT 0
    CHECK (replaced_bytes >= 0);

CREATE INDEX IF NOT EXISTS idx_storage_quota_reservations_owner_state
    ON public.storage_quota_reservations (tenant_id, user_id, state);

CREATE UNIQUE INDEX IF NOT EXISTS uq_storage_quota_active_resource
    ON public.storage_quota_reservations (resource_key)
    WHERE state = 'reserved';

CREATE TABLE IF NOT EXISTS public.storage_quota_objects (
    resource_key TEXT PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    user_id VARCHAR(64) NOT NULL,
    size_bytes BIGINT NOT NULL CHECK (size_bytes >= 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_storage_quota_objects_owner
    ON public.storage_quota_objects (tenant_id, user_id);

-- ---------------------------------------------------------------------------
-- T2. 企业版用户因子代次（upgrade_v1.0.36 + v1.0.37 + v1.0.39 合并）。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS alpha_user_factor_generations (
    tenant_id TEXT NOT NULL CHECK (btrim(tenant_id) <> ''),
    user_id TEXT NOT NULL CHECK (btrim(user_id) <> ''),
    generation_id TEXT NOT NULL CHECK (generation_id ~ '^[A-Za-z0-9_-]{1,64}$'),
    dataset_key TEXT NOT NULL CHECK (dataset_key IN ('l1_factors', 'rd_mined')),
    status TEXT NOT NULL CHECK (status IN ('building', 'published', 'failed', 'archived')),
    job_id TEXT NOT NULL,
    manifest_json JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(manifest_json) = 'object'),
    row_count BIGINT NOT NULL DEFAULT 0 CHECK (row_count >= 0),
    byte_count BIGINT NOT NULL DEFAULT 0 CHECK (byte_count >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at TIMESTAMPTZ,
    PRIMARY KEY (tenant_id, user_id, generation_id),
    UNIQUE (tenant_id, user_id, dataset_key, generation_id),
    FOREIGN KEY (tenant_id, user_id, job_id)
        REFERENCES alpha_factor_jobs (tenant_id, user_id, job_id)
);

CREATE TABLE IF NOT EXISTS alpha_user_factor_generation_features (
    tenant_id TEXT NOT NULL CHECK (btrim(tenant_id) <> ''),
    user_id TEXT NOT NULL CHECK (btrim(user_id) <> ''),
    generation_id TEXT NOT NULL,
    feature_key TEXT NOT NULL CHECK (feature_key ~ '^[A-Za-z][A-Za-z0-9_]{0,127}$'),
    factor_id TEXT NOT NULL,
    factor_code_sha256 TEXT NOT NULL CHECK (factor_code_sha256 ~ '^[0-9a-f]{64}$'),
    factor_revision_no BIGINT CHECK (factor_revision_no IS NULL OR factor_revision_no > 0),
    source_task_id TEXT,
    row_count BIGINT NOT NULL CHECK (row_count >= 0),
    PRIMARY KEY (tenant_id, user_id, generation_id, feature_key),
    UNIQUE (tenant_id, user_id, generation_id, factor_id),
    FOREIGN KEY (tenant_id, user_id, generation_id)
        REFERENCES alpha_user_factor_generations (tenant_id, user_id, generation_id)
        ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, user_id, source_task_id)
        REFERENCES alpha_mining_tasks (tenant_id, user_id, task_id)
);

ALTER TABLE alpha_user_factor_generation_features
    ADD COLUMN IF NOT EXISTS factor_revision_no BIGINT;

CREATE TABLE IF NOT EXISTS alpha_user_factor_dataset_active (
    tenant_id TEXT NOT NULL CHECK (btrim(tenant_id) <> ''),
    user_id TEXT NOT NULL CHECK (btrim(user_id) <> ''),
    dataset_key TEXT NOT NULL CHECK (dataset_key IN ('l1_factors', 'rd_mined')),
    generation_id TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, user_id, dataset_key),
    FOREIGN KEY (tenant_id, user_id, dataset_key, generation_id)
        REFERENCES alpha_user_factor_generations
            (tenant_id, user_id, dataset_key, generation_id)
);

CREATE INDEX IF NOT EXISTS idx_alpha_user_factor_generations_owner_status
    ON alpha_user_factor_generations (tenant_id, user_id, dataset_key, status, created_at DESC);

-- ---------------------------------------------------------------------------
-- T3. 报告归档（企业版 upgrade_v1.0.25 合并段原 v1.0.20）。
-- storage_quota_ledger 对账 SELECT 该表统计报告体积，缺表则对账挂掉；
-- OSS 报告文件本身仍走 QM_REPORT_ARCHIVE_DIR 文件系统，本表只做体积口径。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS qm_user_report_archive (
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    report_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind = 'stock_deep_report'),
    symbol TEXT NOT NULL,
    as_of DATE,
    generated_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL,
    payload_hash TEXT NOT NULL,
    PRIMARY KEY (tenant_id, user_id, report_id)
);

CREATE INDEX IF NOT EXISTS idx_qm_user_report_archive_owner_date
    ON qm_user_report_archive (tenant_id, user_id, generated_at DESC);

COMMIT;
