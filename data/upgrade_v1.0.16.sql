-- upgrade_v1.0.16: 企业版因子挖掘数据库表下放（OSS 第 68 节同 DDL，见 backend/shared/db_init.sql）。
-- 来源（企业版仓库 data/）：upgrade_v1.0.29 ~ v1.0.33、v1.0.34、v1.0.35、v1.0.38、
-- v1.0.40 ~ v1.0.46、v1.0.48、v1.0.49 ~ v1.0.56、v1.0.58、v1.0.59、v1.0.60、
-- v1.0.62 ~ v1.0.65（含散落在 v1.0.37 / v1.0.39 / v1.0.41 ~ v1.0.44 /
-- v1.0.48 / v1.0.51 / v1.0.54 / v1.0.58 / v1.0.59 / v1.0.63 / v1.0.64 的 ALTER）。
-- 同一张表分散在多个文件中的 CREATE + ALTER 已合并为最终完整定义
-- （全列 CREATE TABLE IF NOT EXISTS，不照搬 ALTER 链）。
-- tenant_id / user_id 列与原约束保留：OSS 侧 DML 可与企业版 SQL 零改动复用。
-- 写法约束：本文件不得出现百分号字符（psycopg2 fallback 会误解析）。
--
-- 与企业版原文的差异（OSS 侧无对应父表，建外键会失败，故省略）：
-- 1. 指向 resource_task_leases 的外键省略（alpha_factor_jobs、alpha_research_requests
--    各一处），以及 v1.0.31 在 resource_task_leases 上建的唯一索引；
--    resource_task_leases 系多租户/CVM 基础设施，仍不移植；
--    resource_lease_pools 建表 + alpha_mining 种子行见文件末尾 Q 节。
-- 2. 指向 rd_agent_factors (tenant_id, user_id, factor_id) 的外键省略
--    （validation_attempts、factor_pool、factor_edges 两处、factor_revisions、
--    lifecycle_events、recovery_sources、visibility_events、materializations、
--    mining_materialization_outbox）：OSS 的 rd_agent_factors 尚无 tenant 列，
--    无匹配的唯一键可供引用；factor_id / tenant_id / user_id 数据列全部保留。
-- 3. 在 rd_agent_factors 上建唯一索引 / 可见性索引的语句省略（同上，无 tenant 列）；
--    但 v1.0.49 给 rd_agent_factors 加的 archived_at / archive_reason 两列予以保留。
-- 4. 数据回填 DML 省略（v1.0.32 outbox 回填、v1.0.38 修订版回填）：新库无存量行，
--    存量库按需由业务侧补；表结构不受影响。
-- 5. 跳过（按任务要求）：v1.0.25 resource_task_leases（多租户/CVM 基础设施）、
--    v1.0.36 alpha_user_factor_*（OSS 已有 qm_ 版，见第 67 节）、
--    v1.0.66 alpha_mining_node_assignments（CVM 专用）。
--    注：v1.0.25 的 resource_lease_pools 建表 + alpha_mining 种子行已在文件末尾
--    Q 节下放（AlphaMiningTaskPersistence 建池锁依赖 ready=true）。
-- 6. alpha_mining_tasks -> rd_agent_docs 的外键因三表成环
--    （tasks -> docs -> jobs -> tasks），在文件末尾以 DO 块延迟追加，其余外键内联。
-- 7. alpha_factor_jobs.kind 取各版本最宽并集，避免老约束拒绝新 kind 写入。

BEGIN;

