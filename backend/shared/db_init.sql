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
    is_favorite       BOOLEAN DEFAULT FALSE,
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
    explanation         TEXT DEFAULT '',
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
-- 38b. RISK_EVENTS
-- ========================
CREATE TABLE IF NOT EXISTS risk_events (
    id              SERIAL PRIMARY KEY,
    rule_id         INTEGER,
    rule_type       VARCHAR(50) NOT NULL,
    tenant_id       VARCHAR(64) NOT NULL DEFAULT 'default',
    user_id         INTEGER NOT NULL,
    trade_date      DATE NOT NULL,
    symbol          VARCHAR(32) NOT NULL DEFAULT '*',
    action          VARCHAR(32) NOT NULL,
    status          VARCHAR(32) NOT NULL,
    trigger_price   DOUBLE PRECISION,
    cost_price      DOUBLE PRECISION,
    pnl_pct         DOUBLE PRECISION,
    quantity        DOUBLE PRECISION,
    order_ids       JSONB,
    message         VARCHAR(500),
    created_at      TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_risk_events_user_date
    ON risk_events (tenant_id, user_id, trade_date);
CREATE INDEX IF NOT EXISTS idx_risk_events_rule_date
    ON risk_events (rule_id, trade_date);
CREATE INDEX IF NOT EXISTS idx_risk_events_status
    ON risk_events (status);

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
-- 64. QM_STOCK_POOL 全局股票池 v2（回测/训练/推理/模拟盘/实盘唯一事实源）
-- 与 backend/shared/stock_pool/migrations/001_create_stock_pool.sql 保持一致。
-- 成员唯一事实源 = 前缀式 TXT（/data/stock_pool/<code>.txt，一行一个，保存即生效）；
-- 本表只存元信息；qm_stock_pool_binding 记录长生命周期引用（被引用不可删）。
-- ========================
CREATE TABLE IF NOT EXISTS qm_stock_pool (
    pool_id         TEXT PRIMARY KEY,
    code            TEXT NOT NULL,
    name            TEXT NOT NULL,
    description     TEXT,
    market          TEXT NOT NULL DEFAULT 'CN',
    pool_type       TEXT NOT NULL DEFAULT 'static',
    scope           TEXT NOT NULL DEFAULT 'global',
    tenant_id       TEXT,
    owner_user_id   TEXT,
    status          TEXT NOT NULL DEFAULT 'active',
    file_path       TEXT,
    symbol_count    INTEGER NOT NULL DEFAULT 0,
    checksum        TEXT,
    source_kind     TEXT,
    source_ref      TEXT,
    is_system       BOOLEAN NOT NULL DEFAULT FALSE,
    created_by      TEXT,
    updated_by      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_qm_stock_pool_code
    ON qm_stock_pool (scope, COALESCE(tenant_id, ''), code);

CREATE INDEX IF NOT EXISTS idx_qm_stock_pool_list
    ON qm_stock_pool (market, pool_type, status);

CREATE INDEX IF NOT EXISTS idx_qm_stock_pool_scope
    ON qm_stock_pool (scope, tenant_id, owner_user_id);

CREATE TABLE IF NOT EXISTS qm_stock_pool_binding (
    id              BIGSERIAL PRIMARY KEY,
    pool_id         TEXT NOT NULL REFERENCES qm_stock_pool(pool_id) ON DELETE CASCADE,
    target_type     TEXT NOT NULL,
    target_id       TEXT NOT NULL,
    mode            TEXT NOT NULL DEFAULT 'filter',
    priority        INTEGER NOT NULL DEFAULT 100,
    tenant_id       TEXT,
    user_id         TEXT,
    created_by      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (pool_id, target_type, target_id)
);

CREATE INDEX IF NOT EXISTS idx_qm_stock_pool_binding_target
    ON qm_stock_pool_binding (target_type, target_id);

CREATE INDEX IF NOT EXISTS idx_qm_stock_pool_binding_pool
    ON qm_stock_pool_binding (pool_id);

-- ========================
-- 65. SYSTEM_EVENTS（系统运行事件持久化时间线）
-- 原为 data/upgrade_v1.0.2.sql，已合并进本文件
-- 由 backend/shared/system_events.py 写入，管理后台 /admin/system-events 查询
-- ========================
CREATE TABLE IF NOT EXISTS system_events (
    id          BIGSERIAL PRIMARY KEY,
    event_type  VARCHAR(64)  NOT NULL,   -- service_lifecycle / health_transition / node_alert / data_sync / error
    level       VARCHAR(16)  NOT NULL DEFAULT 'info',   -- info / warning / error / critical
    source      VARCHAR(64)  NOT NULL,   -- quantmind-api / quantmind-engine / quantmind-stream / quantmind-trade / sync
    title       TEXT         NOT NULL,
    message     TEXT,
    meta        JSONB        DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_system_events_created ON system_events (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_system_events_type   ON system_events (event_type);
CREATE INDEX IF NOT EXISTS idx_system_events_level  ON system_events (level);

-- ========================
-- 默认管理员（admin / admin123）
-- NOTE: 幂等，仅在不存在时创建，不覆盖用户已改密码；display_name / 头像由启动期 seed_data.py 负责
-- ========================
INSERT INTO users (user_id, tenant_id, username, email, password_hash, is_active, is_admin, is_verified, is_locked, login_count, created_at, updated_at, is_deleted)
SELECT '10000001', 'default', 'admin', 'admin@quantmind.local',
        '$2b$12$B/yjK9cT.wx4BlB9j.r/t.dADjCbmutIXoDM7PdKZmV6ypuYiiUvW',
        TRUE, TRUE, TRUE, FALSE, 0, NOW(), NOW(), FALSE
WHERE NOT EXISTS (
    SELECT 1 FROM users WHERE tenant_id = 'default' AND username = 'admin'
)
ON CONFLICT (user_id) DO NOTHING;

-- ============================================================
-- 66. 存量库收敛（合并自 data/upgrade_v1.0.1 ~ v1.0.8）
--
-- 这 8 个增量补丁已合并进本文件，data/upgrade_v1.0.*.sql 不再单独维护。
-- 为什么需要本节（而不是只靠上面的建表语句）：
--   CREATE TABLE IF NOT EXISTS 不会改动已存在的表，所以「曾经按旧版本建过表」
--   的存量库即便跑过本文件也补齐不了新增列 / 唯一约束 / 数据订正。
-- 全部语句幂等（IF NOT EXISTS / DO + 异常兜底），可重复执行：
--   - 新库：上面第 1~65 节已直接建全，本节基本是 no-op；
--   - 存量库：靠本节把老结构拉齐，避免 column does not exist 等线上报错。
-- 写法约束：本节不得出现百分号字符（psycopg2 fallback 路径会误解析，
--           故统一用 quote_ident / quote_literal 拼接 + NOTICE 字符串连接）。
-- 执行时机：每次服务启动由 backend/main_oss.py 自动重跑，无需手工干预。
-- ============================================================

-- ---- 66.1（原 v1.0.1）real_trading_preflight_snapshots 唯一约束 ----
--     代码用 ON CONFLICT ON CONSTRAINT 依赖该约束；缺失会导致每次落库抛
--     UndefinedObjectError 并刷屏。
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'uq_preflight_snapshot_daily'
    ) THEN
        ALTER TABLE real_trading_preflight_snapshots
            ADD CONSTRAINT uq_preflight_snapshot_daily
            UNIQUE (tenant_id, user_id, trading_mode, snapshot_date);
    END IF;
END $$;

-- ---- 66.2（原 v1.0.3）news_article_enrichment 补 title 列 ----
--     enricher.py 的 upsert 引用该列，缺失会让 news_enrich_recent 首条写入即报错
ALTER TABLE news_article_enrichment
    ADD COLUMN IF NOT EXISTS title TEXT;

-- ---- 66.3（原 v1.0.4 / v1.0.5）全局股票池 v2 收敛 ----
--     v1 用「元信息 + 版本表 + 成员表 + parquet 快照」重型模型，v2 收敛为
--     元信息单表 + 前缀式 TXT 成员。上面第 64 节已按 v2 建全，本节只处理存量：
-- 元信息表补 file_path（若库还是 v1 结构）
ALTER TABLE qm_stock_pool ADD COLUMN IF NOT EXISTS file_path TEXT;

-- v1 遗留表清理（v2 不再使用）
DROP TABLE IF EXISTS qm_stock_pool_member;
DROP TABLE IF EXISTS qm_stock_pool_version;

-- 状态归一：v1 的 draft/published 行统一改为 active（无发布语义了）
UPDATE qm_stock_pool
   SET status = 'active'
 WHERE status IN ('draft', 'published');

-- ---- 66.4（原 v1.0.6）管理员 user_id 纠正：'admin' → '00000001' ----
--     背景：db_init.sql 曾 seed user_id='admin' 的坏行（username='admin' 正确），
--     且 seed 按 username 判存在后跳过。登录 JWT 的 sub 取 user_id，全链路
--     （信号表/池目录/Redis 键）用的都是它，与 8 位规范 ID 不一致。
--     整数型 user_id 列（strategies/replay_sessions 等存 users.id）不受影响。
--     Redis 键与 JWT 刷新不在 SQL 范围（重登一次即可；全量迁移跑
--     backend/scripts/migrate_legacy_user_ids.py）。
--     FK 说明：4 个指向 users(user_id) 的约束为即时检查，子表先改则子侧校验
--     失败、父表先改则父侧校验失败，故先卸后建（原名，与 db_init.sql 一致）。
DO $$
DECLARE
    t TEXT;
    n INT;
    fk TEXT[][];
    f TEXT[];
BEGIN
    fk := ARRAY[
        ['user_roles', 'user_roles_user_id_fkey'],
        ['identity_verifications', 'identity_verifications_user_id_fkey'],
        ['notifications', 'notifications_user_id_fkey'],
        ['password_reset_tokens', 'password_reset_tokens_user_id_fkey']
    ];

    -- 1. 卸 FK（IF EXISTS，老库无约束也不报错）
    FOREACH f SLICE 1 IN ARRAY fk LOOP
        BEGIN
            EXECUTE 'ALTER TABLE ' || quote_ident(f[1])
                || ' DROP CONSTRAINT IF EXISTS ' || quote_ident(f[2]);
        EXCEPTION WHEN OTHERS THEN
            RAISE NOTICE 'merge 66.4 drop FK skipped';
        END;
    END LOOP;

    -- 2. 全库字符型 user_id 列 sweep（users 表最后单独处理）
    FOR t IN
        SELECT table_name FROM information_schema.columns
        WHERE table_schema = 'public' AND column_name = 'user_id'
          AND data_type IN ('character varying', 'character', 'text')
          AND table_name <> 'users'
        ORDER BY table_name
    LOOP
        BEGIN
            EXECUTE 'UPDATE ' || quote_ident(t)
                || ' SET user_id = ' || quote_literal('00000001')
                || ' WHERE user_id = ' || quote_literal('admin');
            GET DIAGNOSTICS n = ROW_COUNT;
            IF n > 0 THEN
                RAISE NOTICE 'merge 66.4 sweep done';
            END IF;
        EXCEPTION WHEN OTHERS THEN
            RAISE NOTICE 'merge 66.4 sweep skipped';
        END;
    END LOOP;

    -- 3. users 主行
    BEGIN
        UPDATE users SET user_id = '00000001' WHERE user_id = 'admin';
        GET DIAGNOSTICS n = ROW_COUNT;
        IF n > 0 THEN
            RAISE NOTICE 'merge 66.4 users row fixed';
        END IF;
    EXCEPTION WHEN OTHERS THEN
        RAISE NOTICE 'merge 66.4 sweep users skipped';
    END;

    -- 4. 原名建回 FK
    FOREACH f SLICE 1 IN ARRAY fk LOOP
        BEGIN
            EXECUTE 'ALTER TABLE ' || quote_ident(f[1])
                || ' ADD CONSTRAINT ' || quote_ident(f[2])
                || ' FOREIGN KEY (user_id) REFERENCES users(user_id)';
        EXCEPTION WHEN OTHERS THEN
            RAISE NOTICE 'merge 66.4 add FK skipped';
        END;
    END LOOP;

    -- 5. 模拟盘整型/字符 user_id：sim 账户 0 → 1。
    --    背景：旧 admin 的 sub 非数字，sim 侧被映射为 user 0；纠正后新身份读
    --    user 1，资金会 stranded。仅当目标 user 1 完全无行时才搬（NOT EXISTS
    --    守卫，绝不覆盖已有账户）；部分表已存在 user 1 行时跳过并 NOTICE。
    FOR t IN
        SELECT x FROM (VALUES
            ('simulation_accounts'),
            ('simulation_cash_ledger'),
            ('simulation_fills'),
            ('simulation_position_lots'),
            ('simulation_position_daily'),
            ('simulation_account_daily'),
            ('simulation_fund_snapshots'),
            ('simulation_orders'),
            ('simulation_rebalance_jobs'),
            ('simulation_reconcile_reports'),
            ('sim_orders'),
            ('sim_trades')
        ) AS v(x)
        WHERE EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = v.x
              AND column_name = 'user_id'
        )
        ORDER BY 1
    LOOP
        BEGIN
            EXECUTE 'UPDATE ' || quote_ident(t)
                || ' SET user_id = ' || quote_literal('1')
                || ' WHERE user_id::text = ' || quote_literal('0')
                || ' AND NOT EXISTS (SELECT 1 FROM ' || quote_ident(t)
                || ' WHERE user_id::text = ' || quote_literal('1') || ')';
            GET DIAGNOSTICS n = ROW_COUNT;
            IF n > 0 THEN
                RAISE NOTICE 'merge 66.4 sim moved';
            END IF;
        EXCEPTION WHEN OTHERS THEN
            RAISE NOTICE 'merge 66.4 sim skipped';
        END;
    END LOOP;
END $$;

-- ---- 66.5（原 v1.0.7）模拟成交瞬时列统一为 TIMESTAMPTZ ----
--     背景：sim_trades.executed_at 在旧升级脚本里是 timestamptz，新 ORM 一度按
--     TIMESTAMP WITHOUT TIME ZONE 写 naive UTC。asyncpg 按真实列类型编码，
--     旧库（timestamptz + naive）和新库（timestamp + aware）会交替报
--     "can't subtract offset-naive and offset-aware datetimes"，成交整笔回滚。
--     本段把瞬时列统一成 timestamptz，与 UtcDateTime + aware UTC 写入对齐
--     （存量 naive 值按 UTC 解释，禁止当上海墙钟）。
DO $$
DECLARE
    rec RECORD;
BEGIN
    FOR rec IN
        SELECT c.table_name, c.column_name
        FROM information_schema.columns c
        WHERE c.table_schema = 'public'
          AND c.table_name IN ('sim_trades', 'sim_orders')
          AND c.column_name IN (
              'executed_at', 'submitted_at', 'filled_at', 'cancelled_at',
              'created_at', 'updated_at'
          )
          AND c.data_type = 'timestamp without time zone'
    LOOP
        EXECUTE 'ALTER TABLE ' || quote_ident(rec.table_name)
            || ' ALTER COLUMN ' || quote_ident(rec.column_name)
            || ' TYPE timestamptz USING '
            || quote_ident(rec.column_name)
            || ' AT TIME ZONE ' || quote_literal('UTC');
        RAISE NOTICE 'merge 66.5 converted timestamp column to timestamptz';
    END LOOP;
END $$;

-- ---- 66.6（原 v1.0.7 / v1.0.8）管理员 user_id 收口为 10000001 ----
--     v1.0.7 把 users.user_id 纠正和加 FK 拆成独立语句（v1.0.6 放同一 DO 块，
--     后面失败会整段回滚，线上仍可能停在 user_id='admin'）。
--     v1.0.8 再把 00000001 收口成 10000001：规范 ID 曾以 0 开头，
--     Python int('00000001') = 1，JWT sub 与模拟账户键错位，仪表盘显示总资产 0。
--     本节只改字符型 user_id（含 users）以及模拟盘整数 user_id
--     （sim_orders / sim_trades）。strategies.user_id 存的是 users.id
--     主键，不要改。
ALTER TABLE user_roles DROP CONSTRAINT IF EXISTS user_roles_user_id_fkey;
ALTER TABLE identity_verifications DROP CONSTRAINT IF EXISTS identity_verifications_user_id_fkey;
ALTER TABLE notifications DROP CONSTRAINT IF EXISTS notifications_user_id_fkey;
ALTER TABLE password_reset_tokens DROP CONSTRAINT IF EXISTS password_reset_tokens_user_id_fkey;

DO $$
DECLARE
    t TEXT;
    old_id TEXT;
BEGIN
    FOREACH old_id IN ARRAY ARRAY['admin', '00000001']
    LOOP
        FOR t IN
            SELECT table_name FROM information_schema.columns
            WHERE table_schema = 'public' AND column_name = 'user_id'
              AND data_type IN ('character varying', 'character', 'text')
              AND table_name <> 'users'
            ORDER BY table_name
        LOOP
            BEGIN
                EXECUTE 'UPDATE ' || quote_ident(t)
                    || ' SET user_id = ' || quote_literal('10000001')
                    || ' WHERE user_id = ' || quote_literal(old_id);
            EXCEPTION WHEN OTHERS THEN
                RAISE NOTICE 'merge 66.6 sweep skipped';
            END;
        END LOOP;
    END LOOP;
END $$;

UPDATE users SET user_id = '10000001'
 WHERE user_id IN ('admin', '00000001')
   AND NOT EXISTS (SELECT 1 FROM users WHERE user_id = '10000001');

DO $$
BEGIN
    BEGIN
        ALTER TABLE user_roles
            ADD CONSTRAINT user_roles_user_id_fkey
            FOREIGN KEY (user_id) REFERENCES users(user_id);
    EXCEPTION WHEN duplicate_object THEN
        NULL;
    WHEN OTHERS THEN
        RAISE NOTICE 'merge 66.6 add user_roles FK skipped';
    END;
    BEGIN
        ALTER TABLE identity_verifications
            ADD CONSTRAINT identity_verifications_user_id_fkey
            FOREIGN KEY (user_id) REFERENCES users(user_id);
    EXCEPTION WHEN duplicate_object THEN
        NULL;
    WHEN OTHERS THEN
        RAISE NOTICE 'merge 66.6 add identity_verifications FK skipped';
    END;
    BEGIN
        ALTER TABLE notifications
            ADD CONSTRAINT notifications_user_id_fkey
            FOREIGN KEY (user_id) REFERENCES users(user_id);
    EXCEPTION WHEN duplicate_object THEN
        NULL;
    WHEN OTHERS THEN
        RAISE NOTICE 'merge 66.6 add notifications FK skipped';
    END;
    BEGIN
        ALTER TABLE password_reset_tokens
            ADD CONSTRAINT password_reset_tokens_user_id_fkey
            FOREIGN KEY (user_id) REFERENCES users(user_id);
    EXCEPTION WHEN duplicate_object THEN
        NULL;
    WHEN OTHERS THEN
        RAISE NOTICE 'merge 66.6 add password_reset_tokens FK skipped';
    END;
END $$;

-- 模拟资金快照：历史 0 / 1 / 00000001 / admin -> 10000001
-- 同一天已有规范行则跳过，避免 UNIQUE (tenant_id, user_id, snapshot_date)
DO $$
BEGIN
    IF to_regclass('public.simulation_fund_snapshots') IS NULL THEN
        RETURN;
    END IF;
    UPDATE simulation_fund_snapshots s
       SET user_id = '10000001'
     WHERE s.user_id IN ('0', '1', '00000001', 'admin')
       AND NOT EXISTS (
           SELECT 1 FROM simulation_fund_snapshots x
            WHERE x.tenant_id = s.tenant_id
              AND x.user_id = '10000001'
              AND x.snapshot_date = s.snapshot_date
       );
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'merge 66.6 snapshots skipped';
END $$;

-- 模拟委托/成交整数 user_id：0 / 1 -> 10000001
DO $$
BEGIN
    IF to_regclass('public.sim_orders') IS NOT NULL THEN
        UPDATE sim_orders SET user_id = 10000001 WHERE user_id IN (0, 1);
    END IF;
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'merge 66.6 sim_orders skipped';
END $$;

DO $$
BEGIN
    IF to_regclass('public.sim_trades') IS NOT NULL THEN
        UPDATE sim_trades SET user_id = 10000001 WHERE user_id IN (0, 1);
    END IF;
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'merge 66.6 sim_trades skipped';
END $$;

-- ========================
-- 67. QM_USER_FACTOR_GENERATIONS 用户因子训练数据集自动发布（generation + active 指针）
-- ========================
-- 企业版 alpha_user_factor_generations / alpha_user_factor_dataset_active 语义的
-- OSS 下放：promote 成功后自动建代次并切换指针，失败不切换；训练侧只读指针。
-- OSS 单容器无租户隔离与因子任务队列，故不分 tenant/user、不外键 alpha_factor_jobs。
-- 存量库靠 data/upgrade_v1.0.15.sql 拉齐（与本节同 DDL，幂等可重放）。
CREATE TABLE IF NOT EXISTS qm_user_factor_generations (
    generation_id   TEXT PRIMARY KEY,
    dataset_key     TEXT NOT NULL DEFAULT 'l1_factors',
    status          TEXT NOT NULL DEFAULT 'building',
    feature_key     TEXT NOT NULL,
    factor_name     TEXT NOT NULL DEFAULT '',
    expression_sha256 TEXT NOT NULL DEFAULT '',
    manifest_json   TEXT NOT NULL DEFAULT '{}',
    row_count       INTEGER NOT NULL DEFAULT 0,
    byte_count      INTEGER NOT NULL DEFAULT 0,
    error_message   TEXT NOT NULL DEFAULT '',
    created_by      TEXT NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    published_at    TIMESTAMPTZ,
    CHECK (dataset_key IN ('l1_factors', 'rd_mined')),
    CHECK (status IN ('building', 'published', 'failed', 'archived'))
);

CREATE TABLE IF NOT EXISTS qm_user_factor_dataset_active (
    dataset_key     TEXT PRIMARY KEY,
    generation_id   TEXT NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (dataset_key IN ('l1_factors', 'rd_mined'))
);

CREATE INDEX IF NOT EXISTS idx_qm_user_factor_generations_ds_status
    ON qm_user_factor_generations (dataset_key, status, created_at DESC);

-- ========================
-- 68. 因子挖掘数据库表（企业版下放）
-- ========================
-- 企业版 data/upgrade_v1.0.29 ~ v1.0.65 下放：alpha_mining_* 任务链、
-- alpha_factor_* 任务/验证/组合/物化、rd_agent_factor_* 池/血缘/修订/生命周期、
-- rd_agent_docs 文档链、alpha_variant_* 变体、alpha_research_* 研究调用、
-- alpha_direction_batches、user_papers、qm_research_pipeline_runs，共 30 表。
-- 存量库靠 data/upgrade_v1.0.16.sql 拉齐（与本节同 DDL，幂等可重放）。
-- OSS 单容器无 resource_task_leases / CVM 设施，指向它们的父表外键省略；
-- resource_lease_pools 建表 + alpha_mining 种子行见本节末尾 Q 节；
-- tenant_id / user_id 数据列与原约束保留，企业版 SQL 零改动复用。

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
--    data/upgrade_v1.0.16.sql 末尾 Q 节同 DDL，幂等可重放）。
--    alpha_mining 池种子行 ready=true：AlphaMiningTaskPersistence.create /
--    request_cancel 建池锁要求 SELECT ready ... FOR UPDATE 为真，否则抛
--    LeaseUnavailable。ON CONFLICT 仅覆盖 ready（保留 reconciler 维护的 epoch）。
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

-- resource_task_leases：任务租约（企业版原 v1.0.17，与 pools 同段，upgrade_v1.0.16.sql 末尾 R 节同 DDL）。
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

-- rd_agent_factors 补 tenant_id（企业版口径对齐；upgrade_v1.0.16.sql 末尾 R 节同 DDL）。
ALTER TABLE rd_agent_factors ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS idx_rd_agent_factors_tenant_user
    ON rd_agent_factors (tenant_id, user_id);

-- S. 推理作业持久队列（企业版 data/upgrade_v1.0.25.sql 下放）。
-- backend/services/engine/inference/jobs.py 的作业泵读写此表；缺表会每轮报
-- UndefinedTable 并空转。owner 双维 + 幂等键唯一，单容器下仍按用户隔离。
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

-- ========================
-- DONE - 所有缺失表已创建
-- ========================
-- 注：后续增量补丁不再进第 66 节，一律新建 data/upgrade_vX.Y.Z.sql
-- （如 strategies.is_favorite 见 data/upgrade_v1.0.12.sql），由
-- backend/main_oss.py 启动扫描重放 + deploy/update.sh 去重执行。
