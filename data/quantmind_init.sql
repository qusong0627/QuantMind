--
-- QuantMind OSS Edition - Initial Database Schema
-- Version: 1.0.0
-- Description: Minimal schema for open-source deployment
--

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

-- ============================================================================
-- ENUM Types
-- ============================================================================

CREATE TYPE public.orderside AS ENUM ('buy', 'sell');
CREATE TYPE public.orderstatus AS ENUM ('pending', 'submitted', 'partially_filled', 'filled', 'cancelled', 'rejected', 'expired');
CREATE TYPE public.ordertype AS ENUM ('market', 'limit', 'stop', 'stop_limit');
CREATE TYPE public.positionside AS ENUM ('long', 'short');
CREATE TYPE public.simulationstatus AS ENUM ('RUNNING', 'PAUSED', 'STOPPED', 'ERROR');
CREATE TYPE public.strategystatus AS ENUM ('DRAFT', 'REPOSITORY', 'LIVE_TRADING', 'ACTIVE', 'PAUSED', 'STOPPED', 'ARCHIVED');
CREATE TYPE public.strategytype AS ENUM ('TOPK_DROPOUT', 'WEIGHT_STRATEGY', 'CUSTOM', 'LONG_SHORT_TOPK');
CREATE TYPE public.tradeaction AS ENUM ('buy', 'sell');
CREATE TYPE public.tradingmode AS ENUM ('BACKTEST', 'SIMULATION', 'LIVE', 'REAL');

-- ============================================================================
-- Core Tables: Users & Auth
-- ============================================================================

CREATE TABLE public.users (
    id SERIAL PRIMARY KEY,
    user_id VARCHAR(64) NOT NULL UNIQUE,
    tenant_id VARCHAR(64) NOT NULL DEFAULT 'default',
    username VARCHAR(128) NOT NULL,
    email VARCHAR(255),
    phone_number VARCHAR(32),
    password_hash VARCHAR(255) NOT NULL,
    is_active BOOLEAN DEFAULT true,
    is_verified BOOLEAN DEFAULT false,
    is_admin BOOLEAN DEFAULT false,
    is_locked BOOLEAN DEFAULT false,
    last_login_at TIMESTAMPTZ,
    last_login_ip VARCHAR(64),
    login_count INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now(),
    is_deleted BOOLEAN DEFAULT false,
    deleted_at TIMESTAMPTZ
);