-- ---------------------------------------------------------------------------
-- A. 任务主表（合并 v1.0.29 建表 + executor_json / execution_fingerprint /
--    completed_at + v1.0.43 source_doc_id + v1.0.48 archived_at）。
--    source_doc 外键见文件末尾（成环延迟追加）。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS alpha_mining_tasks (
    task_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL CHECK (btrim(tenant_id) <> ''),
    user_id TEXT NOT NULL CHECK (btrim(user_id) <> ''),
    market TEXT NOT NULL DEFAULT 'a_share',
    universe TEXT NOT NULL DEFAULT 'csi300',
    loop_n INT NOT NULL DEFAULT 5,
    direction TEXT NOT NULL DEFAULT '',
    data_source TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    progress TEXT NOT NULL DEFAULT '',
    phase TEXT NOT NULL DEFAULT 'pending',
    progress_pct INT NOT NULL DEFAULT 0,
    celery_task_id TEXT,
    executor_json JSONB NOT NULL DEFAULT '{}',
    execution_fingerprint TEXT,
    error_message TEXT,
    token_usage JSONB NOT NULL DEFAULT '{}',
    source_doc_id TEXT,
    archived_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_alpha_tasks_tenant_user
    ON alpha_mining_tasks (tenant_id, user_id);
CREATE INDEX IF NOT EXISTS idx_alpha_tasks_status ON alpha_mining_tasks (status);
CREATE UNIQUE INDEX IF NOT EXISTS uq_alpha_tasks_owner_id
    ON alpha_mining_tasks (tenant_id, user_id, task_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_alpha_tasks_active_fingerprint
    ON alpha_mining_tasks (tenant_id, user_id, execution_fingerprint)
    WHERE execution_fingerprint IS NOT NULL
      AND status IN ('pending', 'queued', 'running', 'stopping');

-- 不可变任务规格（v1.0.29 原文）。
CREATE TABLE IF NOT EXISTS alpha_mining_task_specs (
    tenant_id TEXT NOT NULL CHECK (btrim(tenant_id) <> ''),
    user_id TEXT NOT NULL CHECK (btrim(user_id) <> ''),
    task_id TEXT NOT NULL,
    schema_version INT NOT NULL CHECK (schema_version = 1),
    algorithm TEXT NOT NULL CHECK (algorithm IN ('rdagent', 'statistical')),
    algorithm_version TEXT NOT NULL CHECK (btrim(algorithm_version) <> ''),
    request_key TEXT CHECK (
        request_key IS NULL OR (length(request_key) BETWEEN 1 AND 128 AND btrim(request_key) <> '')
    ),
    request_fingerprint TEXT NOT NULL CHECK (length(request_fingerprint) = 64),
    request_json JSONB NOT NULL CHECK (jsonb_typeof(request_json) = 'object'),
    params_json JSONB NOT NULL CHECK (jsonb_typeof(params_json) = 'object'),
    data_snapshot JSONB NOT NULL CHECK (jsonb_typeof(data_snapshot) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, user_id, task_id),
    FOREIGN KEY (tenant_id, user_id, task_id)
        REFERENCES alpha_mining_tasks (tenant_id, user_id, task_id)
);
CREATE TABLE IF NOT EXISTS alpha_mining_request_keys (
    tenant_id TEXT NOT NULL CHECK (btrim(tenant_id) <> ''),
    user_id TEXT NOT NULL CHECK (btrim(user_id) <> ''),
    request_key TEXT NOT NULL CHECK (
        length(request_key) BETWEEN 1 AND 128 AND btrim(request_key) <> ''
    ),
    task_id TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL CHECK (length(request_fingerprint) = 64),
    request_json JSONB NOT NULL CHECK (jsonb_typeof(request_json) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, user_id, request_key),
    FOREIGN KEY (tenant_id, user_id, task_id)
        REFERENCES alpha_mining_task_specs (tenant_id, user_id, task_id)
);

-- 不可变挖掘结果载荷（v1.0.30 原文）。
CREATE TABLE IF NOT EXISTS alpha_mining_task_results (
    tenant_id TEXT NOT NULL CHECK (btrim(tenant_id) <> ''),
    user_id TEXT NOT NULL CHECK (btrim(user_id) <> ''),
    task_id TEXT NOT NULL,
    schema_version INT NOT NULL DEFAULT 1 CHECK (schema_version = 1),
    execution_fingerprint TEXT NOT NULL CHECK (length(execution_fingerprint) = 64),
    result_json JSONB NOT NULL CHECK (jsonb_typeof(result_json) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, user_id, task_id),
    FOREIGN KEY (tenant_id, user_id, task_id)
        REFERENCES alpha_mining_tasks (tenant_id, user_id, task_id)
);
CREATE INDEX IF NOT EXISTS idx_alpha_task_results_owner_created
    ON alpha_mining_task_results (tenant_id, user_id, created_at DESC);

-- ---------------------------------------------------------------------------
-- B. 因子任务（合并 v1.0.31 建表 + v1.0.42 / v1.0.51 / v1.0.54 / v1.0.58 /
--    v1.0.59 / v1.0.63 / v1.0.64 的 kind 加宽 + v1.0.54 pool_research 索引导出）。
--    指向 resource_task_leases 的外键按文件头说明省略。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS alpha_factor_jobs (
    tenant_id TEXT NOT NULL CHECK (btrim(tenant_id) <> ''),
    user_id TEXT NOT NULL CHECK (btrim(user_id) <> ''),
    job_id TEXT NOT NULL,
    lease_pool TEXT NOT NULL DEFAULT 'alpha_factor_jobs'
        CHECK (lease_pool = 'alpha_factor_jobs'),
    parent_task_id TEXT,
    kind TEXT NOT NULL CHECK (kind IN (
        'backtest', 'validate', 'materialize', 'serve_features', 'report', 'compose',
        'doc_parse', 'doc_organize', 'promote', 'recovery', 'cleanup',
        'pool_research', 'purge', 'purge_task'
    )),
    status TEXT NOT NULL CHECK (status IN (
        'queued', 'running', 'stopping', 'completed', 'failed', 'cancelled'
    )),
    execution_generation BIGINT NOT NULL DEFAULT 1 CHECK (execution_generation > 0),
    request_fingerprint TEXT NOT NULL CHECK (length(request_fingerprint) = 64),
    request_snapshot JSONB NOT NULL CHECK (jsonb_typeof(request_snapshot) = 'object'),
    progress_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(progress_snapshot) = 'object'),
    result_snapshot JSONB CHECK (
        result_snapshot IS NULL OR jsonb_typeof(result_snapshot) = 'object'
    ),
    error_code TEXT,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    PRIMARY KEY (tenant_id, user_id, job_id),
    FOREIGN KEY (tenant_id, user_id, parent_task_id)
        REFERENCES alpha_mining_tasks (tenant_id, user_id, task_id)
);

CREATE TABLE IF NOT EXISTS alpha_factor_job_request_keys (
    tenant_id TEXT NOT NULL CHECK (btrim(tenant_id) <> ''),
    user_id TEXT NOT NULL CHECK (btrim(user_id) <> ''),
    request_key TEXT NOT NULL CHECK (btrim(request_key) <> ''),
    request_fingerprint TEXT NOT NULL CHECK (length(request_fingerprint) = 64),
    job_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, user_id, request_key),
    FOREIGN KEY (tenant_id, user_id, job_id)
        REFERENCES alpha_factor_jobs (tenant_id, user_id, job_id)
);

CREATE INDEX IF NOT EXISTS alpha_factor_jobs_owner_created
    ON alpha_factor_jobs (tenant_id, user_id, created_at DESC, job_id DESC);
CREATE INDEX IF NOT EXISTS alpha_factor_jobs_queue
    ON alpha_factor_jobs (status, created_at, job_id)
    WHERE status IN ('queued', 'running', 'stopping');
CREATE UNIQUE INDEX IF NOT EXISTS alpha_factor_jobs_active_fingerprint
    ON alpha_factor_jobs (tenant_id, user_id, kind, request_fingerprint)
    WHERE status IN ('queued', 'running', 'stopping');
CREATE INDEX IF NOT EXISTS alpha_factor_job_request_keys_job
    ON alpha_factor_job_request_keys (tenant_id, user_id, job_id);
CREATE INDEX IF NOT EXISTS alpha_factor_jobs_owner_pool_research
    ON alpha_factor_jobs (tenant_id,user_id,((request_snapshot->'request')->>'universe'),created_at DESC,job_id DESC)
    WHERE kind='pool_research';

CREATE OR REPLACE FUNCTION qm_pool_research_evidence_guard() RETURNS trigger LANGUAGE plpgsql AS $guard$
BEGIN
    IF OLD.kind='pool_research' THEN
        IF ROW(NEW.tenant_id,NEW.user_id,NEW.job_id,NEW.kind,NEW.lease_pool,NEW.parent_task_id,NEW.request_fingerprint,NEW.request_snapshot,NEW.created_at)
           IS DISTINCT FROM ROW(OLD.tenant_id,OLD.user_id,OLD.job_id,OLD.kind,OLD.lease_pool,OLD.parent_task_id,OLD.request_fingerprint,OLD.request_snapshot,OLD.created_at) THEN
            RAISE EXCEPTION 'pool research input and ownership are immutable';
        END IF;
        IF OLD.status IN ('completed','failed','cancelled') AND NEW IS DISTINCT FROM OLD THEN
            RAISE EXCEPTION 'terminal pool research evidence is immutable';
        END IF;
    END IF;
    RETURN NEW;
END;
$guard$;
DROP TRIGGER IF EXISTS qm_pool_research_evidence_guard ON alpha_factor_jobs;
CREATE TRIGGER qm_pool_research_evidence_guard BEFORE UPDATE ON alpha_factor_jobs
    FOR EACH ROW EXECUTE FUNCTION qm_pool_research_evidence_guard();

