-- upgrade_v1.1.9: 训练目标注册表（Target Contract）
--
-- 机构级约束：
--   1. 只允许 CREATE TABLE / CREATE INDEX / CREATE OR REPLACE FUNCTION /
--      DROP TRIGGER IF EXISTS / CREATE TRIGGER（全部幂等），不得出现
--      DROP TABLE / TRUNCATE / DELETE FROM / DROP COLUMN——启动期自动迁移
--      （backend/main_oss.py::_upgrade_sql_files）会跳过破坏性脚本。
--   2. 同一 (id, version) 的 spec / spec_hash / market / family / mode 永不可改；
--      改口径必须新增 version。触发器在库层兜底，不依赖应用自觉。
--   3. 状态只允许 draft -> active -> deprecated（draft 可直接废弃）。
--   4. 审计表只追加；正常训练解析不逐条写审计（量太大），target 三元组
--      写 job payload 与模型 metadata。

CREATE TABLE IF NOT EXISTS qm_label_targets (
    id              TEXT        NOT NULL,
    version         INTEGER     NOT NULL CHECK (version >= 1),
    market          TEXT        NOT NULL,
    family          TEXT        NOT NULL,
    mode            TEXT        NOT NULL,
    spec            JSONB       NOT NULL,
    spec_hash       TEXT        NOT NULL CHECK (spec_hash ~ '^[0-9a-f]{64}$'),
    status          TEXT        NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft', 'active', 'deprecated')),
    created_by      TEXT        NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    activated_by    TEXT,
    activated_at    TIMESTAMPTZ,
    deprecated_by   TEXT,
    deprecated_at   TIMESTAMPTZ,
    note            TEXT,
    PRIMARY KEY (id, version),
    CHECK (status <> 'active'
           OR (activated_by IS NOT NULL AND activated_at IS NOT NULL)),
    CHECK (status <> 'deprecated'
           OR (deprecated_by IS NOT NULL AND deprecated_at IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS idx_qm_label_targets_status
    ON qm_label_targets (status, market, family);

CREATE TABLE IF NOT EXISTS qm_label_target_audit (
    audit_id        BIGSERIAL PRIMARY KEY,
    target_id       TEXT        NOT NULL,
    target_version  INTEGER     NOT NULL,
    action          TEXT        NOT NULL
                    CHECK (action IN ('create', 'activate', 'deprecate', 'blocked_resolve')),
    actor           TEXT        NOT NULL,
    at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    job_id          TEXT,
    detail          JSONB
);

CREATE INDEX IF NOT EXISTS idx_qm_label_target_audit_target
    ON qm_label_target_audit (target_id, target_version, at DESC);

CREATE OR REPLACE FUNCTION qm_label_targets_guard() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'qm_label_targets is append-only';
    END IF;
    IF NEW.id <> OLD.id
       OR NEW.version <> OLD.version
       OR NEW.spec IS DISTINCT FROM OLD.spec
       OR NEW.spec_hash <> OLD.spec_hash
       OR NEW.market <> OLD.market
       OR NEW.family <> OLD.family
       OR NEW.mode <> OLD.mode THEN
        RAISE EXCEPTION 'qm_label_targets spec is immutable (id=% version=%)',
            OLD.id, OLD.version;
    END IF;
    IF NOT (
        (OLD.status = 'draft'  AND NEW.status = 'active')
     OR (OLD.status = 'active' AND NEW.status = 'deprecated')
     OR (OLD.status = 'draft'  AND NEW.status = 'deprecated')
    ) THEN
        RAISE EXCEPTION 'invalid target status transition: % -> %',
            OLD.status, NEW.status;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_qm_label_targets_guard ON qm_label_targets;
CREATE TRIGGER trg_qm_label_targets_guard
BEFORE UPDATE OR DELETE ON qm_label_targets
FOR EACH ROW EXECUTE FUNCTION qm_label_targets_guard();
