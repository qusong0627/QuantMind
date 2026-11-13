-- ============================================================
-- upgrade_v1.0.18: 清理 50 个已下线第三方 A 股策略的系统同步副本（幂等）
--
-- 背景：strategy_templates/as01_*.py|.json ~ as50_*.py|.json 已从仓库删除。
--   存量库里由 backend/shared/strategy_template_sync.py::sync_builtin_templates
--   写入个人策略库的 SystemSync 副本不会自动消失，仍会出现在「策略中心」。
--
-- 清理范围（保守，只删确信是系统同步副本的行）：
--   1. tags 含 'SystemSync'；
--   2. parameters.strategy_type 命中 as01 ~ as50；
--   3. is_public = FALSE（用户主动公开的不动）；
--   4. 未被 qm_strategy_model_bindings / strategy_loop_tasks 引用（表不存在时跳过该条件）。
--
-- 幂等：重复执行删除 0 行，可随启动重复重放。
-- 自动执行：
--   1) backend/main_oss.py 的 _ensure_upgrade_scripts 每次启动扫描重放；
--   2) deploy/update.sh 第 4 步按 schema_migrations 去重执行（升级前默认会 pg_dump 备份）。
-- 手工预检 / 定向清理（推荐在正式升级前先 dry-run）：
--   docker exec quantmind python backend/scripts/cleanup_removed_ashare_strategy_sync.py
--   docker exec quantmind python backend/scripts/cleanup_removed_ashare_strategy_sync.py --apply
-- ============================================================

DO $$
DECLARE
    removed INTEGER := 0;
    extra_cond TEXT := '';
    delete_sql TEXT;
BEGIN
    IF to_regclass('public.strategies') IS NULL THEN
        RAISE NOTICE 'v1.0.18: strategies 表不存在，跳过';
        RETURN;
    END IF;

    -- 被模型绑定 / 策略循环任务引用的行保留，避免留下悬空引用。
    IF to_regclass('public.qm_strategy_model_bindings') IS NOT NULL THEN
        extra_cond := extra_cond || ' AND NOT EXISTS ('
            || 'SELECT 1 FROM public.qm_strategy_model_bindings b '
            || 'WHERE b.strategy_id::text = s.id::text)';
    END IF;
    IF to_regclass('public.strategy_loop_tasks') IS NOT NULL THEN
        extra_cond := extra_cond || ' AND NOT EXISTS ('
            || 'SELECT 1 FROM public.strategy_loop_tasks t '
            || 'WHERE t.request_json->>''strategy_id'' = s.id::text)';
    END IF;

    delete_sql := 'DELETE FROM public.strategies s'
        || ' WHERE ''SystemSync'' = ANY(s.tags)'
        || ' AND COALESCE(s.parameters->>''strategy_type'', '''')'
        || '     ~ ''^as(0[1-9]|[1-4][0-9]|50)_'''
        || ' AND COALESCE(s.is_public, FALSE) = FALSE'
        || extra_cond;

    EXECUTE delete_sql;
    GET DIAGNOSTICS removed = ROW_COUNT;
    RAISE NOTICE 'v1.0.18: 已清理 % 条 A 股系统同步策略', removed;
END $$;
