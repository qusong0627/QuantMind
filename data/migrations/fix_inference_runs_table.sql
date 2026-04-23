-- Migration: Fix qm_model_inference_runs table structure
-- Date: 2026-04-23
-- Issue: Column names mismatch between code and database
--   - Code uses: run_id (primary key)
--   - Database has: id (primary key)

-- Drop existing table and recreate with correct structure
DROP TABLE IF EXISTS public.qm_model_inference_runs;

CREATE TABLE public.qm_model_inference_runs (
    run_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL DEFAULT 'default',
    user_id TEXT NOT NULL,
    model_id TEXT NOT NULL,
    data_trade_date DATE NOT NULL,
    prediction_trade_date DATE NOT NULL,
    status TEXT NOT NULL,
    signals_count INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER,
    fallback_used BOOLEAN NOT NULL DEFAULT FALSE,
    fallback_reason TEXT,
    failure_stage TEXT,
    error_message TEXT,
    stdout TEXT,
    stderr TEXT,
    active_model_id TEXT,
    effective_model_id TEXT,
    model_source TEXT,
    active_data_source TEXT,
    request_json JSONB,
    result_json JSONB,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

-- Create indexes
CREATE INDEX idx_qm_model_inference_runs_owner_created
    ON qm_model_inference_runs (tenant_id, user_id, created_at DESC);

CREATE INDEX idx_qm_model_inference_runs_model_status
    ON qm_model_inference_runs (tenant_id, user_id, model_id, status, created_at DESC);

CREATE INDEX idx_qm_model_inference_runs_target_date
    ON qm_model_inference_runs (tenant_id, user_id, prediction_trade_date DESC);

ALTER TABLE public.qm_model_inference_runs OWNER TO quantmind;