-- 任务派发 outbox（v1.0.32 原文；存量 queued 回填 DML 省略，见文件头）。
CREATE TABLE IF NOT EXISTS alpha_factor_job_outbox (
    tenant_id TEXT NOT NULL CHECK (btrim(tenant_id) <> ''),
    user_id TEXT NOT NULL CHECK (btrim(user_id) <> ''),
    job_id TEXT NOT NULL,
    event_type TEXT NOT NULL CHECK (event_type = 'run_factor_job'),
    available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    dispatch_token TEXT,
    claim_until TIMESTAMPTZ,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    published_at TIMESTAMPTZ,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, user_id, job_id, event_type),
    FOREIGN KEY (tenant_id, user_id, job_id)
        REFERENCES alpha_factor_jobs (tenant_id, user_id, job_id)
);

CREATE INDEX IF NOT EXISTS alpha_factor_job_outbox_ready
    ON alpha_factor_job_outbox (available_at, created_at)
    WHERE published_at IS NULL;
CREATE INDEX IF NOT EXISTS alpha_factor_job_outbox_reconcile
    ON alpha_factor_job_outbox (claim_until, available_at, created_at);

-- ---------------------------------------------------------------------------
-- C. 研究文档（合并 v1.0.40 建表 + v1.0.41 解析任务列 + v1.0.42 组织任务列
--    + v1.0.44 多文件列）。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS rd_agent_docs (
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    ext TEXT NOT NULL,
    size_bytes BIGINT NOT NULL CHECK (size_bytes >= 0),
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
    storage_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'uploaded', 'parsing', 'parsed', 'parse_failed', 'organized', 'expired', 'deleted'
    )),
    parser_batch_id TEXT,
    page_count INTEGER CHECK (page_count IS NULL OR page_count >= 0),
    markdown_key TEXT,
    content_list_key TEXT,
    organized_text TEXT,
    organize_kind TEXT,
    organized_at TIMESTAMPTZ,
    error TEXT,
    parse_job_id TEXT,
    parsed_artifact_keys JSONB NOT NULL DEFAULT '[]'::jsonb,
    parse_state TEXT,
    organize_job_id TEXT,
    organize_state TEXT NOT NULL DEFAULT 'idle',
    organized_payload JSONB,
    organize_prompt_version TEXT,
    files_count INTEGER NOT NULL DEFAULT 1,
    original_parts JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (tenant_id, user_id, doc_id),
    CONSTRAINT rd_agent_docs_parsed_artifact_keys_array
        CHECK (jsonb_typeof(parsed_artifact_keys) = 'array'),
    CONSTRAINT rd_agent_docs_organize_state_check
        CHECK (organize_state IN (
            'idle', 'queued', 'running', 'stopping',
            'completed', 'failed', 'cancelled'
        )),
    CONSTRAINT rd_agent_docs_organized_payload_object
        CHECK (organized_payload IS NULL OR jsonb_typeof(organized_payload) = 'object'),
    CONSTRAINT ck_rd_agent_docs_original_parts
        CHECK (
            files_count BETWEEN 1 AND 10
            AND CASE
                WHEN jsonb_typeof(original_parts) = 'array' THEN
                    (files_count = 1 AND jsonb_array_length(original_parts) = 0)
                    OR (files_count > 1 AND jsonb_array_length(original_parts) = files_count)
                ELSE FALSE
            END
        ),
    FOREIGN KEY (tenant_id, user_id, parse_job_id)
        REFERENCES alpha_factor_jobs (tenant_id, user_id, job_id),
    FOREIGN KEY (tenant_id, user_id, organize_job_id)
        REFERENCES alpha_factor_jobs (tenant_id, user_id, job_id)
);

CREATE INDEX IF NOT EXISTS idx_rd_agent_docs_owner_created
    ON rd_agent_docs (tenant_id, user_id, created_at DESC, doc_id);
CREATE INDEX IF NOT EXISTS idx_rd_agent_docs_owner_status
    ON rd_agent_docs (tenant_id, user_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_rd_agent_docs_owner_sha_status
    ON rd_agent_docs (tenant_id, user_id, sha256, status);
CREATE INDEX IF NOT EXISTS idx_rd_agent_docs_owner_parse_job
    ON rd_agent_docs (tenant_id,user_id,parse_job_id)
    WHERE parse_job_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_rd_agent_docs_owner_organize_job
    ON rd_agent_docs (tenant_id,user_id,organize_job_id)
    WHERE organize_job_id IS NOT NULL;

-- v1.0.49：OSS rd_agent_factors 兼容列（归档标记；enterprise SQL 直接复用）。
ALTER TABLE rd_agent_factors ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ;
ALTER TABLE rd_agent_factors ADD COLUMN IF NOT EXISTS archive_reason TEXT;

-- ---------------------------------------------------------------------------
-- D. 因子修订（v1.0.38 原文；存量修订版回填 DML 省略，见文件头）。
--    指向 rd_agent_factors 的外键按文件头说明省略。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS rd_agent_factor_revisions (
    tenant_id TEXT NOT NULL CHECK (btrim(tenant_id) <> ''),
    user_id TEXT NOT NULL CHECK (btrim(user_id) <> ''),
    factor_id TEXT NOT NULL,
    revision_no BIGINT NOT NULL CHECK (revision_no > 0),
    factor_name TEXT NOT NULL,
    factor_code TEXT NOT NULL CHECK (btrim(factor_code) <> ''),
    factor_formulation TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id,user_id,factor_id,revision_no)
);

CREATE INDEX IF NOT EXISTS rd_agent_factor_revisions_history
    ON rd_agent_factor_revisions
        (tenant_id,user_id,factor_id,revision_no DESC);

CREATE OR REPLACE FUNCTION capture_rd_agent_factor_revision()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
DECLARE next_revision BIGINT;
BEGIN
    IF NEW.factor_code IS NULL OR btrim(NEW.factor_code) = '' THEN
        RETURN NEW;
    END IF;
    IF TG_OP = 'UPDATE' AND NEW.factor_code IS DISTINCT FROM OLD.factor_code THEN
        RETURN NEW;
    END IF;
    IF NEW.tenant_id IS NULL OR btrim(NEW.tenant_id) = ''
       OR NEW.user_id IS NULL OR btrim(NEW.user_id) = '' THEN
        RAISE EXCEPTION 'factor source revisions require tenant and user ownership';
    END IF;
    SELECT COALESCE(MAX(revision_no),0) + 1 INTO next_revision
    FROM rd_agent_factor_revisions
    WHERE tenant_id=NEW.tenant_id AND user_id=NEW.user_id
      AND factor_id=NEW.factor_id;
    INSERT INTO rd_agent_factor_revisions
        (tenant_id,user_id,factor_id,revision_no,factor_name,factor_code,factor_formulation)
    VALUES
        (NEW.tenant_id,NEW.user_id,NEW.factor_id,next_revision,NEW.factor_name,
         NEW.factor_code,NEW.factor_formulation);
    RETURN NEW;