CREATE TABLE public.user_profiles (
    id SERIAL PRIMARY KEY,
    user_id VARCHAR(64) NOT NULL,
    tenant_id VARCHAR(64) NOT NULL DEFAULT 'default',
    nickname VARCHAR(128),
    avatar_url VARCHAR(500),
    bio TEXT,
    preferences JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE public.user_sessions (
    id VARCHAR(64) DEFAULT gen_random_uuid()::VARCHAR(64),
    session_id VARCHAR(64) PRIMARY KEY,
    user_id VARCHAR(64) NOT NULL,
    tenant_id VARCHAR(64) NOT NULL DEFAULT 'default',
    token_hash VARCHAR(255),
    token_jti VARCHAR(64),
    refresh_token VARCHAR(1024),
    refresh_token_expires_at TIMESTAMPTZ,
    device_info VARCHAR(255),
    user_agent VARCHAR(255),
    ip_address VARCHAR(64),
    expires_at TIMESTAMPTZ NOT NULL,
    last_activity_at TIMESTAMPTZ,
    last_active_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    is_active BOOLEAN DEFAULT true,
    is_revoked BOOLEAN DEFAULT false,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE public.api_keys (
    id SERIAL PRIMARY KEY,
    user_id VARCHAR(64) NOT NULL,
    tenant_id VARCHAR(64) NOT NULL DEFAULT 'default',
    key_hash VARCHAR(255) NOT NULL,
    key_prefix VARCHAR(16) NOT NULL,
    name VARCHAR(128),
    permissions JSONB DEFAULT '[]',
    last_used_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ,
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- ============================================================================
-- Core Tables: Strategies
-- ============================================================================

CREATE TABLE public.strategies (
    id SERIAL PRIMARY KEY,
    user_id VARCHAR(64) NOT NULL,
    tenant_id VARCHAR(64) NOT NULL DEFAULT 'default',
    name VARCHAR(200) NOT NULL,
    description TEXT,
    strategy_type public.strategytype NOT NULL DEFAULT 'TOPK_DROPOUT',
    status public.strategystatus NOT NULL DEFAULT 'DRAFT',
    config JSONB NOT NULL DEFAULT '{}',
    parameters JSONB NOT NULL DEFAULT '{}',
    code TEXT,
    cos_url VARCHAR(500),
    code_hash VARCHAR(64),
    file_size INTEGER,
    tags TEXT[] DEFAULT ARRAY[]::TEXT[],
    is_public BOOLEAN DEFAULT false,
    backtest_count INTEGER DEFAULT 0,
    view_count INTEGER DEFAULT 0,
    like_count INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now(),
    version INTEGER DEFAULT 1
);

CREATE TABLE public.user_strategies (
    id VARCHAR(64) PRIMARY KEY,
    user_id VARCHAR(64) NOT NULL,
    tenant_id VARCHAR(64) NOT NULL DEFAULT 'default',
    strategy_name VARCHAR(255) NOT NULL,
    description TEXT,
    conditions JSONB DEFAULT '{}',
    stock_pool JSONB DEFAULT '{}',
    position_config JSONB DEFAULT '{}',
    style VARCHAR(64),
    risk_config JSONB DEFAULT '{}',
    cos_url TEXT,
    file_size INTEGER,
    code_hash VARCHAR(128),
    qlib_validated BOOLEAN DEFAULT false,
    validation_result JSONB DEFAULT '{}',
    tags TEXT[] DEFAULT ARRAY[]::TEXT[],
    is_public BOOLEAN DEFAULT false,
    downloads INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE public.stock_pool_files (
    id SERIAL PRIMARY KEY,
    tenant_id VARCHAR(50) DEFAULT 'default',
    user_id VARCHAR(50) NOT NULL,
    pool_name VARCHAR(200),
    session_id VARCHAR(100),
    file_key VARCHAR(500) NOT NULL,
    file_url VARCHAR(1000),
    relative_path VARCHAR(500),
    format VARCHAR(10) DEFAULT 'csv',
    file_size INTEGER,
    code_hash VARCHAR(64),
    stock_count INTEGER,
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- ============================================================================
-- Core Tables: Backtest
-- ============================================================================

CREATE TABLE public.qlib_backtest_runs (
    id VARCHAR(64) PRIMARY KEY,
    user_id VARCHAR(64) NOT NULL,
    tenant_id VARCHAR(64) NOT NULL DEFAULT 'default',
    strategy_id VARCHAR(64),
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    config JSONB NOT NULL DEFAULT '{}',
    result JSONB,
    error_message TEXT,
    task_id VARCHAR(64),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    execution_time_seconds FLOAT,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE public.qlib_optimization_runs (
    id VARCHAR(64) PRIMARY KEY,
    user_id VARCHAR(64) NOT NULL,
    tenant_id VARCHAR(64) NOT NULL DEFAULT 'default',
    strategy_id VARCHAR(64),
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    param_grid JSONB NOT NULL DEFAULT '{}',
    best_params JSONB,
    best_result JSONB,
    all_results JSONB DEFAULT '[]',
    total_combinations INTEGER,
    completed_combinations INTEGER DEFAULT 0,
    task_id VARCHAR(64),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- ============================================================================
-- Core Tables: Simulation Trading
-- ============================================================================

CREATE TABLE public.simulation_jobs (
    id VARCHAR(64) PRIMARY KEY,
    user_id VARCHAR(64) NOT NULL,
    tenant_id VARCHAR(64) NOT NULL DEFAULT 'default',
    strategy_id VARCHAR(64),
    backtest_id VARCHAR(64),
    status public.simulationstatus NOT NULL DEFAULT 'RUNNING',
    initial_capital NUMERIC(18,2) NOT NULL,
    current_capital NUMERIC(18,2),
    config JSONB NOT NULL DEFAULT '{}',
    started_at TIMESTAMPTZ,
    stopped_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE public.sim_orders (
    id VARCHAR(64) PRIMARY KEY,
    job_id VARCHAR(64) NOT NULL,
    user_id VARCHAR(64) NOT NULL,
    tenant_id VARCHAR(64) NOT NULL DEFAULT 'default',
    symbol VARCHAR(20) NOT NULL,
    side public.orderside NOT NULL,
    order_type public.ordertype NOT NULL DEFAULT 'market',
    quantity NUMERIC(18,4) NOT NULL,
    price NUMERIC(18,4),
    status public.orderstatus NOT NULL DEFAULT 'pending',
    filled_quantity NUMERIC(18,4) DEFAULT 0,
    filled_price NUMERIC(18,4),
    commission NUMERIC(18,4) DEFAULT 0,
    signal_time TIMESTAMPTZ,
    submit_time TIMESTAMPTZ,
    fill_time TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE public.sim_trades (
    id VARCHAR(64) PRIMARY KEY,
    job_id VARCHAR(64) NOT NULL,
    order_id VARCHAR(64) NOT NULL,
    user_id VARCHAR(64) NOT NULL,
    tenant_id VARCHAR(64) NOT NULL DEFAULT 'default',
    symbol VARCHAR(20) NOT NULL,
    side public.orderside NOT NULL,
    quantity NUMERIC(18,4) NOT NULL,
    price NUMERIC(18,4) NOT NULL,
    commission NUMERIC(18,4) DEFAULT 0,
    trade_time TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE public.simulation_positions (
    id SERIAL PRIMARY KEY,
    job_id VARCHAR(64) NOT NULL,
    symbol VARCHAR(20) NOT NULL,
    side public.positionside NOT NULL,
    quantity NUMERIC(18,4) NOT NULL DEFAULT 0,
    avg_cost NUMERIC(18,4),
    market_value NUMERIC(18,4),
    unrealized_pnl NUMERIC(18,4),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE public.simulation_fund_snapshots (
    id SERIAL PRIMARY KEY,
    job_id VARCHAR(64) NOT NULL,
    snapshot_date DATE NOT NULL,
    total_capital NUMERIC(18,2) NOT NULL,
    cash NUMERIC(18,2) NOT NULL,
    position_value NUMERIC(18,2) NOT NULL,
    daily_return NUMERIC(10,6),
    cumulative_return NUMERIC(10,6),
    created_at TIMESTAMPTZ DEFAULT now()
);

-- ============================================================================
-- Core Tables: Market Data
-- ============================================================================

CREATE TABLE public.stocks (
    id SERIAL PRIMARY KEY,
    symbol VARCHAR(20) NOT NULL UNIQUE,
    name VARCHAR(100),
    exchange VARCHAR(20),
    industry VARCHAR(50),
    sector VARCHAR(50),
    list_date DATE,
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE public.market_data_daily (
    id SERIAL PRIMARY KEY,
    symbol VARCHAR(20) NOT NULL,
    trade_date DATE NOT NULL,
    open NUMERIC(10,3),
    high NUMERIC(10,3),
    low NUMERIC(10,3),
    close NUMERIC(10,3),
    volume BIGINT,
    amount NUMERIC(18,2),
    turnover_rate NUMERIC(10,6),
    -- 48维特征字段 (由外部特征服务填充)
    feat_01 NUMERIC(10,6),
    feat_02 NUMERIC(10,6),
    feat_03 NUMERIC(10,6),
    feat_04 NUMERIC(10,6),
    feat_05 NUMERIC(10,6),
    feat_06 NUMERIC(10,6),
    feat_07 NUMERIC(10,6),
    feat_08 NUMERIC(10,6),
    feat_09 NUMERIC(10,6),
    feat_10 NUMERIC(10,6),
    feat_11 NUMERIC(10,6),
    feat_12 NUMERIC(10,6),
    feat_13 NUMERIC(10,6),
    feat_14 NUMERIC(10,6),
    feat_15 NUMERIC(10,6),
    feat_16 NUMERIC(10,6),
    feat_17 NUMERIC(10,6),
    feat_18 NUMERIC(10,6),
    feat_19 NUMERIC(10,6),
    feat_20 NUMERIC(10,6),
    feat_21 NUMERIC(10,6),
    feat_22 NUMERIC(10,6),
    feat_23 NUMERIC(10,6),
    feat_24 NUMERIC(10,6),
    feat_25 NUMERIC(10,6),
    feat_26 NUMERIC(10,6),
    feat_27 NUMERIC(10,6),
    feat_28 NUMERIC(10,6),
    feat_29 NUMERIC(10,6),
    feat_30 NUMERIC(10,6),
    feat_31 NUMERIC(10,6),
    feat_32 NUMERIC(10,6),
    feat_33 NUMERIC(10,6),
    feat_34 NUMERIC(10,6),
    feat_35 NUMERIC(10,6),
    feat_36 NUMERIC(10,6),
    feat_37 NUMERIC(10,6),
    feat_38 NUMERIC(10,6),
    feat_39 NUMERIC(10,6),
    feat_40 NUMERIC(10,6),
    feat_41 NUMERIC(10,6),
    feat_42 NUMERIC(10,6),
    feat_43 NUMERIC(10,6),
    feat_44 NUMERIC(10,6),
    feat_45 NUMERIC(10,6),
    feat_46 NUMERIC(10,6),
    feat_47 NUMERIC(10,6),
    feat_48 NUMERIC(10,6),
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE(symbol, trade_date)
);

CREATE TABLE public.stock_daily_latest (
    id SERIAL PRIMARY KEY,
    symbol VARCHAR(20) NOT NULL UNIQUE,
    trade_date DATE NOT NULL,
    open NUMERIC(10,3),
    high NUMERIC(10,3),
    low NUMERIC(10,3),
    close NUMERIC(10,3),
    volume BIGINT,
    amount NUMERIC(18,2),
    change_pct NUMERIC(10,6),
    turnover_rate NUMERIC(10,6),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- ============================================================================
-- Core Tables: Model & Inference
-- ============================================================================

CREATE TABLE public.qm_user_models (
    id VARCHAR(64) PRIMARY KEY,
    user_id VARCHAR(64) NOT NULL,
    tenant_id VARCHAR(64) NOT NULL DEFAULT 'default',
    model_name VARCHAR(200) NOT NULL,
    model_type VARCHAR(50),
    model_path VARCHAR(500),
    config JSONB DEFAULT '{}',
    metrics JSONB DEFAULT '{}',
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE public.qm_model_inference_runs (
    id VARCHAR(64) PRIMARY KEY,
    user_id VARCHAR(64) NOT NULL,
    tenant_id VARCHAR(64) NOT NULL DEFAULT 'default',
    model_id VARCHAR(64),
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    config JSONB NOT NULL DEFAULT '{}',
    result_path VARCHAR(500),
    metrics JSONB,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- ============================================================================
-- Core Tables: Notifications
-- ============================================================================

CREATE TABLE public.notifications (
    id SERIAL PRIMARY KEY,
    user_id VARCHAR(64) NOT NULL,
    tenant_id VARCHAR(64) NOT NULL DEFAULT 'default',
    notification_type VARCHAR(50) NOT NULL,
    title VARCHAR(200) NOT NULL,
    content TEXT,
    data JSONB DEFAULT '{}',
    is_read BOOLEAN DEFAULT false,
    read_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- ============================================================================
-- Core Tables: System
-- ============================================================================

CREATE TABLE public.system_settings (
    key VARCHAR(100) PRIMARY KEY,
    value JSONB NOT NULL,
    description TEXT,
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE public.audit_logs (
    id SERIAL PRIMARY KEY,
    user_id VARCHAR(64),
    tenant_id VARCHAR(64) DEFAULT 'default',
    action VARCHAR(100) NOT NULL,
    resource_type VARCHAR(50),
    resource_id VARCHAR(100),
    old_value JSONB,
    new_value JSONB,
    ip_address VARCHAR(64),
    user_agent VARCHAR(255),
    created_at TIMESTAMPTZ DEFAULT now()
);

-- ============================================================================
-- Indexes
-- ============================================================================

CREATE INDEX idx_users_user_id ON public.users(user_id);
CREATE INDEX idx_users_tenant_id ON public.users(tenant_id);
CREATE INDEX idx_user_sessions_user_id ON public.user_sessions(user_id);
CREATE INDEX idx_api_keys_user_id ON public.api_keys(user_id);
CREATE INDEX idx_strategies_user_id ON public.strategies(user_id);
CREATE INDEX idx_user_strategies_user_id ON public.user_strategies(user_id);
CREATE INDEX idx_qlib_backtest_runs_user_id ON public.qlib_backtest_runs(user_id);
CREATE INDEX idx_qlib_backtest_runs_status ON public.qlib_backtest_runs(status);
CREATE INDEX idx_simulation_jobs_user_id ON public.simulation_jobs(user_id);
CREATE INDEX idx_sim_orders_job_id ON public.sim_orders(job_id);
CREATE INDEX idx_sim_trades_job_id ON public.sim_trades(job_id);
CREATE INDEX idx_market_data_daily_symbol ON public.market_data_daily(symbol);
CREATE INDEX idx_market_data_daily_date ON public.market_data_daily(trade_date);
CREATE INDEX idx_market_data_daily_symbol_date ON public.market_data_daily(symbol, trade_date);
CREATE INDEX idx_notifications_user_id ON public.notifications(user_id);
CREATE INDEX idx_audit_logs_user_id ON public.audit_logs(user_id);
CREATE INDEX idx_audit_logs_created_at ON public.audit_logs(created_at);

-- ============================================================================
-- Initial Data: Default Admin User
-- ============================================================================

INSERT INTO public.users (user_id, tenant_id, username, email, password_hash, is_active, is_verified, is_admin)
VALUES ('admin', 'default', 'admin', 'admin@quantmind.local', 
        '$2b$12$B/yjK9cT.wx4BlB9j.r/t.dADjCbmutIXoDM7PdKZmV6ypuYiiUvW', 
        true, true, true);

INSERT INTO public.user_profiles (user_id, tenant_id, nickname)
VALUES ('admin', 'default', 'Administrator');

-- ============================================================================
-- Initial Data: System Settings
-- ============================================================================

INSERT INTO public.system_settings (key, value, description) VALUES
('storage_mode', '"local"', 'Storage mode: local or cloud'),
('backtest.default_initial_capital', '100000000', 'Default initial capital for backtest'),
('backtest.default_benchmark', '"SH000300"', 'Default benchmark index'),
('backtest.max_concurrent_runs', '10', 'Maximum concurrent backtest runs'),
('simulation.max_concurrent_jobs', '5', 'Maximum concurrent simulation jobs');

-- ============================================================================
-- Grant Permissions
-- ============================================================================

GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO quantmind;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO quantmind;
GRANT USAGE ON SCHEMA public TO quantmind;
