-- upgrade_v1.1.8: 推理中性化（Neutralization Layer）影子表
--
-- 背景（2026-10-30）：推理链路新增分数级中性化后处理层，默认 shadow（只写影子，
-- 不改 engine_signal_scores、不改排序/下单链路）。设计见
-- docs/推理中性化处理前端_生产级设计_20261030.md。
--
-- 纪律：
--   1. 只允许 CREATE TABLE / CREATE INDEX（全部 IF NOT EXISTS）；
--   2. 不写 UPDATE / DELETE / INSERT / DROP —— 存量数据零影响；
--   3. 启动期 _ensure_upgrade_scripts 会自动重放，必须幂等。
-- ============================================================

BEGIN;

SET LOCAL lock_timeout = '10s';

-- 每个模型的中性化配置（模式/控制变量/宇宙/版本）
CREATE TABLE IF NOT EXISTS qm_neutralization_settings (
    tenant_id       TEXT NOT NULL,
    user_id         TEXT NOT NULL,
    model_id        TEXT NOT NULL,
    market          TEXT NOT NULL DEFAULT 'CN',
    mode            TEXT NOT NULL DEFAULT 'shadow',  -- off | shadow | enforce
    universe_id     TEXT NOT NULL DEFAULT 'cn_all_tradable',
    controls        JSONB NOT NULL DEFAULT '["log_mv","beta60","mom20"]'::jsonb,
    min_n           INTEGER NOT NULL DEFAULT 300,
    winsor_pct      NUMERIC(5,4) NOT NULL DEFAULT 0.01,
    control_lag     INTEGER NOT NULL DEFAULT 0,
    config_version  TEXT NOT NULL DEFAULT '',
    updated_by      TEXT NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, user_id, model_id, market)
);

-- 每次计算的诊断（模型×数据日×口径版本）
CREATE TABLE IF NOT EXISTS qm_neutral_runs (
    neutral_run_id   TEXT PRIMARY KEY,
    tenant_id        TEXT NOT NULL,
    user_id          TEXT NOT NULL,
    model_id         TEXT NOT NULL,
    source_run_id    TEXT,
    data_trade_date  DATE NOT NULL,
    universe_id      TEXT NOT NULL,
    universe_scope   TEXT NOT NULL,              -- full_market | hot_set
    control_lag      INTEGER NOT NULL,
    config_version   TEXT NOT NULL,
    status           TEXT NOT NULL,              -- ok | degraded | failed
    n_symbols        INTEGER NOT NULL DEFAULT 0,
    n_total          INTEGER NOT NULL DEFAULT 0,
    coverage         NUMERIC(6,4) NOT NULL DEFAULT 0,
    r2               DOUBLE PRECISION,
    r2_pass1         DOUBLE PRECISION,
    betas            JSONB,
    condition_number DOUBLE PRECISION,
    exposure         JSONB,
    reason           TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_neutral_runs_model_date
    ON qm_neutral_runs (tenant_id, user_id, model_id, data_trade_date DESC);

CREATE INDEX IF NOT EXISTS idx_neutral_runs_source
    ON qm_neutral_runs (source_run_id);

-- 每个标的的中性化结果（影子）
CREATE TABLE IF NOT EXISTS qm_neutral_scores (
    tenant_id        TEXT NOT NULL,
    user_id          TEXT NOT NULL,
    model_id         TEXT NOT NULL,
    data_trade_date  DATE NOT NULL,
    symbol           TEXT NOT NULL,              -- 规范前缀式 SH600036
    neutral_score    DOUBLE PRECISION,           -- 百分位 0~1；NULL = 不可用
    raw_score        DOUBLE PRECISION NOT NULL,
    log_mv           DOUBLE PRECISION,
    beta60           DOUBLE PRECISION,
    mom20            DOUBLE PRECISION,
    flag             TEXT,                       -- missing_controls | degraded | ok
    config_version   TEXT NOT NULL,
    neutral_run_id   TEXT NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, user_id, model_id, data_trade_date, symbol, config_version)
);

CREATE INDEX IF NOT EXISTS idx_neutral_scores_date
    ON qm_neutral_scores (tenant_id, data_trade_date DESC);

CREATE INDEX IF NOT EXISTS idx_neutral_scores_run
    ON qm_neutral_scores (neutral_run_id);

-- 审计（只增不改）
CREATE TABLE IF NOT EXISTS qm_neutralization_audit (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    model_id    TEXT NOT NULL,
    action      TEXT NOT NULL,                   -- set_mode | recompute | backfill | rollback
    from_value  JSONB,
    to_value    JSONB,
    operator    TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_neutral_audit_model
    ON qm_neutralization_audit (tenant_id, user_id, model_id, created_at DESC);

COMMIT;