END
$function$;

DROP TRIGGER IF EXISTS rd_agent_factor_revision_capture ON rd_agent_factors;
CREATE TRIGGER rd_agent_factor_revision_capture
AFTER INSERT OR UPDATE OF factor_code ON rd_agent_factors
FOR EACH ROW EXECUTE FUNCTION capture_rd_agent_factor_revision();

CREATE OR REPLACE FUNCTION reject_rd_agent_factor_revision_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    RAISE EXCEPTION 'factor revisions are immutable';
END
$function$;

DROP TRIGGER IF EXISTS rd_agent_factor_revision_immutable ON rd_agent_factor_revisions;
CREATE TRIGGER rd_agent_factor_revision_immutable
BEFORE UPDATE OR DELETE ON rd_agent_factor_revisions
FOR EACH ROW EXECUTE FUNCTION reject_rd_agent_factor_revision_mutation();

-- ---------------------------------------------------------------------------
-- E. 验证证据（v1.0.33 原文；指向 rd_agent_factors 的外键按文件头说明省略；
--    企业版 DO 块中的 rd_agent_factors 建唯一索引语句省略，见文件头）。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS alpha_factor_validation_attempts (
    tenant_id TEXT NOT NULL CHECK (btrim(tenant_id) <> ''),
    user_id TEXT NOT NULL CHECK (btrim(user_id) <> ''),
    factor_id TEXT NOT NULL,
    validation_id TEXT NOT NULL CHECK (btrim(validation_id) <> ''),
    job_id TEXT NOT NULL,
    execution_generation BIGINT NOT NULL CHECK (execution_generation > 0),
    status TEXT NOT NULL CHECK (status IN ('completed', 'failed')),
    validation_context JSONB NOT NULL
        CHECK (jsonb_typeof(validation_context) = 'object'),
    result_snapshot JSONB CHECK (
        result_snapshot IS NULL OR jsonb_typeof(result_snapshot) = 'object'
    ),
    error_code TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, user_id, validation_id),
    UNIQUE (tenant_id, user_id, factor_id, validation_id),
    UNIQUE (tenant_id, user_id, job_id, factor_id),
    FOREIGN KEY (tenant_id, user_id, job_id)
        REFERENCES alpha_factor_jobs (tenant_id, user_id, job_id)
);

CREATE INDEX IF NOT EXISTS alpha_factor_validation_attempts_history
    ON alpha_factor_validation_attempts
    (tenant_id, user_id, factor_id, created_at DESC, validation_id DESC);

CREATE TABLE IF NOT EXISTS alpha_factor_validation_latest (
    tenant_id TEXT NOT NULL CHECK (btrim(tenant_id) <> ''),
    user_id TEXT NOT NULL CHECK (btrim(user_id) <> ''),
    factor_id TEXT NOT NULL,
    validation_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    submitted_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (tenant_id, user_id, factor_id),
    FOREIGN KEY (tenant_id, user_id, factor_id, validation_id)
        REFERENCES alpha_factor_validation_attempts
            (tenant_id, user_id, factor_id, validation_id),
    FOREIGN KEY (tenant_id, user_id, job_id, factor_id)
        REFERENCES alpha_factor_validation_attempts
            (tenant_id, user_id, job_id, factor_id)
);

-- ---------------------------------------------------------------------------
-- F. 因子池（v1.0.34 原文 + v1.0.35 的 market = 'a_share' 约束内联；
--    指向 rd_agent_factors 的外键按文件头说明省略）。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS rd_agent_factor_pool (
    tenant_id TEXT NOT NULL CHECK (btrim(tenant_id) <> ''),
    user_id TEXT NOT NULL CHECK (btrim(user_id) <> ''),
    factor_id TEXT NOT NULL,
    market TEXT NOT NULL DEFAULT 'a_share',
    universe TEXT NOT NULL DEFAULT '',
    pool_score DOUBLE PRECISION,
    novelty DOUBLE PRECISION,
    max_pool_corr DOUBLE PRECISION,
    max_pool_corr_with TEXT,
    diversity_contrib DOUBLE PRECISION,
    times_retrieved INTEGER NOT NULL DEFAULT 0 CHECK (times_retrieved >= 0),
    last_retrieved_at TIMESTAMPTZ,
    panel_ref TEXT,
    panel_version TEXT,
    extra JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(extra) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    archived_at TIMESTAMPTZ,
    PRIMARY KEY (tenant_id, user_id, factor_id, market, universe),
    CONSTRAINT rd_agent_factor_pool_a_share_only
        CHECK (market = 'a_share')
);

CREATE INDEX IF NOT EXISTS idx_rd_factor_pool_owner_scope
    ON rd_agent_factor_pool
        (tenant_id, user_id, market, universe, updated_at DESC);

-- 因子血缘边（v1.0.35 原文；指向 rd_agent_factors 的两处外键省略，
-- 指向 factor_pool 的两处外键保留）。
CREATE TABLE IF NOT EXISTS rd_agent_factor_edges (
    tenant_id TEXT NOT NULL CHECK (btrim(tenant_id) <> ''),
    user_id TEXT NOT NULL CHECK (btrim(user_id) <> ''),
    market TEXT NOT NULL DEFAULT 'a_share',
    universe TEXT NOT NULL DEFAULT '',
    src_factor_id TEXT NOT NULL,
    dst_factor_id TEXT NOT NULL,
    relation TEXT NOT NULL CHECK (
        relation IN ('same_task', 'similar_to', 'correlated_with')
    ),
    method TEXT NOT NULL CHECK (method IN ('formula', 'value', 'task_round')),
    weight DOUBLE PRECISION CHECK (weight IS NULL OR (weight >= 0 AND weight <= 1)),
    extra JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(extra) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (src_factor_id < dst_factor_id),
    PRIMARY KEY (
        tenant_id, user_id, market, universe,
        src_factor_id, dst_factor_id, relation, method
    ),
    FOREIGN KEY (tenant_id, user_id, src_factor_id, market, universe)
        REFERENCES rd_agent_factor_pool
            (tenant_id, user_id, factor_id, market, universe)
        ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, user_id, dst_factor_id, market, universe)
        REFERENCES rd_agent_factor_pool
            (tenant_id, user_id, factor_id, market, universe)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_rd_factor_edges_owner_scope
    ON rd_agent_factor_edges
        (tenant_id, user_id, market, universe, relation, method);

