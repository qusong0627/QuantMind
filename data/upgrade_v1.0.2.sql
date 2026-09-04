-- ============================================================
-- QuantMind Database Upgrade Script v1.0.2
-- 「最近事件」系统运行事件表
-- ============================================================

-- 统一记录「系统运行相关事件」的持久化时间线。
-- 由 backend/shared/system_events.py 写入，管理后台 /admin/system-events 查询。
-- 幂等（CREATE TABLE IF NOT EXISTS），可重复执行。

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