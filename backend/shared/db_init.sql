-- ============================================================
-- QuantMind OSS Database Initialization Script
-- Creates all missing tables for a fresh deployment
-- 含默认管理员 seed（admin / admin123，幂等 ON CONFLICT DO NOTHING）
-- Run: docker exec -i quantmind-db psql -U quantmind -d quantmind < /tmp/quantmind_init.sql
-- ============================================================

-- ========================
-- 1. STRATEGIES (核心表 - 报错的表)
-- ========================
CREATE TABLE IF NOT EXISTS strategies (
    id                SERIAL PRIMARY KEY,
    user_id           INTEGER NOT NULL,
    name              TEXT NOT NULL,
    description       TEXT,
    strategy_type     TEXT DEFAULT 'CUSTOM',
    status            TEXT DEFAULT 'DRAFT',
    config            JSONB DEFAULT '{}',
    parameters        JSONB DEFAULT '{}',
    execution_config  JSONB DEFAULT '{}',
    code              TEXT,
    cos_url           TEXT,
    cos_key           TEXT,
    code_hash         VARCHAR(64),
    file_size         INTEGER DEFAULT 0,
    tags              TEXT[] DEFAULT '{}',
    is_public         BOOLEAN DEFAULT FALSE,
    shared_users      JSONB DEFAULT '[]',
    backtest_count    INTEGER DEFAULT 0,
    view_count        INTEGER DEFAULT 0,
    like_count        INTEGER DEFAULT 0,
    version           INTEGER DEFAULT 1,
    is_verified       BOOLEAN DEFAULT FALSE,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_strategies_user_id ON strategies (user_id);
CREATE INDEX IF NOT EXISTS idx_strategies_status ON strategies (status);
CREATE INDEX IF NOT EXISTS idx_strategies_is_public ON strategies (is_public) WHERE is_public = TRUE;

-- ========================
-- 2. STOCK_DAILY_LATEST (行情数据 - 分区表)
-- ========================
CREATE TABLE IF NOT EXISTS stock_daily_latest (
    trade_date        DATE NOT NULL,
    symbol            VARCHAR(20) NOT NULL,
    open              DOUBLE PRECISION,
    high              DOUBLE PRECISION,
    low               DOUBLE PRECISION,
    close             DOUBLE PRECISION,
    volume            DOUBLE PRECISION,
    amount            DOUBLE PRECISION,
    adj_factor        DOUBLE PRECISION,
    stock_name        VARCHAR(100),
    industry          VARCHAR(100),
    pe_ttm            DOUBLE PRECISION,
    pb                DOUBLE PRECISION,
    bp                DOUBLE PRECISION,
    ep_ttm            DOUBLE PRECISION,
    roe               DOUBLE PRECISION,
    ln_mv_total       DOUBLE PRECISION,
    total_mv          DOUBLE PRECISION,
    float_mv          DOUBLE PRECISION,
    turnover_rate     DOUBLE PRECISION,
    pct_change        DOUBLE PRECISION,
    is_st             INTEGER DEFAULT 0,
    idx_hs300         INTEGER DEFAULT 0,
    idx_zz1000        INTEGER DEFAULT 0,
    idx_chinext       INTEGER DEFAULT 0,
    idx_margin        INTEGER DEFAULT 0,
    idx_all           INTEGER DEFAULT 0,
    ma5               DOUBLE PRECISION,
    ma10              DOUBLE PRECISION,
    ma20              DOUBLE PRECISION,
    ma60              DOUBLE PRECISION,
    ma_gap_5          DOUBLE PRECISION,
    ma_gap_10         DOUBLE PRECISION,
    ma_gap_20         DOUBLE PRECISION,
    return_1d         DOUBLE PRECISION,
    return_3d         DOUBLE PRECISION,
    return_5d         DOUBLE PRECISION,
    return_10d        DOUBLE PRECISION,
    return_20d        DOUBLE PRECISION,
    return_60d        DOUBLE PRECISION,
    vol_std_5         DOUBLE PRECISION,
    vol_std_20        DOUBLE PRECISION,
    vol_std_60        DOUBLE PRECISION,
    vol_atr_14        DOUBLE PRECISION,
    rsi_14            DOUBLE PRECISION,
    rsi_6             DOUBLE PRECISION,
    kdj_k             DOUBLE PRECISION,
    macd_hist         DOUBLE PRECISION,
    beta_20           DOUBLE PRECISION,
    volume_ratio_5    DOUBLE PRECISION,
    volume_ratio_20   DOUBLE PRECISION,
    volume_ma_5       DOUBLE PRECISION,
    amount_ma_5       DOUBLE PRECISION,
    volume_trend_3d   BOOLEAN,
    main_flow         DOUBLE PRECISION,
    flow_net_amount   DOUBLE PRECISION,
    inst_ownership    DOUBLE PRECISION,
    profit_growth     DOUBLE PRECISION,
    listing_market    VARCHAR(20),
    listed_days       INTEGER,
    concept_ai        DOUBLE PRECISION,
    concept_chip      DOUBLE PRECISION,
    concept_new_energy DOUBLE PRECISION,
    concept_pv        DOUBLE PRECISION,
    concept_lithium   DOUBLE PRECISION,
    concept_military  DOUBLE PRECISION,
    concept_medical   DOUBLE PRECISION,
    concept_fintech   DOUBLE PRECISION,
    concept_consumption DOUBLE PRECISION,
    concept_state_owned DOUBLE PRECISION,
    consecutive_limit_up_days INTEGER DEFAULT 0,
    PRIMARY KEY (trade_date, symbol)
);

CREATE INDEX IF NOT EXISTS idx_sdl_symbol ON stock_daily_latest (symbol);
CREATE INDEX IF NOT EXISTS idx_sdl_date ON stock_daily_latest (trade_date DESC);

-- ========================
-- 3. STOCKS (股票主表)
-- NOTE: 兼容 seed_a_share_stocks.py(symbol/name/is_active) 与
--       stream Symbol / engine StockBasicInfo 模型(stock_code/stock_name/status/market_cap)
-- ========================
CREATE TABLE IF NOT EXISTS stocks (
    id              SERIAL PRIMARY KEY,
    symbol          VARCHAR(20) NOT NULL UNIQUE,
    name            VARCHAR(200),
    stock_code      VARCHAR(20),
    stock_name      VARCHAR(200),
    exchange        VARCHAR(20),
    market          VARCHAR(20),
    industry        VARCHAR(200),
    sector          VARCHAR(200),
    market_cap      VARCHAR(50),
    status          INTEGER DEFAULT 1,
    is_active       BOOLEAN DEFAULT TRUE,
    list_date       DATE,
    created_at      TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_stocks_name ON stocks (name);
CREATE INDEX IF NOT EXISTS idx_stocks_exchange ON stocks (exchange);

-- ========================
-- 3.5 USERS (用户主表)
-- NOTE: 列定义与 backend/services/api/user_app/models/user.py 的 User 模型一致
--       （此前缺失导致认证/社区/策略存储等模块查询失败）
-- ========================
CREATE TABLE IF NOT EXISTS users (
    id              SERIAL PRIMARY KEY,
    user_id         VARCHAR(64) NOT NULL UNIQUE,
    tenant_id       VARCHAR(64) NOT NULL,
    username        VARCHAR(128) NOT NULL,
    email           VARCHAR(255),
    phone_number    VARCHAR(32),
    password_hash   VARCHAR(255) NOT NULL,
    is_active       BOOLEAN DEFAULT TRUE,
    is_verified     BOOLEAN DEFAULT FALSE,
    is_admin        BOOLEAN DEFAULT FALSE,
    is_locked       BOOLEAN DEFAULT FALSE,
    last_login_at   TIMESTAMPTZ,
    last_login_ip   VARCHAR(64),
    login_count     INTEGER DEFAULT 0,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    is_deleted      BOOLEAN DEFAULT FALSE,
    deleted_at      TIMESTAMPTZ,
    CONSTRAINT uq_users_tenant_username UNIQUE (tenant_id, username),
    CONSTRAINT uq_users_tenant_email UNIQUE (tenant_id, email),
    CONSTRAINT uq_users_tenant_phone UNIQUE (tenant_id, phone_number)
);
CREATE INDEX IF NOT EXISTS idx_users_tenant_id ON users (tenant_id);
CREATE INDEX IF NOT EXISTS idx_users_user_id ON users (user_id);
CREATE INDEX IF NOT EXISTS idx_users_username ON users (username);
CREATE INDEX IF NOT EXISTS idx_users_is_deleted ON users (is_deleted);


-- ========================
-- 4. STOCK_INDUSTRY
-- ========================
CREATE TABLE IF NOT EXISTS stock_industry (
    id              SERIAL PRIMARY KEY,
    stock_code      VARCHAR(20) NOT NULL,
    industry_name   VARCHAR(200),
    industry_code   VARCHAR(50),
    sector_name     VARCHAR(200),
    sector_code     VARCHAR(50),
    concept_tags    TEXT,
    created_at      TIMESTAMP DEFAULT NOW(),
    updated_at      TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_stock_industry_code ON stock_industry (stock_code);

-- ========================
-- 5. STOCK_ALIASES (from 2026_05_25 SQL)
-- ========================
CREATE TABLE IF NOT EXISTS stock_aliases (
    id          BIGSERIAL    PRIMARY KEY,
    ticker      VARCHAR(16)  NOT NULL,
    alias       VARCHAR(64)  NOT NULL,
    alias_type  VARCHAR(16)  NOT NULL,
    priority    SMALLINT     NOT NULL DEFAULT 50,
    industry    VARCHAR(64),
    sector      VARCHAR(64),
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE (ticker, alias)
);

CREATE INDEX IF NOT EXISTS idx_stock_aliases_alias ON stock_aliases (alias);
CREATE INDEX IF NOT EXISTS idx_stock_aliases_ticker ON stock_aliases (ticker);

-- ========================
-- 6. NEWS_ARTICLE_ENRICHMENT (from 2026_05_25 SQL)
-- ========================
CREATE TABLE IF NOT EXISTS news_article_enrichment (
    huntly_page_id      BIGINT      PRIMARY KEY,
    tickers             TEXT[]      NOT NULL DEFAULT '{}',
    industries          TEXT[]      NOT NULL DEFAULT '{}',
    event_tags          TEXT[]      NOT NULL DEFAULT '{}',
    sentiment_score     REAL,
    sentiment_label     VARCHAR(16),
    sentiment_confidence REAL,
    enriched_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    model_version       VARCHAR(64) NOT NULL,
    title_hash          BIGINT,
    title               TEXT,
    error               TEXT,
    -- extended columns from migrations
    countries           TEXT[]      NOT NULL DEFAULT '{}',
    regions             TEXT[]      NOT NULL DEFAULT '{}',
    key_terms           TEXT[]      NOT NULL DEFAULT '{}',
    date_entities       TEXT[]      NOT NULL DEFAULT '{}',
    entity_sentiments   JSONB       NOT NULL DEFAULT '{}',
    provinces           TEXT[]      NOT NULL DEFAULT '{}',
    cities              TEXT[]      NOT NULL DEFAULT '{}',
    politicians         TEXT[]      NOT NULL DEFAULT '{}',
    visits              TEXT[]      NOT NULL DEFAULT '{}',
    departments         TEXT[]      NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_news_enrichment_tickers
    ON news_article_enrichment USING GIN (tickers);
CREATE INDEX IF NOT EXISTS idx_news_enrichment_industries
    ON news_article_enrichment USING GIN (industries);
CREATE INDEX IF NOT EXISTS idx_news_enrichment_event_tags
    ON news_article_enrichment USING GIN (event_tags);
CREATE INDEX IF NOT EXISTS idx_news_enrichment_label
    ON news_article_enrichment (sentiment_label, enriched_at DESC);
CREATE INDEX IF NOT EXISTS idx_news_enrichment_score
    ON news_article_enrichment (sentiment_score DESC NULLS LAST);

-- ========================
-- 7. FINANCE_LEXICON (from 2026_05_25 SQL)
-- ========================
CREATE TABLE IF NOT EXISTS finance_lexicon (
    id          BIGSERIAL    PRIMARY KEY,
    term        VARCHAR(64)  NOT NULL,
    kind        VARCHAR(16)  NOT NULL,
    event_tag   VARCHAR(32),
    weight      REAL         NOT NULL DEFAULT 1.0,
    note        TEXT,
    enabled     BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE (term, kind)
);

CREATE INDEX IF NOT EXISTS idx_lexicon_kind_enabled ON finance_lexicon (kind, enabled);

-- ========================
-- 8. STOCK_POOL_FILES
-- ========================
CREATE TABLE IF NOT EXISTS stock_pool_files (
    id              SERIAL PRIMARY KEY,
    tenant_id       VARCHAR(50) DEFAULT 'default',
    user_id         VARCHAR(50) NOT NULL,
    pool_name       VARCHAR(200),
    session_id      VARCHAR(100),
    file_key        VARCHAR(500) NOT NULL,
    file_url        VARCHAR(1000),
    relative_path   VARCHAR(500),
    format          VARCHAR(10) DEFAULT 'csv',
    file_size       INTEGER,
    code_hash       VARCHAR(64),
    stock_count     INTEGER,
    is_active       BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMP DEFAULT NOW(),
    updated_at      TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_spf_user_id ON stock_pool_files (user_id);

-- ========================
-- 9. STRATEGY_LOOP_TASKS
-- ========================
CREATE TABLE IF NOT EXISTS strategy_loop_tasks (
    task_id         TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    tenant_id       TEXT NOT NULL DEFAULT 'default',
    status          TEXT NOT NULL,
    error_message   TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    request_json    JSONB,
    result_json     JSONB
);

-- ========================
-- 10. PIPELINE_RUNS
-- ========================
CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id          TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    tenant_id       TEXT NOT NULL DEFAULT 'default',
    status          TEXT NOT NULL,
    stage           TEXT NOT NULL,
    error_message   TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    request_json    JSONB,
    result_json     JSONB
);

-- ========================
-- 11. ENGINE_FEATURE_RUNS
-- ========================
CREATE TABLE IF NOT EXISTS engine_feature_runs (
    run_id          TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL DEFAULT 'default',
    user_id         TEXT NOT NULL,
    trade_date      DATE NOT NULL,
    model_name      TEXT,
    model_version   TEXT,
    feature_version TEXT,
    feature_dim     INTEGER,
    window_start    TIMESTAMPTZ,
    window_end      TIMESTAMPTZ,
    status          TEXT NOT NULL,
    expected_symbols INTEGER,
    ready_symbols   INTEGER,
    missing_symbols INTEGER,
    source          TEXT,
    checksum        TEXT,
    quality         JSONB,
    error_message   TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ========================
-- 12. ENGINE_SIGNAL_SCORES
-- ========================
CREATE TABLE IF NOT EXISTS engine_signal_scores (
    id              BIGSERIAL PRIMARY KEY,
    run_id          TEXT NOT NULL,
    tenant_id       TEXT NOT NULL,
    user_id         TEXT NOT NULL,
    trade_date      DATE NOT NULL,
    symbol          TEXT NOT NULL,
    model_version   TEXT,
    feature_version TEXT,
    light_score     DOUBLE PRECISION,
    tft_score       DOUBLE PRECISION,
    fusion_score    DOUBLE PRECISION NOT NULL,
    risk_weight     DOUBLE PRECISION DEFAULT 1.0,
    regime          TEXT DEFAULT 'normal',
    score_rank      INTEGER,
    universe_tag    TEXT,
    signal_side     TEXT,
    expected_price  DOUBLE PRECISION,
    quality         JSONB,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (tenant_id, user_id, trade_date, symbol, model_version, feature_version, run_id)
);

CREATE INDEX IF NOT EXISTS idx_ess_run_id ON engine_signal_scores (run_id);
CREATE INDEX IF NOT EXISTS idx_ess_trade_date ON engine_signal_scores (trade_date DESC);

-- ========================
-- 13. ENGINE_DISPATCH_BATCHES
-- ========================
CREATE TABLE IF NOT EXISTS engine_dispatch_batches (
    batch_id            TEXT PRIMARY KEY,
    run_id              TEXT NOT NULL,
    tenant_id           TEXT NOT NULL DEFAULT 'default',
    user_id             TEXT NOT NULL,
    trade_date          DATE NOT NULL,
    strategy_id         TEXT,
    trading_mode        TEXT,
    stage               TEXT NOT NULL,
    stage_updated_at    TIMESTAMPTZ,
    total_signals       INTEGER,
    dispatched_signals  INTEGER,
    acked_signals       INTEGER,
    order_submitted_count INTEGER,
    order_filled_count  INTEGER,
    failed_count        INTEGER,
    trace_id            TEXT,
    last_error          TEXT,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

-- ========================
-- 14. ENGINE_DISPATCH_ITEMS
-- ========================
CREATE TABLE IF NOT EXISTS engine_dispatch_items (
    id                  BIGSERIAL PRIMARY KEY,
    batch_id            TEXT NOT NULL,
    run_id              TEXT NOT NULL,
    signal_id           TEXT,
    client_order_id     TEXT UNIQUE,
    tenant_id           TEXT NOT NULL DEFAULT 'default',
    user_id             TEXT NOT NULL,
    trade_date          DATE NOT NULL,
    symbol              TEXT NOT NULL,
    action              TEXT NOT NULL,
    quantity            DOUBLE PRECISION,
    price               DOUBLE PRECISION,
    score               DOUBLE PRECISION,
    dispatch_status     TEXT NOT NULL,
    order_id            UUID,
    exchange_order_id   TEXT,
    exchange_trade_id   TEXT,
    exec_message        TEXT,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_edi_batch_id ON engine_dispatch_items (batch_id);
CREATE INDEX IF NOT EXISTS idx_edi_symbol ON engine_dispatch_items (symbol);

-- ========================
-- 15. QM_RESEARCH_CANDIDATE_SNAPSHOT
-- ========================
CREATE TABLE IF NOT EXISTS qm_research_candidate_snapshot (
    id                      BIGSERIAL PRIMARY KEY,
    tenant_id               TEXT NOT NULL,
    user_id                 TEXT NOT NULL,
    run_id                  TEXT NOT NULL,
    model_id                TEXT,
    data_trade_date         DATE,
    prediction_trade_date   DATE,
    symbol                  TEXT NOT NULL,
    fusion_score            DOUBLE PRECISION,
    score_rank              INTEGER,
    signal_side             TEXT,
    expected_price          DOUBLE PRECISION,
    universe_tag            TEXT,
    confidence_level        TEXT,
    thesis_summary          TEXT,
    created_at              TIMESTAMPTZ DEFAULT NOW(),
    updated_at              TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (tenant_id, user_id, run_id, symbol)
);

-- ========================
-- 16. QM_TRADING_AGENTS_HISTORY
-- ========================
CREATE TABLE IF NOT EXISTS qm_trading_agents_history (
    analysis_id     TEXT PRIMARY KEY,
    ticker          TEXT,
    trade_date      TEXT,
    signal          TEXT,
    llm_provider    TEXT,
    deep_think_llm  TEXT,
    quick_think_llm TEXT,
    stage_reports   JSONB,
    final_state     JSONB,
    stats           JSONB,
    elapsed_seconds DOUBLE PRECISION,
    error           TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tah_ticker ON qm_trading_agents_history (ticker);
CREATE INDEX IF NOT EXISTS idx_tah_trade_date ON qm_trading_agents_history (trade_date);

-- ========================
-- 17. QM_USER_WATCHLIST
-- ========================
CREATE TABLE IF NOT EXISTS qm_user_watchlist (
    id                  BIGSERIAL PRIMARY KEY,
    tenant_id           TEXT NOT NULL,
    user_id             TEXT NOT NULL,
    symbol              TEXT NOT NULL,
    stock_name          TEXT,
    added_at            TIMESTAMPTZ DEFAULT NOW(),
    source_run_id       TEXT,
    features_snapshot   JSONB,
    updated_at          TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (tenant_id, user_id, symbol)
);

-- ========================
-- 18. QM_USER_RESEARCH_POOL
-- ========================
CREATE TABLE IF NOT EXISTS qm_user_research_pool (
    id                  BIGSERIAL PRIMARY KEY,
    tenant_id           TEXT NOT NULL,
    user_id             TEXT NOT NULL,
    symbol              TEXT NOT NULL,
    stock_name          TEXT,
    added_at            TIMESTAMPTZ DEFAULT NOW(),
    source_run_id       TEXT,
    status              TEXT,
    model_id            TEXT,
    fusion_score        DOUBLE PRECISION,
    thesis_summary      TEXT,
    features_snapshot   JSONB,
    updated_at          TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (tenant_id, user_id, symbol)
);

-- ========================
-- 19. QM_FEATURE_CATEGORY
-- ========================
CREATE TABLE IF NOT EXISTS qm_feature_category (
    category_id     VARCHAR PRIMARY KEY,
    category_name   VARCHAR,
    sort_order      INTEGER,
    description     TEXT
);

-- ========================
-- 20. QM_FEATURE_DEFINITION
-- ========================
CREATE TABLE IF NOT EXISTS qm_feature_definition (
    feature_id          UUID,
    feature_key         VARCHAR PRIMARY KEY,
    feature_name        VARCHAR,
    formula             TEXT,
    category_id         VARCHAR REFERENCES qm_feature_category (category_id),
    source_table_fields TEXT,
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

-- ========================
-- 21. QM_FEATURE_SET_VERSION
-- ========================
CREATE TABLE IF NOT EXISTS qm_feature_set_version (
    version_id      VARCHAR PRIMARY KEY,
    version_name    VARCHAR,
    status          VARCHAR,
    feature_count   INTEGER,
    effective_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ========================
-- 22. QM_FEATURE_SET_ITEM
-- ========================
CREATE TABLE IF NOT EXISTS qm_feature_set_item (
    id              SERIAL PRIMARY KEY,
    version_id      VARCHAR REFERENCES qm_feature_set_version (version_id),
    category_id     VARCHAR REFERENCES qm_feature_category (category_id),
    feature_key     VARCHAR REFERENCES qm_feature_definition (feature_key),
    order_no        INTEGER,
    enabled         BOOLEAN DEFAULT TRUE,
    UNIQUE (version_id, feature_key)
);

-- ========================
-- 22.1 QUANTDB TRAINING FACTOR CATALOG (direct-read)
-- ========================
CREATE TABLE IF NOT EXISTS qm_quantdb_factor_field (
    market          VARCHAR(16) NOT NULL DEFAULT 'CN',
    dataset_id      VARCHAR(64) NOT NULL,
    column_name     VARCHAR(128) NOT NULL,
    data_type       VARCHAR(64),
    schema_hash     VARCHAR(128) NOT NULL DEFAULT '',
    min_date        DATE,
    max_date        DATE,
    is_present      BOOLEAN NOT NULL DEFAULT TRUE,
    discovered_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (market, dataset_id, column_name)
);

CREATE TABLE IF NOT EXISTS qm_training_factor_catalog_version (
    version_id      VARCHAR(64) PRIMARY KEY,
    version_name    VARCHAR(128) NOT NULL,
    status          VARCHAR(16) NOT NULL DEFAULT 'draft',
    source_dataset  VARCHAR(64),
    created_by      VARCHAR(128),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    published_at    TIMESTAMPTZ,
    CHECK (status IN ('draft', 'published', 'archived'))
);

CREATE TABLE IF NOT EXISTS qm_training_factor_mapping (
    mapping_id       VARCHAR(64) PRIMARY KEY,
    version_id       VARCHAR(64) NOT NULL REFERENCES qm_training_factor_catalog_version(version_id) ON DELETE CASCADE,
    source_dataset   VARCHAR(64) NOT NULL,
    source_column    VARCHAR(128) NOT NULL,
    feature_key      VARCHAR(128) NOT NULL,
    display_name     VARCHAR(256) NOT NULL,
    category_id      VARCHAR(64) NOT NULL,
    category_name    VARCHAR(128) NOT NULL,
    enabled          BOOLEAN NOT NULL DEFAULT TRUE,
    default_selected BOOLEAN NOT NULL DEFAULT FALSE,
    required         BOOLEAN NOT NULL DEFAULT FALSE,
    sort_order       INTEGER NOT NULL DEFAULT 0,
    UNIQUE(version_id, source_dataset, feature_key),
    UNIQUE(version_id, source_dataset, source_column)
);

CREATE INDEX IF NOT EXISTS idx_qm_training_factor_mapping_version
    ON qm_training_factor_mapping(version_id, source_dataset, category_id, sort_order);

-- ========================
-- 23. QM_MARKET_CALENDAR_DAY
-- ========================
CREATE TABLE IF NOT EXISTS qm_market_calendar_day (
    market          VARCHAR(32) NOT NULL,
    trade_date      DATE NOT NULL,
    is_trading_day  BOOLEAN NOT NULL,
    timezone        VARCHAR(64) NOT NULL DEFAULT 'Asia/Shanghai',
    source          VARCHAR(64) NOT NULL DEFAULT 'manual',
    version         VARCHAR(64),
    tenant_id       VARCHAR(64) NOT NULL DEFAULT 'default',
    user_id         VARCHAR(64) NOT NULL DEFAULT '*',
    metadata_json   JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (market, trade_date, tenant_id, user_id)
);

-- ========================
-- 24. QM_MARKET_TRADING_SESSION
-- ========================
CREATE TABLE IF NOT EXISTS qm_market_trading_session (
    market          VARCHAR(32) NOT NULL,
    session_name    VARCHAR(64) NOT NULL,
    start_time      TIME NOT NULL,
    end_time        TIME NOT NULL,
    cross_day       BOOLEAN NOT NULL DEFAULT FALSE,
    trade_date_rule VARCHAR(64) NOT NULL DEFAULT 'TRADE_DATE',
    timezone        VARCHAR(64) NOT NULL DEFAULT 'Asia/Shanghai',
    tenant_id       VARCHAR(64) NOT NULL DEFAULT 'default',
    user_id         VARCHAR(64) NOT NULL DEFAULT '*',
    metadata_json   JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (market, session_name, tenant_id, user_id)
);

-- ========================
-- 25. QM_MARKET_CALENDAR_EXCEPTION
-- ========================
CREATE TABLE IF NOT EXISTS qm_market_calendar_exception (
    id              BIGSERIAL PRIMARY KEY,
    market          VARCHAR(32) NOT NULL,
    trade_date      DATE NOT NULL,
    action          VARCHAR(16) NOT NULL,
    reason          TEXT,
    tenant_id       VARCHAR(64) NOT NULL DEFAULT 'default',
    user_id         VARCHAR(64) NOT NULL DEFAULT '*',
    approved_by     VARCHAR(128),
    metadata_json   JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ========================
-- 26. QM_MARKET_CALENDAR_VERSION
-- ========================
CREATE TABLE IF NOT EXISTS qm_market_calendar_version (
    market          VARCHAR(32) NOT NULL,
    year            INTEGER NOT NULL,
    checksum        VARCHAR(128) NOT NULL,
    status          VARCHAR(32) NOT NULL DEFAULT 'draft',
    source          VARCHAR(64),
    published_at    TIMESTAMPTZ,
    metadata_json   JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (market, year)
);

-- ========================
-- 27. DATA_QUALITY_ALERTS (from 2026_05_24 SQL)
-- ========================
CREATE TABLE IF NOT EXISTS data_quality_alerts (
    id              BIGSERIAL PRIMARY KEY,
    alert_type      VARCHAR(32) NOT NULL,
    severity        VARCHAR(16) NOT NULL,
    market          VARCHAR(8),
    field           VARCHAR(48),
    source          VARCHAR(32),
    symbol          VARCHAR(32),
    trade_date      DATE,
    message         TEXT NOT NULL,
    details         JSONB,
    acknowledged    BOOLEAN NOT NULL DEFAULT FALSE,
    acknowledged_by VARCHAR(64),
    acknowledged_at TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_dqa_created_at ON data_quality_alerts (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_dqa_unack_severity ON data_quality_alerts (acknowledged, severity, created_at DESC) WHERE acknowledged = FALSE;
CREATE INDEX IF NOT EXISTS idx_dqa_market_field ON data_quality_alerts (market, field, created_at DESC);

-- ========================
-- 28. REAL_ACCOUNT_BASELINES
-- ========================
CREATE TABLE IF NOT EXISTS real_account_baselines (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    user_id         TEXT NOT NULL,
    account_id      TEXT NOT NULL,
    initial_equity  DOUBLE PRECISION,
    first_snapshot_at TIMESTAMPTZ,
    source          TEXT DEFAULT 'qmt_bridge_first_report',
    UNIQUE (tenant_id, user_id, account_id)
);

-- ========================
-- 29. TRADING_CALENDAR (simple version from SQL)
-- ========================
CREATE TABLE IF NOT EXISTS trading_calendar (
    market         VARCHAR(8)  NOT NULL,
    trade_date     DATE        NOT NULL,
    is_trading     BOOLEAN     NOT NULL,
    is_half_day    BOOLEAN     NOT NULL DEFAULT FALSE,
    note           TEXT,
    source         VARCHAR(32),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (market, trade_date)
);

CREATE INDEX IF NOT EXISTS idx_trading_calendar_market_trading ON trading_calendar (market, is_trading, trade_date);

-- ========================
-- 30. AI_STRATEGIES (legacy table)
-- ========================
CREATE TABLE IF NOT EXISTS ai_strategies (
    id              SERIAL PRIMARY KEY,
    strategy_id     VARCHAR(64) UNIQUE,
    user_id         VARCHAR(64),
    name            VARCHAR(255),
    description     TEXT,
    market          VARCHAR(32),
    risk_level      VARCHAR(16),
    provider        VARCHAR(32),
    code            TEXT,
    cos_file_key    VARCHAR(500),
    cos_file_url    VARCHAR(1000),
    factors         TEXT,
    risk_controls   TEXT,
    assumptions     TEXT,
    notes           TEXT,
    created_at      TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ai_strategies_user_id ON ai_strategies (user_id);

-- ========================
-- 31. USER_STRATEGIES (legacy table for migration)
-- ========================
CREATE TABLE IF NOT EXISTS user_strategies (
    id              SERIAL PRIMARY KEY,
    user_id         VARCHAR(64) NOT NULL,
    strategy_name   VARCHAR(255),
    description     TEXT,
    conditions      JSONB,
    stock_pool      JSONB,
    position_config JSONB,
    style           VARCHAR(32),
    risk_config     JSONB,
    cos_url         TEXT,
    file_size       INTEGER,
    code_hash       VARCHAR(64),
    qlib_validated  BOOLEAN DEFAULT FALSE,
    validation_result JSONB,
    tags            TEXT[],
    is_public       BOOLEAN DEFAULT FALSE,
    downloads       INTEGER DEFAULT 0,
    created_at      TIMESTAMP DEFAULT NOW(),
    updated_at      TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_us_user_id ON user_strategies (user_id);

-- ========================
-- TRADE ENUMS (needed for orders/trades tables)
-- ========================
DO $$ BEGIN
    -- Create enums if they don't exist
    -- NOTE: values must match backend/services/trade/models/enums.py（小写）
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'orderside') THEN
        CREATE TYPE orderside AS ENUM ('buy', 'sell');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'tradeaction') THEN
        CREATE TYPE tradeaction AS ENUM ('buy_to_open', 'sell_to_close', 'sell_to_open', 'buy_to_close');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'positionside') THEN
        CREATE TYPE positionside AS ENUM ('long', 'short');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'ordertype') THEN
        CREATE TYPE ordertype AS ENUM ('market', 'limit', 'stop', 'stop_limit');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'tradingmode') THEN
        CREATE TYPE tradingmode AS ENUM ('SIMULATION', 'SHADOW', 'REAL');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'orderstatus') THEN
        CREATE TYPE orderstatus AS ENUM ('pending', 'submitted', 'partially_filled', 'filled', 'cancelled', 'rejected', 'expired');
    END IF;
END $$;

-- ========================
-- 32. ORDERS
-- ========================
CREATE TABLE IF NOT EXISTS orders (
    id              SERIAL PRIMARY KEY,
    order_id        UUID NOT NULL UNIQUE,
    tenant_id       VARCHAR(64) NOT NULL DEFAULT 'default',
    user_id         VARCHAR(32) NOT NULL,
    portfolio_id    INTEGER NOT NULL,
    strategy_id     INTEGER,
    symbol          VARCHAR(20) NOT NULL,
    symbol_name     VARCHAR(50),
    side            orderside NOT NULL,
    trade_action    tradeaction,
    position_side   positionside NOT NULL,
    is_margin_trade BOOLEAN NOT NULL DEFAULT FALSE,
    order_type      ordertype NOT NULL,
    trading_mode    tradingmode NOT NULL,
    status          orderstatus NOT NULL,
    quantity        FLOAT NOT NULL,
    filled_quantity FLOAT NOT NULL DEFAULT 0,
    price           FLOAT,
    stop_price      FLOAT,
    average_price   FLOAT,
    order_value     FLOAT NOT NULL DEFAULT 0,
    filled_value    FLOAT NOT NULL DEFAULT 0,
    commission      FLOAT NOT NULL DEFAULT 0,
    submitted_at    TIMESTAMP,
    filled_at       TIMESTAMP,
    cancelled_at    TIMESTAMP,
    expired_at      TIMESTAMP,
    client_order_id VARCHAR(100) UNIQUE,
    exchange_order_id VARCHAR(100),
    remarks         VARCHAR(500),
    version         INTEGER NOT NULL DEFAULT 1,
    created_at      TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMP NOT NULL DEFAULT NOW()
);

-- ========================
-- 33. TRADES
-- ========================
CREATE TABLE IF NOT EXISTS trades (
    id              SERIAL PRIMARY KEY,
    trade_id        UUID NOT NULL UNIQUE,
    order_id        UUID NOT NULL REFERENCES orders (order_id),
    tenant_id       VARCHAR(64) NOT NULL DEFAULT 'default',
    user_id         VARCHAR(32) NOT NULL,
    portfolio_id    INTEGER NOT NULL,
    symbol          VARCHAR(20) NOT NULL,
    symbol_name     VARCHAR(50),
    side            orderside NOT NULL,
    trade_action    tradeaction,
    position_side   positionside NOT NULL,
    is_margin_trade BOOLEAN NOT NULL DEFAULT FALSE,
    trading_mode    tradingmode NOT NULL,
    quantity        FLOAT NOT NULL,
    price           FLOAT NOT NULL,
    trade_value     FLOAT NOT NULL,
    commission      FLOAT NOT NULL DEFAULT 0,
    stamp_duty      FLOAT NOT NULL DEFAULT 0,
    transfer_fee    FLOAT NOT NULL DEFAULT 0,
    total_fee       FLOAT NOT NULL DEFAULT 0,
    executed_at     TIMESTAMP NOT NULL,
    exchange_trade_id VARCHAR(100),
    exchange_name   VARCHAR(50),
    remarks         VARCHAR(500),
    created_at      TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMP NOT NULL DEFAULT NOW()
);

-- ========================
-- 34. PORTFOLIOS
-- ========================
CREATE TABLE IF NOT EXISTS portfolios (
    id                  SERIAL PRIMARY KEY,
    tenant_id           VARCHAR(64) NOT NULL DEFAULT 'default',
    user_id             VARCHAR(32) NOT NULL,
    name                VARCHAR(100) NOT NULL,
    description         TEXT,
    initial_capital     NUMERIC(20, 2) NOT NULL DEFAULT 0,
    current_capital     NUMERIC(20, 2) NOT NULL DEFAULT 0,
    available_cash      NUMERIC(20, 2) NOT NULL DEFAULT 0,
    frozen_cash         NUMERIC(20, 2) NOT NULL DEFAULT 0,
    total_value         NUMERIC(20, 2) NOT NULL DEFAULT 0,
    total_pnl           NUMERIC(20, 2) NOT NULL DEFAULT 0,
    total_return        NUMERIC(10, 4) NOT NULL DEFAULT 0,
    daily_pnl           NUMERIC(20, 2) NOT NULL DEFAULT 0,
    daily_return        NUMERIC(10, 4) NOT NULL DEFAULT 0,
    yesterday_total_value NUMERIC(20, 2) NOT NULL DEFAULT 0,
    max_drawdown        NUMERIC(10, 4) NOT NULL DEFAULT 0,
    sharpe_ratio        NUMERIC(10, 4),
    volatility          NUMERIC(10, 4),
    status              VARCHAR(20) NOT NULL DEFAULT 'ACTIVE',
    trading_mode        tradingmode NOT NULL DEFAULT 'SIMULATION',
    broker_type         VARCHAR(32),
    broker_account_id   VARCHAR(64),
    broker_params       JSONB,
    strategy_id         INTEGER,
    real_trading_id     VARCHAR(50),
    run_status          VARCHAR(20) NOT NULL DEFAULT 'STOPPED',
    is_deleted          BOOLEAN NOT NULL DEFAULT FALSE,
    created_at          TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT check_initial_capital_positive CHECK (initial_capital >= 0),
    CONSTRAINT check_available_cash_positive CHECK (available_cash >= 0)
);

-- ========================
-- 35. POSITIONS
-- ========================
CREATE TABLE IF NOT EXISTS positions (
    id                  SERIAL PRIMARY KEY,
    portfolio_id        INTEGER NOT NULL REFERENCES portfolios (id),
    symbol              VARCHAR(20) NOT NULL,
    symbol_name         VARCHAR(100),
    exchange            VARCHAR(20),
    side                VARCHAR(20) NOT NULL DEFAULT 'LONG',
    quantity            INTEGER NOT NULL DEFAULT 0,
    available_quantity  INTEGER NOT NULL DEFAULT 0,
    frozen_quantity     INTEGER NOT NULL DEFAULT 0,
    avg_cost            NUMERIC(20, 4) NOT NULL DEFAULT 0,
    total_cost          NUMERIC(20, 2) NOT NULL DEFAULT 0,
    current_price       NUMERIC(20, 4) NOT NULL DEFAULT 0,
    market_value        NUMERIC(20, 2) NOT NULL DEFAULT 0,
    unrealized_pnl      NUMERIC(20, 2) NOT NULL DEFAULT 0,
    unrealized_pnl_rate NUMERIC(10, 4) NOT NULL DEFAULT 0,
    realized_pnl        NUMERIC(20, 2) NOT NULL DEFAULT 0,
    weight              NUMERIC(10, 4) NOT NULL DEFAULT 0,
    status              VARCHAR(20) NOT NULL DEFAULT 'OPEN',
    opened_at           TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMP NOT NULL DEFAULT NOW(),
    closed_at           TIMESTAMP,
    CONSTRAINT check_quantity_positive CHECK (quantity >= 0),
    CONSTRAINT check_available_quantity_positive CHECK (available_quantity >= 0)
);

-- ========================
-- 36. POSITION_HISTORY
-- ========================
CREATE TABLE IF NOT EXISTS position_history (
    id              SERIAL PRIMARY KEY,
    position_id     INTEGER NOT NULL REFERENCES positions (id),
    action          VARCHAR(20) NOT NULL,
    quantity_change INTEGER NOT NULL,
    price           NUMERIC(20, 4) NOT NULL,
    amount          NUMERIC(20, 2) NOT NULL,
    quantity_after  INTEGER NOT NULL,
    avg_cost_after  NUMERIC(20, 4) NOT NULL,
    note            TEXT,
    created_at      TIMESTAMP NOT NULL DEFAULT NOW()
);

-- ========================
-- 37. PORTFOLIO_SNAPSHOTS
-- ========================
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id              SERIAL PRIMARY KEY,
    portfolio_id    INTEGER NOT NULL REFERENCES portfolios (id),
    snapshot_date   TIMESTAMP NOT NULL,
    total_value     NUMERIC(20, 2) NOT NULL DEFAULT 0,
    available_cash  NUMERIC(20, 2) NOT NULL DEFAULT 0,
    market_value    NUMERIC(20, 2) NOT NULL DEFAULT 0,
    total_pnl       NUMERIC(20, 2) NOT NULL DEFAULT 0,
    total_return    NUMERIC(10, 4) NOT NULL DEFAULT 0,
    daily_pnl       NUMERIC(20, 2) NOT NULL DEFAULT 0,
    daily_return    NUMERIC(10, 4) NOT NULL DEFAULT 0,
    max_drawdown    NUMERIC(10, 4) NOT NULL DEFAULT 0,
    sharpe_ratio    NUMERIC(10, 4),
    volatility      NUMERIC(10, 4),
    position_count  INTEGER NOT NULL DEFAULT 0,
    is_settlement   BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMP NOT NULL DEFAULT NOW()
);

-- ========================
-- 38. RISK_RULES
-- ========================
CREATE TABLE IF NOT EXISTS risk_rules (
    id              SERIAL PRIMARY KEY,
    rule_name       VARCHAR(100) NOT NULL,
    rule_type       VARCHAR(50) NOT NULL,
    description     VARCHAR(500),
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    parameters      JSONB NOT NULL DEFAULT '{}',
    applies_to_all  BOOLEAN NOT NULL DEFAULT TRUE,
    user_ids        JSONB,
    priority        INTEGER NOT NULL DEFAULT 0,
    created_at      TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMP NOT NULL DEFAULT NOW()
);

-- ========================
-- 39. REAL_ACCOUNT_SNAPSHOTS
-- ========================
CREATE TABLE IF NOT EXISTS real_account_snapshots (
    id              SERIAL PRIMARY KEY,
    tenant_id       VARCHAR(50) NOT NULL DEFAULT 'default',
    user_id         VARCHAR(50) NOT NULL,
    account_id      VARCHAR(64) NOT NULL,
    snapshot_at     TIMESTAMP NOT NULL,
    snapshot_date   DATE NOT NULL,
    snapshot_month  VARCHAR(7) NOT NULL,
    total_asset     FLOAT NOT NULL DEFAULT 0,
    cash            FLOAT NOT NULL DEFAULT 0,
    market_value    FLOAT NOT NULL DEFAULT 0,
    today_pnl_raw   FLOAT NOT NULL DEFAULT 0,
    total_pnl_raw   FLOAT NOT NULL DEFAULT 0,
    floating_pnl_raw FLOAT NOT NULL DEFAULT 0,
    source          VARCHAR(32) NOT NULL DEFAULT 'qmt',
    payload_json    JSONB NOT NULL DEFAULT '{}'
);

-- 账户快照统一展示视图：最新快照 + 日/月/累计基线
--   initial_equity    = 该账户首条快照（累计基线）
--   day_open_equity   = 上一交易日最后一条快照（无则退回当日首条）
--   month_open_equity = 当月首条快照
CREATE OR REPLACE VIEW real_account_snapshot_overview_v AS
 SELECT s.id,
    s.tenant_id,
    s.user_id,
    s.account_id,
    s.snapshot_at,
    s.snapshot_date,
    s.snapshot_month,
    s.total_asset,
    s.cash,
    s.market_value,
    s.today_pnl_raw,
    s.total_pnl_raw,
    s.floating_pnl_raw,
    s.source,
    s.payload_json,
    COALESCE(( SELECT ras.total_asset
           FROM real_account_snapshots ras
          WHERE ((ras.tenant_id)::text = (s.tenant_id)::text AND (ras.user_id)::text = (s.user_id)::text AND (ras.account_id)::text = (s.account_id)::text)
          ORDER BY ras.snapshot_at
         LIMIT 1), s.total_asset) AS initial_equity,
    COALESCE(
        ( SELECT ras.total_asset
           FROM real_account_snapshots ras
          WHERE ((ras.tenant_id)::text = (s.tenant_id)::text AND (ras.user_id)::text = (s.user_id)::text AND (ras.account_id)::text = (s.account_id)::text AND ras.snapshot_date < s.snapshot_date)
          ORDER BY ras.snapshot_at DESC
         LIMIT 1),
        ( SELECT ras.total_asset
           FROM real_account_snapshots ras
          WHERE ((ras.tenant_id)::text = (s.tenant_id)::text AND (ras.user_id)::text = (s.user_id)::text AND (ras.account_id)::text = (s.account_id)::text AND ras.snapshot_date = s.snapshot_date)
          ORDER BY ras.snapshot_at
         LIMIT 1),
        s.total_asset) AS day_open_equity,
    COALESCE(( SELECT ras.total_asset
           FROM real_account_snapshots ras
          WHERE ((ras.tenant_id)::text = (s.tenant_id)::text AND (ras.user_id)::text = (s.user_id)::text AND (ras.account_id)::text = (s.account_id)::text AND (ras.snapshot_month)::text = (s.snapshot_month)::text)
          ORDER BY ras.snapshot_at
         LIMIT 1), s.total_asset) AS month_open_equity
   FROM real_account_snapshots s;

-- ========================
-- 40. REAL_TRADING_PREFLIGHT_SNAPSHOTS
-- ========================
CREATE TABLE IF NOT EXISTS real_trading_preflight_snapshots (
    id                  SERIAL PRIMARY KEY,
    tenant_id           VARCHAR(64) NOT NULL DEFAULT 'default',
    user_id             VARCHAR(64) NOT NULL,
    trading_mode        VARCHAR(16) NOT NULL,
    snapshot_date       DATE NOT NULL,
    ready               BOOLEAN NOT NULL DEFAULT FALSE,
    total_checks        INTEGER NOT NULL DEFAULT 0,
    passed_checks       INTEGER NOT NULL DEFAULT 0,
    required_failed_count INTEGER NOT NULL DEFAULT 0,
    run_count           INTEGER NOT NULL DEFAULT 0,
    failed_required_keys JSONB NOT NULL DEFAULT '[]',
    checks              JSONB NOT NULL DEFAULT '[]',
    source              VARCHAR(32) NOT NULL DEFAULT 'preflight',
    last_checked_at     TIMESTAMP NOT NULL DEFAULT NOW(),
    created_at          TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMP NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, user_id, trading_mode, snapshot_date)
);

-- ========================
-- 41. REAL_ACCOUNT_LEDGER_DAILY_SNAPSHOTS
-- ========================
CREATE TABLE IF NOT EXISTS real_account_ledger_daily_snapshots (
    id              SERIAL PRIMARY KEY,
    tenant_id       VARCHAR(64) NOT NULL DEFAULT 'default',
    user_id         VARCHAR(64) NOT NULL,
    account_id      VARCHAR(64) NOT NULL,
    snapshot_date   DATE NOT NULL,
    last_snapshot_at TIMESTAMP NOT NULL DEFAULT NOW(),
    initial_equity  FLOAT NOT NULL DEFAULT 0,
    day_open_equity FLOAT NOT NULL DEFAULT 0,
    month_open_equity FLOAT NOT NULL DEFAULT 0,
    total_asset     FLOAT NOT NULL DEFAULT 0,
    cash            FLOAT NOT NULL DEFAULT 0,
    market_value    FLOAT NOT NULL DEFAULT 0,
    today_pnl_raw   FLOAT NOT NULL DEFAULT 0,
    monthly_pnl_raw FLOAT NOT NULL DEFAULT 0,
    total_pnl_raw   FLOAT NOT NULL DEFAULT 0,
    floating_pnl_raw FLOAT NOT NULL DEFAULT 0,
    daily_return_pct FLOAT NOT NULL DEFAULT 0,
    total_return_pct FLOAT NOT NULL DEFAULT 0,
    position_count  INTEGER NOT NULL DEFAULT 0,
    source          VARCHAR(32) NOT NULL DEFAULT 'qmt',
    payload_json    JSONB NOT NULL DEFAULT '{}',
    UNIQUE (tenant_id, user_id, account_id, snapshot_date)
);

-- ========================
-- 42. SIM_ORDERS
-- ========================
CREATE TABLE IF NOT EXISTS sim_orders (
    id              SERIAL PRIMARY KEY,
    order_id        UUID NOT NULL UNIQUE,
    tenant_id       VARCHAR(64) NOT NULL DEFAULT 'default',
    user_id         INTEGER NOT NULL,
    portfolio_id    INTEGER NOT NULL,
    strategy_id     INTEGER,
    symbol          VARCHAR(20) NOT NULL,
    side            orderside NOT NULL,
    order_type      ordertype NOT NULL,
    trading_mode    tradingmode NOT NULL DEFAULT 'SIMULATION',
    status          orderstatus NOT NULL,
    quantity        FLOAT NOT NULL,
    filled_quantity FLOAT NOT NULL DEFAULT 0,
    price           FLOAT,
    average_price   FLOAT,
    order_value     FLOAT NOT NULL DEFAULT 0,
    filled_value    FLOAT NOT NULL DEFAULT 0,
    commission      FLOAT NOT NULL DEFAULT 0,
    submitted_at    TIMESTAMPTZ,
    filled_at       TIMESTAMPTZ,
    cancelled_at    TIMESTAMPTZ,
    execution_model VARCHAR(32) NOT NULL DEFAULT 'next_bar_open',
    price_source    VARCHAR(64),
    remarks         VARCHAR(500),
    version         INTEGER NOT NULL DEFAULT 1,
    total_fee       FLOAT NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ========================
-- 43. SIM_TRADES
-- ========================
CREATE TABLE IF NOT EXISTS sim_trades (
    id              SERIAL PRIMARY KEY,
    trade_id        UUID NOT NULL UNIQUE,
    order_id        UUID NOT NULL REFERENCES sim_orders (order_id),
    tenant_id       VARCHAR(64) NOT NULL DEFAULT 'default',
    user_id         INTEGER NOT NULL,
    portfolio_id    INTEGER NOT NULL,
    symbol          VARCHAR(20) NOT NULL,
    side            orderside NOT NULL,
    trading_mode    tradingmode NOT NULL DEFAULT 'SIMULATION',
    quantity        FLOAT NOT NULL,
    price           FLOAT NOT NULL,
    trade_value     FLOAT NOT NULL DEFAULT 0,
    commission      FLOAT NOT NULL DEFAULT 0,
    stamp_duty      FLOAT NOT NULL DEFAULT 0,
    transfer_fee    FLOAT NOT NULL DEFAULT 0,
    total_fee       FLOAT NOT NULL DEFAULT 0,
    executed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    price_source    VARCHAR(64),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ========================
-- 44. SIMULATION_FUND_SNAPSHOTS
-- ========================
CREATE TABLE IF NOT EXISTS simulation_fund_snapshots (
    id              SERIAL PRIMARY KEY,
    tenant_id       VARCHAR(50) NOT NULL DEFAULT 'default',
    user_id         VARCHAR(50) NOT NULL,
    snapshot_date   DATE NOT NULL,
    total_asset     FLOAT NOT NULL DEFAULT 0,
    available_balance FLOAT NOT NULL DEFAULT 0,
    frozen_balance  FLOAT NOT NULL DEFAULT 0,
    market_value    FLOAT NOT NULL DEFAULT 0,
    initial_capital FLOAT NOT NULL DEFAULT 0,
    total_pnl       FLOAT NOT NULL DEFAULT 0,
    today_pnl       FLOAT NOT NULL DEFAULT 0,
    source          VARCHAR(64) NOT NULL DEFAULT 'sim',
    updated_at      TIMESTAMP NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, user_id, snapshot_date)
);

-- ========================
-- 45. KLINES
-- ========================
CREATE TABLE IF NOT EXISTS klines (
    id              SERIAL PRIMARY KEY,
    symbol          VARCHAR(20) NOT NULL,
    interval        VARCHAR(10) NOT NULL,
    timestamp       TIMESTAMP NOT NULL,
    open_price      FLOAT NOT NULL,
    high_price      FLOAT NOT NULL,
    low_price       FLOAT NOT NULL,
    close_price     FLOAT NOT NULL,
    volume          INTEGER NOT NULL,
    amount          FLOAT,
    change          FLOAT,
    change_percent  FLOAT,
    turnover_rate   FLOAT,
    data_source     VARCHAR(20),
    created_at      TIMESTAMP DEFAULT NOW(),
    UNIQUE (symbol, interval, timestamp)
);

-- ========================
-- 46. QUOTES
-- ========================
CREATE TABLE IF NOT EXISTS quotes (
    id              SERIAL PRIMARY KEY,
    symbol          VARCHAR(20) NOT NULL,
    timestamp       TIMESTAMPTZ NOT NULL,
    open_price      FLOAT,
    high_price      FLOAT,
    low_price       FLOAT,
    close_price     FLOAT,
    current_price   FLOAT NOT NULL,
    volume          INTEGER,
    amount          FLOAT,
    pre_close       FLOAT,
    change          FLOAT,
    change_percent  FLOAT,
    bid1_price      FLOAT,
    bid1_volume     INTEGER,
    bid2_price      FLOAT,
    bid2_volume     INTEGER,
    bid3_price      FLOAT,
    bid3_volume     INTEGER,
    bid4_price      FLOAT,
    bid4_volume     INTEGER,
    bid5_price      FLOAT,
    bid5_volume     INTEGER,
    ask1_price      FLOAT,
    ask1_volume     INTEGER,
    ask2_price      FLOAT,
    ask2_volume     INTEGER,
    ask3_price      FLOAT,
    ask3_volume     INTEGER,
    ask4_price      FLOAT,
    ask4_volume     INTEGER,
    ask5_price      FLOAT,
    ask5_volume     INTEGER,
    data_source     VARCHAR(20),
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ========================
-- 47. QUOTE_DAILY_SUMMARIES
-- NOTE: 列定义与 backend/services/stream/main.py 的归档 SQL 保持一致
-- ========================
CREATE TABLE IF NOT EXISTS quote_daily_summaries (
    id              SERIAL PRIMARY KEY,
    trade_date      DATE NOT NULL,
    symbol          VARCHAR(20) NOT NULL,
    data_source     VARCHAR(20),
    open_price      FLOAT,
    high_price      FLOAT,
    low_price       FLOAT,
    close_price     FLOAT,
    avg_price       FLOAT,
    volume_sum      BIGINT,
    amount_sum      FLOAT,
    quote_count     INTEGER DEFAULT 0,
    first_quote_at  TIMESTAMPTZ,
    last_quote_at   TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (trade_date, symbol, data_source)
);

-- ========================
-- 48. COMMUNITY_POSTS
-- ========================
CREATE TABLE IF NOT EXISTS community_posts (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       VARCHAR(64) NOT NULL DEFAULT 'default',
    author_id       VARCHAR(64) NOT NULL,
    title           VARCHAR(256) NOT NULL,
    content         TEXT NOT NULL,
    category        VARCHAR(64),
    tags            JSONB DEFAULT '[]',
    media           JSONB DEFAULT '[]',
    excerpt         TEXT,
    views           INTEGER DEFAULT 0,
    likes           INTEGER DEFAULT 0,
    comments        INTEGER DEFAULT 0,
    collections     INTEGER DEFAULT 0,
    pinned          BOOLEAN DEFAULT FALSE,
    featured        BOOLEAN DEFAULT FALSE,
    created_at      TIMESTAMP DEFAULT NOW(),
    updated_at      TIMESTAMP DEFAULT NOW(),
    last_comment_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_cp_tenant_id ON community_posts (tenant_id);
CREATE INDEX IF NOT EXISTS idx_cp_author_id ON community_posts (author_id);
CREATE INDEX IF NOT EXISTS idx_cp_category ON community_posts (tenant_id, category);

-- ========================
-- 49. COMMUNITY_COMMENTS
-- ========================
CREATE TABLE IF NOT EXISTS community_comments (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       VARCHAR(64) NOT NULL DEFAULT 'default',
    post_id         BIGINT NOT NULL REFERENCES community_posts (id),
    author_id       VARCHAR(64) NOT NULL,
    content         TEXT NOT NULL,
    parent_id       BIGINT,
    reply_to_id     BIGINT,
    likes           INTEGER DEFAULT 0,
    created_at      TIMESTAMP DEFAULT NOW(),
    updated_at      TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_cc_post_id ON community_comments (post_id);
CREATE INDEX IF NOT EXISTS idx_cc_author_id ON community_comments (author_id);

-- ========================
-- 50. COMMUNITY_INTERACTIONS
-- ========================
CREATE TABLE IF NOT EXISTS community_interactions (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       VARCHAR(64) NOT NULL DEFAULT 'default',
    user_id         VARCHAR(64) NOT NULL,
    post_id         BIGINT,
    comment_id      BIGINT,
    type            VARCHAR(32) NOT NULL,
    created_at      TIMESTAMP DEFAULT NOW(),
    UNIQUE (tenant_id, user_id, post_id, comment_id, type)
);

-- ========================
-- 51. COMMUNITY_AUTHOR_FOLLOWS
-- ========================
CREATE TABLE IF NOT EXISTS community_author_follows (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       VARCHAR(64) NOT NULL DEFAULT 'default',
    follower_user_id VARCHAR(64) NOT NULL,
    author_user_id  VARCHAR(64) NOT NULL,
    created_at      TIMESTAMP DEFAULT NOW(),
    UNIQUE (tenant_id, follower_user_id, author_user_id)
);

-- ========================
-- 52. COMMUNITY_AUDIT_LOGS
-- ========================
CREATE TABLE IF NOT EXISTS community_audit_logs (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       VARCHAR(64) NOT NULL DEFAULT 'default',
    user_id         VARCHAR(64) NOT NULL,
    action          VARCHAR(64) NOT NULL,
    entity_type     VARCHAR(64) NOT NULL,
    entity_id       VARCHAR(64),
    ip              VARCHAR(64),
    user_agent      VARCHAR(256),
    meta            JSONB,
    created_at      TIMESTAMP DEFAULT NOW()
);

-- ========================
-- 53. ADMIN_MODELS
-- ========================
CREATE TABLE IF NOT EXISTS admin_models (
    id              SERIAL PRIMARY KEY,
    tenant_id       VARCHAR(64) NOT NULL DEFAULT 'default',
    user_id         VARCHAR(64) NOT NULL,
    name            VARCHAR(128) NOT NULL,
    description     TEXT,
    source_type     VARCHAR(32) NOT NULL,
    start_date      TIMESTAMP,
    end_date        TIMESTAMP,
    config          JSONB,
    is_active       BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMP DEFAULT NOW(),
    updated_at      TIMESTAMP DEFAULT NOW()
);

-- ========================
-- 54. ADMIN_DATA_FILES
-- ========================
CREATE TABLE IF NOT EXISTS admin_data_files (
    id              SERIAL PRIMARY KEY,
    tenant_id       VARCHAR(64) NOT NULL DEFAULT 'default',
    data_source_id  INTEGER REFERENCES admin_models (id) ON DELETE CASCADE,
    filename        VARCHAR(255) NOT NULL,
    file_size       INTEGER,
    status          VARCHAR(32),
    meta            JSONB,
    created_at      TIMESTAMP DEFAULT NOW()
);

-- ========================
-- 55. ADMIN_TRAINING_JOBS
-- ========================
CREATE TABLE IF NOT EXISTS admin_training_jobs (
    id              VARCHAR(64) PRIMARY KEY,
    tenant_id       VARCHAR(64) NOT NULL DEFAULT 'default',
    user_id         VARCHAR(64) NOT NULL,
    status          VARCHAR(32),
    instance_id     VARCHAR(64),
    request_payload JSONB,
    logs            TEXT,
    result          JSONB,
    progress        INTEGER DEFAULT 0,
    created_at      TIMESTAMP DEFAULT NOW(),
    updated_at      TIMESTAMP DEFAULT NOW()
);

-- ========================
-- 56. LOGIN_DEVICES
-- ========================
CREATE TABLE IF NOT EXISTS login_devices (
    id              SERIAL PRIMARY KEY,
    user_id         VARCHAR(64) NOT NULL,
    tenant_id       VARCHAR(64) NOT NULL DEFAULT 'default',
    device_id       VARCHAR(128) NOT NULL,
    device_name     VARCHAR(128),
    device_type     VARCHAR(32),
    os              VARCHAR(64),
    browser         VARCHAR(64),
    ip_address      VARCHAR(64),
    location        VARCHAR(128),
    is_trusted      BOOLEAN DEFAULT FALSE,
    is_active       BOOLEAN DEFAULT TRUE,
    first_seen_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at    TIMESTAMPTZ,
    last_location_change TIMESTAMPTZ
);

-- ========================
-- REPLAY (时光回放：模拟盘历史单步推演)
-- 与 sim_orders/sim_trades 刻意分表：会话生命周期独立，
-- 且 trade_date 记录的是「模拟交易日」而非墙钟时间。
-- ========================
CREATE TABLE IF NOT EXISTS replay_sessions (
    session_id      UUID PRIMARY KEY,
    tenant_id       VARCHAR(64) NOT NULL DEFAULT 'default',
    user_id         INTEGER NOT NULL,
    name            VARCHAR(128) NOT NULL DEFAULT '',
    model_id        VARCHAR(128),
    strategy_params JSONB NOT NULL DEFAULT '{}'::jsonb,
    initial_cash    FLOAT NOT NULL,
    start_date      DATE NOT NULL,
    end_date        DATE NOT NULL,
    cursor_date     DATE,
    next_date       DATE,
    sessions_total  INTEGER NOT NULL DEFAULT 0,
    sessions_done   INTEGER NOT NULL DEFAULT 0,
    status          VARCHAR(20) NOT NULL DEFAULT 'creating',
    signal_progress JSONB NOT NULL DEFAULT '{}'::jsonb,
    auto_trade      BOOLEAN NOT NULL DEFAULT TRUE,
    stop_loss_pct   FLOAT,
    pending_orders  JSONB,
    error_message   TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_replay_session_scope_status
    ON replay_sessions (tenant_id, user_id, status);

CREATE TABLE IF NOT EXISTS replay_orders (
    id              SERIAL PRIMARY KEY,
    order_id        UUID NOT NULL UNIQUE,
    session_id      UUID NOT NULL REFERENCES replay_sessions(session_id) ON DELETE CASCADE,
    trade_date      DATE NOT NULL,
    symbol          VARCHAR(20) NOT NULL,
    side            VARCHAR(10) NOT NULL,
    order_type      VARCHAR(10) NOT NULL DEFAULT 'market',
    status          VARCHAR(20) NOT NULL DEFAULT 'pending',
    origin          VARCHAR(20) NOT NULL DEFAULT 'signal',
    quantity        FLOAT NOT NULL,
    filled_quantity FLOAT NOT NULL DEFAULT 0,
    price           FLOAT,
    average_price   FLOAT,
    filled_value    FLOAT NOT NULL DEFAULT 0,
    total_fee       FLOAT NOT NULL DEFAULT 0,
    reject_reason   VARCHAR(200),
    price_source    VARCHAR(64),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_replay_order_session_date
    ON replay_orders (session_id, trade_date);

CREATE TABLE IF NOT EXISTS replay_trades (
    id              SERIAL PRIMARY KEY,
    trade_id        UUID NOT NULL UNIQUE,
    session_id      UUID NOT NULL REFERENCES replay_sessions(session_id) ON DELETE CASCADE,
    order_id        UUID NOT NULL REFERENCES replay_orders(order_id) ON DELETE CASCADE,
    trade_date      DATE NOT NULL,
    symbol          VARCHAR(20) NOT NULL,
    side            VARCHAR(10) NOT NULL,
    origin          VARCHAR(20) NOT NULL DEFAULT 'signal',
    quantity        FLOAT NOT NULL,
    price           FLOAT NOT NULL,
    trade_value     FLOAT NOT NULL,
    commission      FLOAT NOT NULL DEFAULT 0,
    stamp_duty      FLOAT NOT NULL DEFAULT 0,
    transfer_fee    FLOAT NOT NULL DEFAULT 0,
    total_fee       FLOAT NOT NULL DEFAULT 0,
    price_source    VARCHAR(64),
    avg_cost_before FLOAT,
    realized_pnl    FLOAT,
    holding_days    INTEGER,
    executed_at     TIMESTAMP NOT NULL DEFAULT NOW(),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_replay_trade_session_date
    ON replay_trades (session_id, trade_date);

CREATE TABLE IF NOT EXISTS replay_equity_snapshots (
    id              SERIAL PRIMARY KEY,
    session_id      UUID NOT NULL REFERENCES replay_sessions(session_id) ON DELETE CASCADE,
    trade_date      DATE NOT NULL,
    cash            FLOAT NOT NULL DEFAULT 0,
    market_value    FLOAT NOT NULL DEFAULT 0,
    total_asset     FLOAT NOT NULL DEFAULT 0,
    day_pnl         FLOAT NOT NULL DEFAULT 0,
    cum_pnl         FLOAT NOT NULL DEFAULT 0,
    realized_pnl_cum FLOAT NOT NULL DEFAULT 0,
    unrealized_pnl  FLOAT NOT NULL DEFAULT 0,
    position_count  INTEGER NOT NULL DEFAULT 0,
    positions       JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_replay_equity_session_date UNIQUE (session_id, trade_date)
);

CREATE TABLE IF NOT EXISTS replay_signals (
    id              SERIAL PRIMARY KEY,
    session_id      UUID NOT NULL REFERENCES replay_sessions(session_id) ON DELETE CASCADE,
    trade_date      DATE NOT NULL,
    symbol          VARCHAR(20) NOT NULL,
    score           FLOAT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_replay_signal_session_date_symbol UNIQUE (session_id, trade_date, symbol)
);

CREATE INDEX IF NOT EXISTS idx_replay_signal_session_date
    ON replay_signals (session_id, trade_date);

-- ========================
-- 59. USER APP 扩展表（认证/RBAC/订阅/通知/KYC）
-- NOTE: 列定义与 backend/services/api/user_app/models/ 下的模型一致
-- ========================

-- 59.1 ROLES
CREATE TABLE IF NOT EXISTS roles (
    id              SERIAL PRIMARY KEY,
    name            VARCHAR(64) NOT NULL UNIQUE,
    code            VARCHAR(64) NOT NULL UNIQUE,
    description     TEXT,
    is_active       BOOLEAN DEFAULT TRUE,
    is_system       BOOLEAN DEFAULT FALSE,
    priority        INTEGER DEFAULT 0,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_roles_code ON roles (code);

-- 59.2 PERMISSIONS
CREATE TABLE IF NOT EXISTS permissions (
    id              SERIAL PRIMARY KEY,
    name            VARCHAR(128) NOT NULL UNIQUE,
    code            VARCHAR(128) NOT NULL UNIQUE,
    resource        VARCHAR(64) NOT NULL,
    action          VARCHAR(32) NOT NULL,
    description     TEXT,
    is_active       BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_permissions_code ON permissions (code);
CREATE INDEX IF NOT EXISTS idx_permissions_resource ON permissions (resource);

-- 59.3 USER_ROLES（用户-角色关联）
CREATE TABLE IF NOT EXISTS user_roles (
    user_id         VARCHAR(64) NOT NULL REFERENCES users(user_id),
    role_id         INTEGER NOT NULL REFERENCES roles(id),
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (user_id, role_id)
);

-- 59.4 ROLE_PERMISSIONS（角色-权限关联）
CREATE TABLE IF NOT EXISTS role_permissions (
    role_id         INTEGER NOT NULL REFERENCES roles(id),
    permission_id   INTEGER NOT NULL REFERENCES permissions(id),
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (role_id, permission_id)
);

-- 59.5 API_KEYS
CREATE TABLE IF NOT EXISTS api_keys (
    id              SERIAL PRIMARY KEY,
    user_id         VARCHAR(64) NOT NULL,
    tenant_id       VARCHAR(64) NOT NULL,
    access_key      VARCHAR(64) NOT NULL UNIQUE,
    secret_hash     VARCHAR(255) NOT NULL,
    name            VARCHAR(100),
    permissions     JSONB DEFAULT '[]'::jsonb,
    is_active       BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    expires_at      TIMESTAMPTZ,
    last_used_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_api_keys_user_id ON api_keys (user_id);
CREATE INDEX IF NOT EXISTS idx_api_keys_tenant_id ON api_keys (tenant_id);
CREATE INDEX IF NOT EXISTS idx_api_keys_access_key ON api_keys (access_key);

-- 59.6 IDENTITY_VERIFICATIONS（实名认证）
CREATE TABLE IF NOT EXISTS identity_verifications (
    id                  SERIAL PRIMARY KEY,
    user_id             VARCHAR(64) NOT NULL REFERENCES users(user_id),
    tenant_id           VARCHAR(64) NOT NULL,
    real_name           VARCHAR(128) NOT NULL,
    id_number           VARCHAR(128) NOT NULL,
    document_type       VARCHAR(32) DEFAULT 'id_card',
    front_image_url     VARCHAR(512),
    back_image_url      VARCHAR(512),
    handheld_image_url  VARCHAR(512),
    status              VARCHAR(32) DEFAULT 'pending',
    rejection_reason    TEXT,
    submitted_at        TIMESTAMPTZ DEFAULT NOW(),
    verified_at         TIMESTAMPTZ,
    verified_by         VARCHAR(64)
);
CREATE INDEX IF NOT EXISTS idx_identity_verifications_user_id ON identity_verifications (user_id);
CREATE INDEX IF NOT EXISTS idx_identity_verifications_tenant_id ON identity_verifications (tenant_id);
CREATE INDEX IF NOT EXISTS idx_identity_verifications_id_number ON identity_verifications (id_number);
CREATE INDEX IF NOT EXISTS idx_identity_verifications_status ON identity_verifications (status);

-- 59.7 NOTIFICATIONS
CREATE TABLE IF NOT EXISTS notifications (
    id                  SERIAL PRIMARY KEY,
    user_id             VARCHAR(64) NOT NULL REFERENCES users(user_id),
    tenant_id           VARCHAR(64) NOT NULL,
    title               VARCHAR(128) NOT NULL,
    content             TEXT NOT NULL,
    notification_type   VARCHAR(32) DEFAULT 'system',
    level               VARCHAR(16) DEFAULT 'info',
    action_url          VARCHAR(512),
    is_read             BOOLEAN DEFAULT FALSE,
    read_at             TIMESTAMPTZ,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    expires_at          TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_notifications_user_id ON notifications (user_id);
CREATE INDEX IF NOT EXISTS idx_notifications_tenant_id ON notifications (tenant_id);
CREATE INDEX IF NOT EXISTS idx_notifications_type ON notifications (notification_type);
CREATE INDEX IF NOT EXISTS idx_notifications_is_read ON notifications (is_read);
CREATE INDEX IF NOT EXISTS idx_notifications_created_at ON notifications (created_at);

-- 59.8 PAYMENT_TRANSACTIONS
CREATE TABLE IF NOT EXISTS payment_transactions (
    id              SERIAL PRIMARY KEY,
    user_id         VARCHAR(64) NOT NULL,
    tenant_id       VARCHAR(64) NOT NULL,
    amount          FLOAT NOT NULL,
    currency        VARCHAR(16) NOT NULL DEFAULT 'CNY',
    status          VARCHAR(32) NOT NULL DEFAULT 'pending',
    provider        VARCHAR(32) NOT NULL DEFAULT 'alipay',
    transaction_id  VARCHAR(128) NOT NULL UNIQUE,
    description     TEXT,
    metadata_info   JSONB,
    created_at      TIMESTAMP DEFAULT NOW(),
    completed_at    TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_payment_transactions_user_id ON payment_transactions (user_id);
CREATE INDEX IF NOT EXISTS idx_payment_transactions_tenant_id ON payment_transactions (tenant_id);
CREATE INDEX IF NOT EXISTS idx_payment_transactions_tx_id ON payment_transactions (transaction_id);

-- 59.9 USER_AUDIT_LOGS
CREATE TABLE IF NOT EXISTS user_audit_logs (
    id              SERIAL PRIMARY KEY,
    user_id         VARCHAR(64) NOT NULL,
    tenant_id       VARCHAR(64) NOT NULL,
    action          VARCHAR(64) NOT NULL,
    resource        VARCHAR(128),
    resource_id     VARCHAR(128),
    description     TEXT,
    request_data    TEXT,
    response_data   TEXT,
    ip_address      VARCHAR(64),
    user_agent      TEXT,
    request_method  VARCHAR(16),
    request_path    VARCHAR(255),
    status_code     INTEGER,
    success         BOOLEAN DEFAULT TRUE,
    error_message   TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    duration_ms     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_user_audit_logs_user_id ON user_audit_logs (user_id);
CREATE INDEX IF NOT EXISTS idx_user_audit_logs_tenant_id ON user_audit_logs (tenant_id);
CREATE INDEX IF NOT EXISTS idx_user_audit_logs_action ON user_audit_logs (action);
CREATE INDEX IF NOT EXISTS idx_user_audit_logs_created_at ON user_audit_logs (created_at);

-- 59.10 EMAIL_VERIFICATIONS
CREATE TABLE IF NOT EXISTS email_verifications (
    id                  SERIAL PRIMARY KEY,
    user_id             VARCHAR(64) NOT NULL,
    tenant_id           VARCHAR(64) NOT NULL,
    email               VARCHAR(255) NOT NULL,
    verification_code   VARCHAR(128) NOT NULL UNIQUE,
    code_type           VARCHAR(32) NOT NULL,
    is_used             BOOLEAN DEFAULT FALSE,
    is_expired          BOOLEAN DEFAULT FALSE,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    expires_at          TIMESTAMPTZ NOT NULL,
    used_at             TIMESTAMPTZ,
    attempts            INTEGER DEFAULT 0,
    ip_address          VARCHAR(64)
);
CREATE INDEX IF NOT EXISTS idx_email_verifications_user_id ON email_verifications (user_id);
CREATE INDEX IF NOT EXISTS idx_email_verifications_tenant_id ON email_verifications (tenant_id);
CREATE INDEX IF NOT EXISTS idx_email_verifications_email ON email_verifications (email);
CREATE INDEX IF NOT EXISTS idx_email_verifications_code ON email_verifications (verification_code);
CREATE INDEX IF NOT EXISTS idx_email_verifications_is_used ON email_verifications (is_used);
CREATE INDEX IF NOT EXISTS idx_email_verifications_expires_at ON email_verifications (expires_at);

-- 59.11 PASSWORD_RESET_TOKENS
CREATE TABLE IF NOT EXISTS password_reset_tokens (
    id              SERIAL PRIMARY KEY,
    user_id         VARCHAR(64) NOT NULL REFERENCES users(user_id),
    tenant_id       VARCHAR(64) NOT NULL,
    email           VARCHAR(255) NOT NULL,
    token           VARCHAR(128) NOT NULL UNIQUE,
    is_used         BOOLEAN DEFAULT FALSE,
    is_expired      BOOLEAN DEFAULT FALSE,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    expires_at      TIMESTAMPTZ NOT NULL,
    used_at         TIMESTAMPTZ,
    ip_address      VARCHAR(64),
    attempts        INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_password_reset_tokens_user_id ON password_reset_tokens (user_id);
CREATE INDEX IF NOT EXISTS idx_password_reset_tokens_tenant_id ON password_reset_tokens (tenant_id);
CREATE INDEX IF NOT EXISTS idx_password_reset_tokens_email ON password_reset_tokens (email);
CREATE INDEX IF NOT EXISTS idx_password_reset_tokens_token ON password_reset_tokens (token);
CREATE INDEX IF NOT EXISTS idx_password_reset_tokens_is_used ON password_reset_tokens (is_used);
CREATE INDEX IF NOT EXISTS idx_password_reset_tokens_expires_at ON password_reset_tokens (expires_at);

-- 59.12 SUBSCRIPTION_PLANS
CREATE TABLE IF NOT EXISTS subscription_plans (
    id              SERIAL PRIMARY KEY,
    name            VARCHAR(100) NOT NULL,
    code            VARCHAR(50) NOT NULL UNIQUE,
    description     VARCHAR(255),
    price           NUMERIC(10, 2) NOT NULL DEFAULT 0.00,
    currency        VARCHAR(3) DEFAULT 'CNY',
    "interval"      VARCHAR(20) DEFAULT 'month',
    features        JSONB DEFAULT '[]'::jsonb,
    is_active       BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_subscription_plans_code ON subscription_plans (code);

-- 59.13 USER_SUBSCRIPTIONS
CREATE TABLE IF NOT EXISTS user_subscriptions (
    id              SERIAL PRIMARY KEY,
    user_id         VARCHAR(64) NOT NULL,
    tenant_id       VARCHAR(64) NOT NULL,
    plan_id         INTEGER NOT NULL REFERENCES subscription_plans(id),
    status          VARCHAR(20) DEFAULT 'active',
    start_date      TIMESTAMPTZ NOT NULL,
    end_date        TIMESTAMPTZ NOT NULL,
    auto_renew      BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_user_subscriptions_user_id ON user_subscriptions (user_id);
CREATE INDEX IF NOT EXISTS idx_user_subscriptions_tenant_id ON user_subscriptions (tenant_id);

-- 59.14 USER_PROFILES
CREATE TABLE IF NOT EXISTS user_profiles (
    id                      SERIAL PRIMARY KEY,
    user_id                 VARCHAR(64) NOT NULL UNIQUE,
    tenant_id               VARCHAR(64) NOT NULL,
    display_name            VARCHAR(128),
    avatar_url              VARCHAR(512),
    bio                     TEXT,
    location                VARCHAR(128),
    website                 VARCHAR(255),
    phone                   VARCHAR(32),
    trading_experience      VARCHAR(32) DEFAULT 'intermediate',
    risk_tolerance          VARCHAR(32) DEFAULT 'medium',
    investment_goal         VARCHAR(128),
    github_url              VARCHAR(255),
    twitter_handle          VARCHAR(128),
    linkedin_url            VARCHAR(255),
    preferences             JSONB DEFAULT '{}'::jsonb,
    notification_settings   JSONB DEFAULT '{}'::jsonb,
    api_key                 TEXT,
    created_at              TIMESTAMPTZ DEFAULT NOW(),
    updated_at              TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_user_profiles_user_id ON user_profiles (user_id);
CREATE INDEX IF NOT EXISTS idx_user_profiles_tenant_id ON user_profiles (tenant_id);

-- 59.15 USER_SESSIONS
CREATE TABLE IF NOT EXISTS user_sessions (
    id              VARCHAR(64) PRIMARY KEY,
    session_id      VARCHAR(64) NOT NULL UNIQUE,
    user_id         VARCHAR(64) NOT NULL,
    tenant_id       VARCHAR(64) NOT NULL,
    token_jti       VARCHAR(64),
    refresh_token   VARCHAR(1024),
    ip_address      VARCHAR(64),
    user_agent      VARCHAR(255),
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    expires_at      TIMESTAMPTZ NOT NULL,
    last_active_at  TIMESTAMPTZ,
    is_active       BOOLEAN DEFAULT TRUE,
    is_revoked      BOOLEAN DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS idx_user_sessions_user_id ON user_sessions (user_id);
CREATE INDEX IF NOT EXISTS idx_user_sessions_tenant_id ON user_sessions (tenant_id);
CREATE INDEX IF NOT EXISTS idx_user_sessions_token_jti ON user_sessions (token_jti);

-- ========================
-- 60. SIMULATION 账本表（模拟交易重构：账本/日终/公司行为/调仓）
-- NOTE: 列定义与 backend/services/trade/simulation/models/ 下的模型一致
-- ========================

-- 60.1 SIMULATION_ACCOUNTS
CREATE TABLE IF NOT EXISTS simulation_accounts (
    account_id              VARCHAR(96) PRIMARY KEY,
    tenant_id               VARCHAR(64) NOT NULL DEFAULT 'default',
    user_id                 VARCHAR(64) NOT NULL,
    base_currency           VARCHAR(16) NOT NULL DEFAULT 'CNY',
    account_type            VARCHAR(32) NOT NULL DEFAULT 'cash',
    status                  VARCHAR(32) NOT NULL DEFAULT 'active',
    initial_equity          FLOAT NOT NULL DEFAULT 0,
    cash                    FLOAT NOT NULL DEFAULT 0,
    available_cash          FLOAT NOT NULL DEFAULT 0,
    frozen_cash             FLOAT NOT NULL DEFAULT 0,
    long_market_value       FLOAT NOT NULL DEFAULT 0,
    short_market_value      FLOAT NOT NULL DEFAULT 0,
    total_asset             FLOAT NOT NULL DEFAULT 0,
    liabilities             FLOAT NOT NULL DEFAULT 0,
    equity                  FLOAT NOT NULL DEFAULT 0,
    maintenance_margin_ratio FLOAT NOT NULL DEFAULT 0,
    last_trade_at           TIMESTAMP,
    last_projected_at       TIMESTAMP,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_simulation_accounts_tenant_user
    ON simulation_accounts (tenant_id, user_id);

-- 60.2 SIMULATION_ACCOUNT_DAILY
CREATE TABLE IF NOT EXISTS simulation_account_daily (
    id                  SERIAL PRIMARY KEY,
    account_id          VARCHAR(96) NOT NULL,
    tenant_id           VARCHAR(64) NOT NULL DEFAULT 'default',
    user_id             VARCHAR(64) NOT NULL,
    snapshot_date       DATE NOT NULL,
    snapshot_at         TIMESTAMP NOT NULL,
    cash                FLOAT NOT NULL DEFAULT 0,
    available_cash      FLOAT NOT NULL DEFAULT 0,
    frozen_cash         FLOAT NOT NULL DEFAULT 0,
    long_market_value   FLOAT NOT NULL DEFAULT 0,
    short_market_value  FLOAT NOT NULL DEFAULT 0,
    total_asset         FLOAT NOT NULL DEFAULT 0,
    liabilities         FLOAT NOT NULL DEFAULT 0,
    equity              FLOAT NOT NULL DEFAULT 0,
    daily_pnl           FLOAT NOT NULL DEFAULT 0,
    total_pnl           FLOAT NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_sim_account_daily_owner_time
    ON simulation_account_daily (tenant_id, user_id, snapshot_at);
CREATE INDEX IF NOT EXISTS idx_sim_account_daily_owner_date
    ON simulation_account_daily (tenant_id, user_id, snapshot_date);
CREATE INDEX IF NOT EXISTS idx_sim_account_daily_account_id
    ON simulation_account_daily (account_id);
CREATE INDEX IF NOT EXISTS idx_sim_account_daily_snapshot_date
    ON simulation_account_daily (snapshot_date);

-- 60.3 SIMULATION_CASH_LEDGER
CREATE TABLE IF NOT EXISTS simulation_cash_ledger (
    id              SERIAL PRIMARY KEY,
    account_id      VARCHAR(96) NOT NULL,
    tenant_id       VARCHAR(64) NOT NULL DEFAULT 'default',
    user_id         VARCHAR(64) NOT NULL,
    event_type      VARCHAR(64) NOT NULL,
    ref_type        VARCHAR(32) NOT NULL DEFAULT 'trade',
    ref_id          VARCHAR(96),
    amount          FLOAT NOT NULL DEFAULT 0,
    balance_after   FLOAT,
    trade_date      TIMESTAMP,
    occurred_at     TIMESTAMP NOT NULL,
    currency        VARCHAR(16) NOT NULL DEFAULT 'CNY',
    note            VARCHAR(255),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_sim_cash_ledger_owner_time
    ON simulation_cash_ledger (tenant_id, user_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_sim_cash_ledger_account_id
    ON simulation_cash_ledger (account_id);
CREATE INDEX IF NOT EXISTS idx_sim_cash_ledger_event_type
    ON simulation_cash_ledger (event_type);

-- 60.4 SIMULATION_CORPORATE_ACTIONS
CREATE TABLE IF NOT EXISTS simulation_corporate_actions (
    id                      SERIAL PRIMARY KEY,
    symbol                  VARCHAR(20) NOT NULL,
    action_type             VARCHAR(32) NOT NULL,
    ex_date                 TIMESTAMP,
    effective_date          TIMESTAMP,
    cash_dividend_per_share FLOAT NOT NULL DEFAULT 0,
    share_ratio             FLOAT NOT NULL DEFAULT 0,
    rights_price            FLOAT NOT NULL DEFAULT 0,
    source                  VARCHAR(64) NOT NULL DEFAULT 'manual',
    note                    VARCHAR(255),
    status                  VARCHAR(32) NOT NULL DEFAULT 'pending',
    applied_at              TIMESTAMP,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_sim_corporate_actions_symbol_dates
    ON simulation_corporate_actions (symbol, ex_date, effective_date);
CREATE INDEX IF NOT EXISTS idx_sim_corporate_actions_status_effective
    ON simulation_corporate_actions (status, effective_date);
CREATE INDEX IF NOT EXISTS idx_sim_corporate_actions_action_type
    ON simulation_corporate_actions (action_type);

-- 60.5 SIMULATION_FILLS
CREATE TABLE IF NOT EXISTS simulation_fills (
    id              SERIAL PRIMARY KEY,
    fill_id         UUID NOT NULL UNIQUE,
    order_id        UUID NOT NULL,
    legacy_trade_id INTEGER,
    tenant_id       VARCHAR(64) NOT NULL DEFAULT 'default',
    user_id         VARCHAR(32) NOT NULL,
    account_id      VARCHAR(128) NOT NULL,
    strategy_id     VARCHAR(64),
    portfolio_id    INTEGER NOT NULL DEFAULT 0,
    symbol          VARCHAR(20) NOT NULL,
    side            VARCHAR(16) NOT NULL,
    position_side   VARCHAR(16) NOT NULL DEFAULT 'long',
    trade_action    VARCHAR(32),
    fill_price      FLOAT NOT NULL,
    fill_quantity   FLOAT NOT NULL,
    gross_amount    FLOAT NOT NULL,
    commission      FLOAT NOT NULL DEFAULT 0,
    stamp_duty      FLOAT NOT NULL DEFAULT 0,
    transfer_fee    FLOAT NOT NULL DEFAULT 0,
    borrow_fee      FLOAT NOT NULL DEFAULT 0,
    executed_at     TIMESTAMP NOT NULL,
    price_source    VARCHAR(64),
    session_phase   VARCHAR(32),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_simulation_fills_owner_symbol
    ON simulation_fills (tenant_id, user_id, symbol);
CREATE INDEX IF NOT EXISTS idx_simulation_fills_owner_executed
    ON simulation_fills (tenant_id, user_id, executed_at);
CREATE INDEX IF NOT EXISTS idx_simulation_fills_order_id ON simulation_fills (order_id);
CREATE INDEX IF NOT EXISTS idx_simulation_fills_symbol ON simulation_fills (symbol);

-- 60.6 SIMULATION_ORDERS
CREATE TABLE IF NOT EXISTS simulation_orders (
    id                  SERIAL PRIMARY KEY,
    order_id            UUID NOT NULL UNIQUE,
    client_order_id     VARCHAR(64),
    tenant_id           VARCHAR(64) NOT NULL DEFAULT 'default',
    user_id             VARCHAR(32) NOT NULL,
    strategy_id         VARCHAR(64),
    account_id          VARCHAR(128) NOT NULL,
    portfolio_id        INTEGER NOT NULL DEFAULT 0,
    legacy_order_id     INTEGER,
    symbol              VARCHAR(20) NOT NULL,
    side                VARCHAR(16) NOT NULL,
    position_side       VARCHAR(16) NOT NULL DEFAULT 'long',
    trade_action        VARCHAR(32),
    order_type          VARCHAR(16) NOT NULL,
    time_in_force       VARCHAR(16) NOT NULL DEFAULT 'DAY',
    quantity            FLOAT NOT NULL,
    price               FLOAT,
    trigger_source      VARCHAR(32) NOT NULL DEFAULT 'manual',
    status              VARCHAR(32) NOT NULL DEFAULT 'pending',
    rejected_reason     VARCHAR(500),
    trading_session_date DATE,
    submitted_at        TIMESTAMP,
    expires_at          TIMESTAMP,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_simulation_orders_owner_status
    ON simulation_orders (tenant_id, user_id, status);
CREATE INDEX IF NOT EXISTS idx_simulation_orders_owner_created
    ON simulation_orders (tenant_id, user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_simulation_orders_order_id ON simulation_orders (order_id);
CREATE INDEX IF NOT EXISTS idx_simulation_orders_symbol ON simulation_orders (symbol);

-- 60.7 SIMULATION_POSITION_DAILY
CREATE TABLE IF NOT EXISTS simulation_position_daily (
    id                  SERIAL PRIMARY KEY,
    account_id          VARCHAR(96) NOT NULL,
    tenant_id           VARCHAR(64) NOT NULL DEFAULT 'default',
    user_id             VARCHAR(64) NOT NULL,
    snapshot_date       DATE NOT NULL,
    snapshot_at         TIMESTAMP NOT NULL,
    symbol              VARCHAR(20) NOT NULL,
    position_side       VARCHAR(16) NOT NULL DEFAULT 'long',
    quantity            FLOAT NOT NULL DEFAULT 0,
    available_quantity  FLOAT NOT NULL DEFAULT 0,
    frozen_quantity     FLOAT NOT NULL DEFAULT 0,
    cost_price          FLOAT NOT NULL DEFAULT 0,
    close_price         FLOAT NOT NULL DEFAULT 0,
    market_value        FLOAT NOT NULL DEFAULT 0,
    unrealized_pnl      FLOAT NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_sim_position_daily_owner_symbol_time
    ON simulation_position_daily (tenant_id, user_id, symbol, snapshot_at);
CREATE INDEX IF NOT EXISTS idx_sim_position_daily_owner_symbol_date
    ON simulation_position_daily (tenant_id, user_id, symbol, snapshot_date);
CREATE INDEX IF NOT EXISTS idx_sim_position_daily_snapshot_date
    ON simulation_position_daily (snapshot_date);

-- 60.8 SIMULATION_POSITION_LOTS
CREATE TABLE IF NOT EXISTS simulation_position_lots (
    id                  SERIAL PRIMARY KEY,
    account_id          VARCHAR(96) NOT NULL,
    tenant_id           VARCHAR(64) NOT NULL DEFAULT 'default',
    user_id             VARCHAR(64) NOT NULL,
    symbol              VARCHAR(20) NOT NULL,
    position_side       VARCHAR(16) NOT NULL DEFAULT 'long',
    open_fill_id        VARCHAR(96),
    open_date           TIMESTAMP,
    quantity_open       FLOAT NOT NULL DEFAULT 0,
    quantity_remaining  FLOAT NOT NULL DEFAULT 0,
    cost_price          FLOAT NOT NULL DEFAULT 0,
    cost_amount         FLOAT NOT NULL DEFAULT 0,
    status              VARCHAR(32) NOT NULL DEFAULT 'open',
    closed_at           TIMESTAMP,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_sim_position_lots_owner_symbol_side
    ON simulation_position_lots (tenant_id, user_id, symbol, position_side);
CREATE INDEX IF NOT EXISTS idx_sim_position_lots_symbol
    ON simulation_position_lots (symbol);

-- 60.9 SIMULATION_REBALANCE_JOBS
CREATE TABLE IF NOT EXISTS simulation_rebalance_jobs (
    id                  SERIAL PRIMARY KEY,
    job_id              VARCHAR(96) NOT NULL UNIQUE,
    tenant_id           VARCHAR(64) NOT NULL DEFAULT 'default',
    user_id             VARCHAR(64) NOT NULL,
    strategy_id         VARCHAR(96) NOT NULL,
    job_type            VARCHAR(32) NOT NULL DEFAULT 'rebalance',
    schedule_type       VARCHAR(32) NOT NULL DEFAULT 'interval',
    planned_run_at      TIMESTAMP,
    window_start_at     TIMESTAMP,
    window_end_at       TIMESTAMP,
    status              VARCHAR(32) NOT NULL DEFAULT 'pending',
    attempt_count       INTEGER NOT NULL DEFAULT 0,
    last_error          VARCHAR(500),
    idempotency_key     VARCHAR(128),
    started_at          TIMESTAMP,
    finished_at         TIMESTAMP,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_sim_rebalance_jobs_owner_status
    ON simulation_rebalance_jobs (tenant_id, user_id, status);
CREATE INDEX IF NOT EXISTS idx_sim_rebalance_jobs_job_id
    ON simulation_rebalance_jobs (job_id);
CREATE INDEX IF NOT EXISTS idx_sim_rebalance_jobs_idempotency_key
    ON simulation_rebalance_jobs (idempotency_key);

-- ========================
-- 61. QMT_AGENT（QMT Agent 设备绑定与会话）
-- NOTE: 列定义与 backend/services/trade/models/qmt_agent_binding.py / qmt_agent_session.py 一致
-- ========================

-- 61.1 QMT_AGENT_BINDINGS
CREATE TABLE IF NOT EXISTS qmt_agent_bindings (
    id                  VARCHAR(64) PRIMARY KEY,
    tenant_id           VARCHAR(64) NOT NULL DEFAULT 'default',
    user_id             VARCHAR(64) NOT NULL,
    api_key_id          INTEGER NOT NULL,
    agent_type          VARCHAR(32) NOT NULL DEFAULT 'qmt',
    account_id          VARCHAR(64) NOT NULL,
    client_fingerprint  VARCHAR(255) NOT NULL,
    hostname            VARCHAR(255),
    client_version      VARCHAR(64),
    status              VARCHAR(32) NOT NULL DEFAULT 'active',
    last_ip             VARCHAR(64),
    last_seen_at        TIMESTAMPTZ,
    bound_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_qmt_binding_tenant_account_status
    ON qmt_agent_bindings (tenant_id, account_id, status);
CREATE INDEX IF NOT EXISTS idx_qmt_binding_api_key ON qmt_agent_bindings (api_key_id);
CREATE INDEX IF NOT EXISTS idx_qmt_binding_tenant_id ON qmt_agent_bindings (tenant_id);
CREATE INDEX IF NOT EXISTS idx_qmt_binding_user_id ON qmt_agent_bindings (user_id);
CREATE INDEX IF NOT EXISTS idx_qmt_binding_account_id ON qmt_agent_bindings (account_id);
CREATE INDEX IF NOT EXISTS idx_qmt_binding_status ON qmt_agent_bindings (status);

-- 61.2 QMT_AGENT_SESSIONS
CREATE TABLE IF NOT EXISTS qmt_agent_sessions (
    id              VARCHAR(64) PRIMARY KEY,
    binding_id      VARCHAR(64) NOT NULL,
    tenant_id       VARCHAR(64) NOT NULL DEFAULT 'default',
    user_id         VARCHAR(64) NOT NULL,
    token_hash      VARCHAR(64) NOT NULL UNIQUE,
    expires_at      TIMESTAMPTZ NOT NULL,
    revoked_at      TIMESTAMPTZ,
    last_used_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_qmt_session_binding ON qmt_agent_sessions (binding_id);
CREATE INDEX IF NOT EXISTS idx_qmt_session_tenant_user ON qmt_agent_sessions (tenant_id, user_id);
CREATE INDEX IF NOT EXISTS idx_qmt_session_tenant_id ON qmt_agent_sessions (tenant_id);
CREATE INDEX IF NOT EXISTS idx_qmt_session_user_id ON qmt_agent_sessions (user_id);
CREATE INDEX IF NOT EXISTS idx_qmt_session_token_hash ON qmt_agent_sessions (token_hash);

-- ========================
-- 62. ENGINE 标签与系统任务表
-- NOTE: 列定义与 backend/services/engine/models/stock_tag.py / task.py 一致
-- ========================

-- 62.1 TAG_DICTIONARY（标签字典）
CREATE TABLE IF NOT EXISTS tag_dictionary (
    tag_code        VARCHAR(64) PRIMARY KEY,
    tag_name        VARCHAR(128) NOT NULL,
    tag_category    VARCHAR(32) NOT NULL,
    source          VARCHAR(64),
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    sort_order      INTEGER NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 62.2 STOCK_TAG（股票-标签成员关系）
CREATE TABLE IF NOT EXISTS stock_tag (
    id              BIGSERIAL PRIMARY KEY,
    symbol          VARCHAR(16) NOT NULL,
    tag_code        VARCHAR(64) NOT NULL REFERENCES tag_dictionary(tag_code),
    source          VARCHAR(64),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_stock_tag_symbol_code UNIQUE (symbol, tag_code)
);
CREATE INDEX IF NOT EXISTS ix_stock_tag_tag_code ON stock_tag (tag_code);
CREATE INDEX IF NOT EXISTS ix_stock_tag_symbol ON stock_tag (symbol);

-- 62.3 SYSTEM_TASKS（系统后台任务）
CREATE TABLE IF NOT EXISTS system_tasks (
    task_id         VARCHAR(64) PRIMARY KEY,
    task_type       VARCHAR(32) NOT NULL,
    status          VARCHAR(20) DEFAULT 'PENDING',
    progress        INTEGER DEFAULT 0,
    logs            TEXT,
    result_path     TEXT,
    error_message   TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    finished_at     TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_system_tasks_task_type ON system_tasks (task_type);
CREATE INDEX IF NOT EXISTS ix_system_tasks_status ON system_tasks (status);

-- ========================
-- 63. QM_USER_MODELS & QM_STRATEGY_MODEL_BINDINGS (用户模型注册与策略绑定表)
-- ========================
CREATE TABLE IF NOT EXISTS qm_user_models (
    tenant_id VARCHAR(64) NOT NULL,
    user_id VARCHAR(64) NOT NULL,
    model_id VARCHAR(128) NOT NULL,
    source_run_id VARCHAR(64),
    status VARCHAR(32) NOT NULL DEFAULT 'candidate',
    storage_path TEXT,
    model_file VARCHAR(255),
    metadata_json JSONB,
    metrics_json JSONB,
    is_default BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    activated_at TIMESTAMPTZ,
    PRIMARY KEY (tenant_id, user_id, model_id)
);

CREATE TABLE IF NOT EXISTS qm_strategy_model_bindings (
    tenant_id VARCHAR(64) NOT NULL,
    user_id VARCHAR(64) NOT NULL,
    strategy_id VARCHAR(128) NOT NULL,
    model_id VARCHAR(128) NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, user_id, strategy_id)
);

CREATE INDEX IF NOT EXISTS idx_qm_user_models_user_status
    ON qm_user_models (tenant_id, user_id, status, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_qm_strategy_model_bindings_model
    ON qm_strategy_model_bindings (tenant_id, user_id, model_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_qm_user_models_default_per_user
    ON qm_user_models (tenant_id, user_id)
    WHERE is_default = TRUE;

-- 彻底清除存量硬编码 model_qlib 与 alpha158 假数据记录
DELETE FROM qm_user_models
WHERE model_id IN ('model_qlib', 'alpha158', 'sys-model_qlib', 'sys-alpha158');

-- ========================
-- 默认管理员（admin / admin123）
-- NOTE: 幂等，仅在不存在时创建，不覆盖用户已改密码；display_name / 头像由启动期 seed_data.py 负责
-- ========================
INSERT INTO users (user_id, tenant_id, username, email, password_hash, is_active, is_admin, is_verified, is_locked, login_count, created_at, updated_at, is_deleted)
VALUES ('admin', 'default', 'admin', 'admin@quantmind.local',
        '$2b$12$B/yjK9cT.wx4BlB9j.r/t.dADjCbmutIXoDM7PdKZmV6ypuYiiUvW',
        TRUE, TRUE, TRUE, FALSE, 0, NOW(), NOW(), FALSE)
ON CONFLICT (user_id) DO NOTHING;

-- ========================
-- DONE - 所有缺失表已创建
-- ========================

-- ========================
-- 33.5 ORDER_HISTORY（历史委托归档表，多交易所）
-- ========================
-- 设计说明：
--   1. 目的：持久化历史委托。桥/券商接口的当日委托列表有上限，历史委托会滚出
--      实时列表（TDX 桥只支持当日委托查询），本表作为不可变归档防丢失。
--   2. 多交易所：market (CN/HK/US/FUTURES/CRYPTO) + exchange (SSE/SZSE/BSE/HKEX/
--      NASDAQ/NYSE/AMEX/SHFE/DCE/CZCE/CFFEX/INE/CRYPTO) 双维度。
--   3. 多券商：broker_type (tdx/futu/tiger/ib/qmt/manual)。
--   4. 幂等：UNIQUE (broker_type, exchange_order_id, trade_date)，重复归档用
--      ON CONFLICT DO NOTHING 跳过。
--   5. raw_payload JSONB 保留券商原始报文，字段差异不丢数据。
CREATE TABLE IF NOT EXISTS order_history (
    id                  SERIAL PRIMARY KEY,
    history_id          UUID NOT NULL UNIQUE,
    tenant_id           VARCHAR(64) NOT NULL DEFAULT 'default',
    user_id             VARCHAR(32) NOT NULL,
    account_id          VARCHAR(64),
    portfolio_id        INTEGER NOT NULL DEFAULT 0,
    strategy_id         INTEGER,

    -- 市场/交易所维度
    market              VARCHAR(16) NOT NULL,
    exchange            VARCHAR(16) NOT NULL,
    currency            VARCHAR(8) NOT NULL DEFAULT 'CNY',
    broker_type         VARCHAR(16) NOT NULL,

    -- 标的
    symbol              VARCHAR(32) NOT NULL,
    symbol_name         VARCHAR(64),

    -- 委托内容
    side                VARCHAR(8) NOT NULL,
    order_type          VARCHAR(16) NOT NULL DEFAULT 'market',
    status              VARCHAR(20) NOT NULL,
    quantity            FLOAT NOT NULL,
    filled_quantity     FLOAT NOT NULL DEFAULT 0,
    price               FLOAT,
    average_price       FLOAT,
    stop_price          FLOAT,

    -- 金额与费用
    order_value         FLOAT NOT NULL DEFAULT 0,
    filled_value        FLOAT NOT NULL DEFAULT 0,
    commission          FLOAT NOT NULL DEFAULT 0,
    stamp_duty          FLOAT NOT NULL DEFAULT 0,
    transfer_fee        FLOAT NOT NULL DEFAULT 0,
    total_fee           FLOAT NOT NULL DEFAULT 0,

    -- 时间
    trade_date          TIMESTAMP NOT NULL,
    submitted_at        TIMESTAMP,
    filled_at           TIMESTAMP,
    cancelled_at        TIMESTAMP,
    expired_at          TIMESTAMP,
    archived_at         TIMESTAMP NOT NULL DEFAULT NOW(),

    -- 溯源
    client_order_id     VARCHAR(100),
    exchange_order_id   VARCHAR(100),
    source              VARCHAR(32) NOT NULL DEFAULT 'bridge',
    remarks             VARCHAR(500),
    raw_payload         JSONB,

    created_at          TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMP NOT NULL DEFAULT NOW(),
    UNIQUE (broker_type, exchange_order_id, trade_date)
);

CREATE INDEX IF NOT EXISTS idx_order_history_market_exchange ON order_history (market, exchange);
CREATE INDEX IF NOT EXISTS idx_order_history_symbol_date ON order_history (symbol, trade_date);
CREATE INDEX IF NOT EXISTS idx_order_history_user_status ON order_history (user_id, status);
CREATE INDEX IF NOT EXISTS idx_order_history_archived ON order_history (archived_at);