-- ---------------------------------------------------------------------------
-- G. 因子组合（v1.0.45 原文）。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS alpha_factor_combos (
    tenant_id TEXT NOT NULL CHECK (btrim(tenant_id) <> ''),
    user_id TEXT NOT NULL CHECK (btrim(user_id) <> ''),
    combo_id TEXT NOT NULL CHECK (btrim(combo_id) <> ''),
    job_id TEXT NOT NULL CHECK (btrim(job_id) <> ''),
    market TEXT NOT NULL CHECK (market = 'a_share'),
    universe TEXT NOT NULL CHECK (btrim(universe) <> ''),
    name TEXT NOT NULL DEFAULT '' CHECK (length(name) <= 128),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, user_id, combo_id),
    UNIQUE (tenant_id, user_id, job_id),
    FOREIGN KEY (tenant_id, user_id, job_id)
        REFERENCES alpha_factor_jobs (tenant_id, user_id, job_id)
);

CREATE TABLE IF NOT EXISTS alpha_factor_combo_members (
    tenant_id TEXT NOT NULL CHECK (btrim(tenant_id) <> ''),
    user_id TEXT NOT NULL CHECK (btrim(user_id) <> ''),
    combo_id TEXT NOT NULL,
    factor_id TEXT NOT NULL,
    factor_revision_no BIGINT NOT NULL CHECK (factor_revision_no > 0),
    panel_version TEXT NOT NULL CHECK (panel_version ~ '^[0-9a-f]{64}$'),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    PRIMARY KEY (tenant_id, user_id, combo_id, factor_id),
    UNIQUE (tenant_id, user_id, combo_id, ordinal),
    FOREIGN KEY (tenant_id, user_id, combo_id)
        REFERENCES alpha_factor_combos (tenant_id, user_id, combo_id)
        ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, user_id, factor_id, factor_revision_no)
        REFERENCES rd_agent_factor_revisions
            (tenant_id, user_id, factor_id, revision_no)
);

CREATE INDEX IF NOT EXISTS idx_alpha_factor_combos_owner_created
    ON alpha_factor_combos (tenant_id, user_id, created_at DESC, combo_id DESC);
CREATE INDEX IF NOT EXISTS idx_alpha_factor_combo_members_factor
    ON alpha_factor_combo_members
        (tenant_id, user_id, factor_id, factor_revision_no);

-- ---------------------------------------------------------------------------
-- H. 生命周期事件（v1.0.46 原文；指向 rd_agent_factors 的外键省略，
--    jobs / revisions 外键保留）+ 追加只读触发器。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS rd_agent_factor_lifecycle_events (
    event_id BIGINT NOT NULL CHECK (event_id > 0),
    tenant_id TEXT NOT NULL CHECK (btrim(tenant_id) <> ''),
    user_id TEXT NOT NULL CHECK (btrim(user_id) <> ''),
    factor_id TEXT NOT NULL CHECK (btrim(factor_id) <> ''),
    event_type TEXT NOT NULL CHECK (event_type IN ('evaluation', 'manual_restore')),
    evaluated_at TIMESTAMPTZ NOT NULL,
    factor_revision_no BIGINT CHECK (factor_revision_no IS NULL OR factor_revision_no > 0),
    evidence_job_id TEXT,
    event_snapshot JSONB NOT NULL CHECK (jsonb_typeof(event_snapshot) = 'object'),
    FOREIGN KEY (tenant_id, user_id, evidence_job_id)
        REFERENCES alpha_factor_jobs (tenant_id, user_id, job_id),
    FOREIGN KEY (tenant_id, user_id, factor_id, factor_revision_no)
        REFERENCES rd_agent_factor_revisions
            (tenant_id, user_id, factor_id, revision_no),
    PRIMARY KEY (tenant_id, user_id, factor_id, event_id)
);

CREATE INDEX IF NOT EXISTS idx_rd_agent_factor_lifecycle_owner_history
    ON rd_agent_factor_lifecycle_events
        (tenant_id, user_id, factor_id, evaluated_at DESC, event_id DESC);

CREATE OR REPLACE FUNCTION reject_rd_agent_factor_lifecycle_event_mutation()
RETURNS trigger LANGUAGE plpgsql AS $function$
BEGIN
    RAISE EXCEPTION 'factor lifecycle history is append-only';
END;
$function$;

DROP TRIGGER IF EXISTS rd_agent_factor_lifecycle_events_immutable ON rd_agent_factor_lifecycle_events;
CREATE TRIGGER rd_agent_factor_lifecycle_events_immutable
BEFORE UPDATE OR DELETE ON rd_agent_factor_lifecycle_events
FOR EACH ROW EXECUTE FUNCTION reject_rd_agent_factor_lifecycle_event_mutation();

-- 可见性事件（v1.0.49 原文；指向 rd_agent_factors 的外键按文件头说明省略）。
CREATE TABLE IF NOT EXISTS alpha_factor_visibility_events (
    tenant_id TEXT NOT NULL CHECK (btrim(tenant_id) <> ''),
    user_id TEXT NOT NULL CHECK (btrim(user_id) <> ''),
    factor_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('archived','restored')),
    reason TEXT NOT NULL CHECK (length(reason) <= 500),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id,user_id,factor_id,event_id)
);

-- ---------------------------------------------------------------------------
-- I. 统计变体（v1.0.50 原文）。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS alpha_variant_contexts (
    tenant_id TEXT NOT NULL CHECK (btrim(tenant_id) <> ''),
    user_id TEXT NOT NULL CHECK (btrim(user_id) <> ''),
    context_id TEXT NOT NULL CHECK (context_id ~ '^[0-9a-f]{64}$'),
    universe TEXT NOT NULL CHECK (btrim(universe) <> ''),
    scope_json JSONB NOT NULL CHECK (jsonb_typeof(scope_json)='object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id,user_id,context_id)
);
CREATE INDEX IF NOT EXISTS alpha_variant_contexts_owner_pool
    ON alpha_variant_contexts (tenant_id,user_id,universe,updated_at DESC,context_id);

CREATE TABLE IF NOT EXISTS alpha_variant_observations (
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    context_id TEXT NOT NULL,
    variant_key TEXT NOT NULL,
    task_id TEXT NOT NULL,
    baseline_key TEXT NOT NULL,
    best_horizon INT CHECK (best_horizon BETWEEN 1 AND 250),
    horizons_json JSONB NOT NULL CHECK (
        jsonb_typeof(horizons_json)='array' AND jsonb_array_length(horizons_json)<=8
    ),
    observed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id,user_id,context_id,variant_key,task_id),
    FOREIGN KEY (tenant_id,user_id,context_id)
        REFERENCES alpha_variant_contexts (tenant_id,user_id,context_id),
    FOREIGN KEY (tenant_id,user_id,task_id)
        REFERENCES alpha_mining_task_results (tenant_id,user_id,task_id)
);
CREATE INDEX IF NOT EXISTS alpha_variant_observations_recent
    ON alpha_variant_observations (tenant_id,user_id,context_id,variant_key,observed_at DESC,task_id);

