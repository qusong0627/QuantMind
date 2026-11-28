-- upgrade_v1.3.0: MCP 服务（v1/v2）的配置、按 key 覆盖与审计
-- 2026-11-28
--
-- 说明：
-- * 本迁移只建表/索引，不动 api_keys；MCP 权限复用 api_keys.permissions。
-- * API 启动时 mcp_admin_service.ensure_schema() 也会幂等自愈一次，
--   因此迁移未执行时接口仍可用（与 eval_contract 同口径）。

BEGIN;

CREATE TABLE IF NOT EXISTS qm_mcp_settings (
    id SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    tool_flags JSONB NOT NULL DEFAULT '{}'::jsonb,
    limits JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by TEXT
);

CREATE TABLE IF NOT EXISTS qm_mcp_key_settings (
    access_key TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    tool_flags JSONB NOT NULL DEFAULT '{}'::jsonb,
    limits JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS qm_mcp_audit (
    id BIGSERIAL PRIMARY KEY,
    ts TIMESTAMPTZ NOT NULL DEFAULT now(),
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    access_key_fp TEXT NOT NULL,
    session_id TEXT,
    tool TEXT NOT NULL,
    args_digest TEXT NOT NULL,
    status TEXT NOT NULL,
    error_code TEXT,
    latency_ms INTEGER,
    request_id TEXT,
    client_info TEXT
);

CREATE INDEX IF NOT EXISTS idx_qm_mcp_audit_ts ON qm_mcp_audit (ts DESC);
CREATE INDEX IF NOT EXISTS idx_qm_mcp_audit_key ON qm_mcp_audit (access_key_fp, ts DESC);

COMMIT;