CREATE TABLE IF NOT EXISTS alpha_variant_states (
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    context_id TEXT NOT NULL,
    variant_key TEXT NOT NULL,
    evidence_task_id TEXT NOT NULL,
    cooled_since TIMESTAMPTZ,
    cooled_until TIMESTAMPTZ,
    reset_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id,user_id,context_id,variant_key),
    CHECK ((cooled_since IS NULL AND cooled_until IS NULL)
        OR (cooled_since IS NOT NULL AND cooled_until IS NOT NULL AND cooled_until > cooled_since)),
    FOREIGN KEY (tenant_id,user_id,context_id,variant_key,evidence_task_id)
        REFERENCES alpha_variant_observations (tenant_id,user_id,context_id,variant_key,task_id)
);

CREATE TABLE IF NOT EXISTS alpha_variant_reset_events (
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    context_id TEXT NOT NULL,
    variant_key TEXT NOT NULL,
    event_id TEXT NOT NULL,
    evidence_task_id TEXT NOT NULL,
    reason TEXT NOT NULL CHECK (length(btrim(reason)) BETWEEN 1 AND 500),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id,user_id,context_id,variant_key,event_id),
    FOREIGN KEY (tenant_id,user_id,context_id,variant_key,evidence_task_id)
        REFERENCES alpha_variant_observations (tenant_id,user_id,context_id,variant_key,task_id)
);

-- ---------------------------------------------------------------------------
-- J. 任务产物清理回执（v1.0.51 原文）。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS alpha_task_artifact_cleanups (
    tenant_id TEXT NOT NULL CHECK (btrim(tenant_id) <> ''),
    user_id TEXT NOT NULL CHECK (btrim(user_id) <> ''),
    task_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('pending', 'deleting', 'deleted')),
    root_identity JSONB NOT NULL CHECK (jsonb_typeof(root_identity) = 'object'),
    removed_bytes BIGINT NOT NULL DEFAULT 0 CHECK (removed_bytes >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at TIMESTAMPTZ,
    PRIMARY KEY (tenant_id, user_id, task_id),
    FOREIGN KEY (tenant_id,user_id,task_id)
        REFERENCES alpha_mining_tasks (tenant_id,user_id,task_id),
    FOREIGN KEY (tenant_id,user_id,job_id)
        REFERENCES alpha_factor_jobs (tenant_id,user_id,job_id)
);

-- ---------------------------------------------------------------------------
-- K. 恢复来源（v1.0.52 原文；指向 rd_agent_factors 的外键省略，
--    jobs / revisions 外键保留）。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS alpha_factor_recovery_sources (
    tenant_id TEXT NOT NULL CHECK (btrim(tenant_id) <> ''),
    user_id TEXT NOT NULL CHECK (btrim(user_id) <> ''),
    job_id TEXT NOT NULL,
    factor_id TEXT NOT NULL,
    execution_generation BIGINT NOT NULL CHECK (execution_generation > 0),
    revision_no BIGINT CHECK (revision_no > 0),
    input_fingerprint TEXT NOT NULL CHECK (length(input_fingerprint) = 64),
    code_sha256 TEXT CHECK (length(code_sha256) = 64),
    prompt_version TEXT NOT NULL,
    model TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('generating','code_saved','completed','failed')),
    result_snapshot JSONB CHECK (result_snapshot IS NULL OR jsonb_typeof(result_snapshot) = 'object'),
    error_message TEXT CHECK (error_message IS NULL OR length(error_message) <= 2000),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at TIMESTAMPTZ,
    CHECK (status NOT IN ('code_saved','completed') OR (revision_no IS NOT NULL AND code_sha256 IS NOT NULL)),
    PRIMARY KEY (tenant_id,user_id,job_id,factor_id),
    FOREIGN KEY (tenant_id,user_id,job_id) REFERENCES alpha_factor_jobs (tenant_id,user_id,job_id),
    FOREIGN KEY (tenant_id,user_id,factor_id,revision_no)
        REFERENCES rd_agent_factor_revisions (tenant_id,user_id,factor_id,revision_no)
);
CREATE INDEX IF NOT EXISTS alpha_factor_recovery_sources_owner_factor_created
    ON alpha_factor_recovery_sources (tenant_id,user_id,factor_id,created_at DESC,job_id DESC);

-- ---------------------------------------------------------------------------
-- L. 研究请求与模型调用（v1.0.53 原文；
--    指向 resource_task_leases 的外键按文件头说明省略）。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS alpha_research_requests (
    tenant_id TEXT NOT NULL CHECK (btrim(tenant_id) <> ''),
    user_id TEXT NOT NULL CHECK (btrim(user_id) <> ''),
    request_id TEXT NOT NULL,
    request_key TEXT NOT NULL CHECK (length(request_key) BETWEEN 1 AND 128),
    fingerprint TEXT NOT NULL CHECK (length(fingerprint) = 64),
    kind TEXT NOT NULL CHECK (kind IN ('explain','decompose')),
    lease_pool TEXT NOT NULL DEFAULT 'alpha_research_llm' CHECK (lease_pool='alpha_research_llm'),
    status TEXT NOT NULL CHECK (status IN ('queued','running','stopping','completed','failed','cancelled')),
    call_count INTEGER NOT NULL DEFAULT 0 CHECK (call_count BETWEEN 0 AND 2),
    request_snapshot JSONB NOT NULL CHECK (jsonb_typeof(request_snapshot)='object'),
    controller_json JSONB NOT NULL CHECK (jsonb_typeof(controller_json)='object'),
    result_snapshot JSONB CHECK (result_snapshot IS NULL OR jsonb_typeof(result_snapshot)='object'),
    error_code TEXT,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    CHECK (status <> 'completed' OR result_snapshot IS NOT NULL),
    CHECK (status NOT IN ('completed','failed','cancelled') OR completed_at IS NOT NULL),
    PRIMARY KEY (tenant_id,user_id,request_id),
    UNIQUE (tenant_id,user_id,request_key)
);
CREATE INDEX IF NOT EXISTS alpha_research_requests_owner_created
    ON alpha_research_requests (tenant_id,user_id,created_at DESC,request_id DESC);

CREATE TABLE IF NOT EXISTS alpha_research_llm_calls (
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal > 0 AND ordinal <= 2),
    model TEXT NOT NULL,
    prompt_sha256 TEXT NOT NULL CHECK (length(prompt_sha256) = 64),
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id,user_id,request_id,ordinal),
    FOREIGN KEY (tenant_id,user_id,request_id)
        REFERENCES alpha_research_requests (tenant_id,user_id,request_id)
);
CREATE INDEX IF NOT EXISTS alpha_research_llm_calls_day ON alpha_research_llm_calls (started_at);
CREATE INDEX IF NOT EXISTS alpha_research_llm_calls_owner_day ON alpha_research_llm_calls (tenant_id,user_id,started_at);

CREATE OR REPLACE FUNCTION qm_research_request_guard() RETURNS trigger LANGUAGE plpgsql AS $guard$
BEGIN
    IF OLD.status IN ('completed','failed','cancelled') AND NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION 'terminal research requests are immutable';
    END IF;
    IF ROW(NEW.tenant_id,NEW.user_id,NEW.request_id,NEW.request_key,NEW.fingerprint,NEW.kind,NEW.lease_pool,NEW.request_snapshot,NEW.controller_json,NEW.created_at)
       IS DISTINCT FROM ROW(OLD.tenant_id,OLD.user_id,OLD.request_id,OLD.request_key,OLD.fingerprint,OLD.kind,OLD.lease_pool,OLD.request_snapshot,OLD.controller_json,OLD.created_at)
       OR NEW.call_count < OLD.call_count THEN
        RAISE EXCEPTION 'research input, ownership and call counters cannot be rewritten';
    END IF;
    RETURN NEW;
END;
$guard$;
DROP TRIGGER IF EXISTS qm_research_request_guard ON alpha_research_requests;
CREATE TRIGGER qm_research_request_guard BEFORE UPDATE ON alpha_research_requests
    FOR EACH ROW EXECUTE FUNCTION qm_research_request_guard();

-- ---------------------------------------------------------------------------
-- M. 方向批次（v1.0.55 原文）+ 物化结果（v1.0.56 原文；
--    指向 rd_agent_factors 的外键省略，jobs 外键保留）。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS alpha_direction_batches (
    tenant_id TEXT NOT NULL CHECK (btrim(tenant_id) <> ''),
    user_id TEXT NOT NULL CHECK (btrim(user_id) <> ''),
    batch_id TEXT NOT NULL CHECK (length(batch_id) BETWEEN 1 AND 64 AND batch_id ~ '^[A-Za-z0-9_-]+$'),
    fingerprint TEXT NOT NULL CHECK (length(fingerprint)=64),
    request_snapshot JSONB NOT NULL CHECK (jsonb_typeof(request_snapshot)='object' AND octet_length(request_snapshot::text)<=262144),
    selection_snapshot JSONB NOT NULL CHECK (jsonb_typeof(selection_snapshot)='array' AND jsonb_array_length(selection_snapshot) BETWEEN 1 AND 10 AND octet_length(selection_snapshot::text)<=2097152),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id,user_id,batch_id)
);
CREATE INDEX IF NOT EXISTS alpha_direction_batches_owner_created
    ON alpha_direction_batches (tenant_id,user_id,created_at DESC,batch_id DESC);
CREATE OR REPLACE FUNCTION qm_direction_batch_immutable() RETURNS trigger LANGUAGE plpgsql AS $guard$
BEGIN
    IF NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION 'direction batch ownership, request and first selection are immutable';
    END IF;
    RETURN NEW;
END;
$guard$;
DROP TRIGGER IF EXISTS qm_direction_batch_immutable ON alpha_direction_batches;
CREATE TRIGGER qm_direction_batch_immutable BEFORE UPDATE ON alpha_direction_batches
    FOR EACH ROW EXECUTE FUNCTION qm_direction_batch_immutable();

CREATE TABLE IF NOT EXISTS alpha_factor_materializations (
    tenant_id TEXT NOT NULL CHECK (btrim(tenant_id) <> ''),
    user_id TEXT NOT NULL CHECK (btrim(user_id) <> ''),
    job_id TEXT NOT NULL,
    factor_id TEXT NOT NULL,
    generation_id TEXT NOT NULL CHECK (generation_id ~ '^[A-Za-z0-9_-]{1,64}$'),
    feature_key TEXT NOT NULL CHECK (feature_key ~ '^[A-Za-z][A-Za-z0-9_]{0,127}$'),
    direction TEXT NOT NULL CHECK (length(direction) <= 2000),
    status TEXT NOT NULL CHECK (status IN ('materialized','rejected_duplicate')),
    corr REAL CHECK (corr IS NULL OR (corr >= 0 AND corr <= 1)),
    corr_against TEXT CHECK (corr_against IS NULL OR length(corr_against) <= 128),
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(evidence) = 'object'
               AND octet_length(evidence::text) <= 16384),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id,user_id,job_id),
    FOREIGN KEY (tenant_id,user_id,job_id)
        REFERENCES alpha_factor_jobs (tenant_id,user_id,job_id)
);
CREATE INDEX IF NOT EXISTS alpha_factor_materializations_owner_created
    ON alpha_factor_materializations (tenant_id,user_id,created_at DESC);
CREATE OR REPLACE FUNCTION qm_factor_materialization_immutable()
RETURNS trigger LANGUAGE plpgsql AS $guard$
BEGIN
    RAISE EXCEPTION 'materialization outcomes are append-only evidence';
END;
$guard$
;
DROP TRIGGER IF EXISTS qm_factor_materialization_immutable
    ON alpha_factor_materializations;
CREATE TRIGGER qm_factor_materialization_immutable
    BEFORE UPDATE ON alpha_factor_materializations
    FOR EACH ROW EXECUTE FUNCTION qm_factor_materialization_immutable();

-- ---------------------------------------------------------------------------
-- N. 论文库（v1.0.60 原文；建表前置所有者列守卫省略——新库无存量表，
--    存量库若有缺列旧表则由守卫逻辑交人工处理，见企业版原文）。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS user_papers (
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    paper_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    size_bytes BIGINT NOT NULL CHECK (size_bytes >= 0),
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
    storage_key TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, user_id, paper_id)
);

CREATE INDEX IF NOT EXISTS idx_user_papers_owner_created
    ON user_papers (tenant_id, user_id, created_at DESC, paper_id);

-- ---------------------------------------------------------------------------
-- O. 挖掘物化 outbox（v1.0.62 原文；指向 rd_agent_factors 的外键省略，
--    tasks / jobs 外键保留）+ 研究流水线（v1.0.65 原文）。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS alpha_mining_materialization_outbox (
    tenant_id TEXT NOT NULL CHECK (btrim(tenant_id) <> ''),
    user_id TEXT NOT NULL CHECK (btrim(user_id) <> ''),
    task_id TEXT NOT NULL,
    factor_id TEXT NOT NULL,
    universe TEXT NOT NULL CHECK (btrim(universe) <> '' AND length(universe) <= 128),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'claimed', 'submitted', 'failed')),
    attempts INT NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    claim_token TEXT,
    claim_until TIMESTAMPTZ,
    factor_job_id TEXT,
    last_error TEXT CHECK (last_error IS NULL OR length(last_error) <= 1000),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, user_id, task_id, factor_id),
    FOREIGN KEY (tenant_id, user_id, task_id)
        REFERENCES alpha_mining_tasks (tenant_id, user_id, task_id),
    FOREIGN KEY (tenant_id, user_id, factor_job_id)
        REFERENCES alpha_factor_jobs (tenant_id, user_id, job_id)
);

CREATE INDEX IF NOT EXISTS alpha_mining_materialization_outbox_dispatch
    ON alpha_mining_materialization_outbox (available_at, created_at)
    WHERE status IN ('pending', 'claimed');

CREATE TABLE IF NOT EXISTS qm_research_pipeline_runs (
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    request_payload JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued','running','stopping','completed','rejected','failed','cancelled')),
    stage TEXT NOT NULL DEFAULT 'mining',
    snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_message TEXT,
    claim_token TEXT NOT NULL DEFAULT '',
    claim_until TIMESTAMPTZ,
    next_check_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id,user_id,run_id),
    UNIQUE (tenant_id,user_id,idempotency_key)
);
CREATE INDEX IF NOT EXISTS qm_research_pipeline_due
    ON qm_research_pipeline_runs (status,next_check_at);
CREATE OR REPLACE FUNCTION qm_research_pipeline_immutable_request()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF ROW(NEW.tenant_id,NEW.user_id,NEW.run_id,NEW.idempotency_key,
           NEW.request_fingerprint,NEW.request_payload)
       IS DISTINCT FROM ROW(OLD.tenant_id,OLD.user_id,OLD.run_id,OLD.idempotency_key,
           OLD.request_fingerprint,OLD.request_payload) THEN
        RAISE EXCEPTION 'Research pipeline request is immutable';
    END IF;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS qm_research_pipeline_request_guard ON qm_research_pipeline_runs;
CREATE TRIGGER qm_research_pipeline_request_guard BEFORE UPDATE ON qm_research_pipeline_runs
    FOR EACH ROW EXECUTE FUNCTION qm_research_pipeline_immutable_request();

-- ---------------------------------------------------------------------------
-- P. 成环外键延迟追加（tasks -> docs；docs 依赖 jobs、jobs 依赖 tasks，
--    建表时序无法内联，故在此统一追加，幂等）。
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'alpha_mining_tasks_source_doc_owner_fk'
    ) THEN
        ALTER TABLE alpha_mining_tasks
            ADD CONSTRAINT alpha_mining_tasks_source_doc_owner_fk
            FOREIGN KEY (tenant_id, user_id, source_doc_id)
            REFERENCES rd_agent_docs (tenant_id, user_id, doc_id);
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS idx_alpha_mining_tasks_owner_source_doc
    ON alpha_mining_tasks (tenant_id, user_id, source_doc_id, created_at DESC)
    WHERE source_doc_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Q. 资源租约池（企业版 data/upgrade_v1.0.25.sql 合并段“原 v1.0.17”逐字下放；
--    两表之中仅 resource_lease_pools 一张，resource_task_leases 仍不在 OSS 建表）。
--    alpha_mining 池种子行 ready=true：AlphaMiningTaskPersistence.create /
--    request_cancel 建池锁要求 SELECT ready ... FOR UPDATE 为真，否则抛
--    LeaseUnavailable。幂等：IF NOT EXISTS + ON CONFLICT 仅覆盖 ready
--   （保留 reconciler 维护的 epoch，避免围栏误伤运行中任务）。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS resource_lease_pools (
    pool TEXT PRIMARY KEY,
    epoch TEXT NOT NULL,
    ready BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
INSERT INTO resource_lease_pools (pool, epoch, ready)
VALUES ('alpha_mining', 'oss-bootstrap', TRUE)
ON CONFLICT (pool) DO UPDATE SET ready = TRUE, updated_at = NOW();

-- resource_task_leases：任务租约（企业版原 v1.0.17，与 pools 同段）。
-- AlphaMiningTaskPersistence.create / request_cancel / queue_position 读写。
CREATE TABLE IF NOT EXISTS resource_task_leases (
    pool TEXT NOT NULL REFERENCES resource_lease_pools(pool),
    task_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL CHECK (tenant_id <> ''),
    user_id TEXT NOT NULL CHECK (user_id <> ''),
    token TEXT,
    epoch TEXT,
    state TEXT NOT NULL CHECK (state IN (
        'queued', 'reserved', 'running', 'suspect',
        'completed', 'failed', 'cancelled'
    )),
    executor JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    heartbeat_at TIMESTAMPTZ,
    PRIMARY KEY (pool, task_id),
    CHECK (state NOT IN ('reserved', 'running', 'suspect')
        OR (token IS NOT NULL AND epoch IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS resource_task_leases_owner_state
    ON resource_task_leases (pool, tenant_id, user_id, state);

-- R. rd_agent_factors 补 tenant_id（企业版口径对齐，生命周期/版本查询需双维过滤）。
-- 存量行默认 ''，与 save_factor 新写入一致；生命周期端点只查本人新数据。
ALTER TABLE rd_agent_factors ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS idx_rd_agent_factors_tenant_user
    ON rd_agent_factors (tenant_id, user_id);

-- S. 推理作业持久队列（企业版 data/upgrade_v1.0.25.sql 下放）。
-- backend/services/engine/inference/jobs.py 作业泵读写此表，缺表会每轮空转报错。
CREATE TABLE IF NOT EXISTS qm_inference_jobs (
    task_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL CHECK (tenant_id <> ''),
    user_id TEXT NOT NULL CHECK (user_id <> ''),
    idempotency_key TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued','running','cancelling','completed','failed','cancelled')),
    token TEXT,
    request_payload JSONB NOT NULL,
    executor JSONB NOT NULL DEFAULT '{}'::jsonb,
    result_payload JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    heartbeat_at TIMESTAMPTZ,
    UNIQUE (tenant_id,user_id,idempotency_key)
);
CREATE INDEX IF NOT EXISTS qm_inference_jobs_owner_status
    ON qm_inference_jobs (tenant_id,user_id,status,created_at);
CREATE INDEX IF NOT EXISTS qm_inference_jobs_dispatch
    ON qm_inference_jobs (status,created_at,task_id);

COMMIT;
